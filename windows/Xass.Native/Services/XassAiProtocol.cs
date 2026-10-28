using System.Collections.Concurrent;
using System.Globalization;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace Xass.Native.Services;

public sealed class XassAiProtocolException : Exception
{
    public XassAiProtocolException(string message) : base(message) { }
    public XassAiProtocolException(string message, Exception innerException) : base(message, innerException) { }
}

public sealed class XassAiCancellationException : OperationCanceledException
{
    public XassAiCancellationException(bool confirmed, CancellationToken cancellationToken)
        : base(confirmed
            ? "XASS AI confirmed that the request was stopped."
            : "XASS AI did not confirm that the request stopped.", null, cancellationToken)
        => Confirmed = confirmed;

    public bool Confirmed { get; }
}

/// <summary>
/// Authenticated loopback endpoint. The token deliberately has no public accessor and
/// ToString never includes it, so diagnostics cannot accidentally persist the credential.
/// </summary>
public sealed class XassAiRuntimeEndpoint
{
    private readonly string sessionToken;

    internal XassAiRuntimeEndpoint(int port, string sessionToken)
    {
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        if (string.IsNullOrWhiteSpace(sessionToken) || sessionToken.Length > 1024)
            throw new ArgumentException("A bounded in-memory session token is required.", nameof(sessionToken));
        BaseUri = new Uri($"http://127.0.0.1:{port}/", UriKind.Absolute);
        this.sessionToken = sessionToken;
    }

    public Uri BaseUri { get; }

    internal Uri WebSocketUri => new UriBuilder(BaseUri)
    {
        Scheme = "ws",
        Path = "/api/ws",
        Query = "token=" + Uri.EscapeDataString(sessionToken)
    }.Uri;

    public override string ToString() => BaseUri.GetLeftPart(UriPartial.Authority);
}

internal readonly record struct XassAiSocketMessage(WebSocketMessageType Type, string Text);

internal interface IXassAiWebSocket : IAsyncDisposable
{
    WebSocketState State { get; }
    Task ConnectAsync(Uri uri, CancellationToken cancellationToken);
    Task SendTextAsync(string text, CancellationToken cancellationToken);
    Task<XassAiSocketMessage> ReceiveAsync(CancellationToken cancellationToken);
    Task CloseAsync(CancellationToken cancellationToken);
}

internal sealed class XassAiClientWebSocket : IXassAiWebSocket
{
    private const int MaximumFrameBytes = 1024 * 1024;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);
    private readonly ClientWebSocket socket = new();

    public WebSocketState State => socket.State;
    public Task ConnectAsync(Uri uri, CancellationToken cancellationToken) => socket.ConnectAsync(uri, cancellationToken);

    public async Task SendTextAsync(string text, CancellationToken cancellationToken)
    {
        byte[] payload = Encoding.UTF8.GetBytes(text);
        if (payload.Length > MaximumFrameBytes) throw new XassAiProtocolException("XASS AI request is too large.");
        await socket.SendAsync(payload, WebSocketMessageType.Text, true, cancellationToken).ConfigureAwait(false);
    }

    public async Task<XassAiSocketMessage> ReceiveAsync(CancellationToken cancellationToken)
    {
        byte[] buffer = new byte[16 * 1024];
        using var content = new MemoryStream();
        WebSocketMessageType? type = null;
        while (true)
        {
            ValueWebSocketReceiveResult result = await socket.ReceiveAsync(buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
            type ??= result.MessageType;
            if (result.MessageType != type) throw new XassAiProtocolException("XASS AI returned a mixed frame.");
            if (result.MessageType == WebSocketMessageType.Close)
                return new XassAiSocketMessage(WebSocketMessageType.Close, string.Empty);
            if (content.Length + result.Count > MaximumFrameBytes)
                throw new XassAiProtocolException("XASS AI returned an oversized frame.");
            content.Write(buffer, 0, result.Count);
            if (!result.EndOfMessage) continue;
            if (result.MessageType != WebSocketMessageType.Text)
                throw new XassAiProtocolException("XASS AI returned an unsupported binary frame.");
            return new XassAiSocketMessage(WebSocketMessageType.Text, StrictUtf8.GetString(content.ToArray()));
        }
    }

    public async Task CloseAsync(CancellationToken cancellationToken)
    {
        if (socket.State is WebSocketState.Open or WebSocketState.CloseReceived)
            await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "XASS AI client closed", cancellationToken)
                .ConfigureAwait(false);
    }

    public ValueTask DisposeAsync()
    {
        socket.Dispose();
        return ValueTask.CompletedTask;
    }
}

internal sealed record XassAiGatewayEvent(string Type, string? SessionId, JsonElement Payload);

/// <summary>
/// Minimal fail-closed client for the installed gateway contract. It has no discovery,
/// remote-host, alternative endpoint, simulated-success, or process-management path.
/// </summary>
public sealed class XassAiProtocolClient : IAsyncDisposable
{
    private const string SupportedRuntimeVersion = "0.22.2";
    private const string SupportedRuntimeCommit = "d923a5eb487a17c0a68801bba9ef0f56c81040a8";
    private const int MaximumHealthBytes = 64 * 1024;
    private const int MaximumPromptCharacters = 64 * 1024;
    private const int MaximumAnswerCharacters = 2 * 1024 * 1024;
    private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan GatewayReadyTimeout = TimeSpan.FromSeconds(15);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(30);
    private static readonly TimeSpan PromptTimeout = TimeSpan.FromMinutes(30);

    private readonly XassAiRuntimeEndpoint endpoint;
    private readonly HttpClient http;
    private readonly IXassAiWebSocket socket;
    private readonly SemaphoreSlim sendGate = new(1, 1);
    private readonly SemaphoreSlim promptGate = new(1, 1);
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonElement>> pending = new();
    private readonly Channel<XassAiGatewayEvent> events = Channel.CreateBounded<XassAiGatewayEvent>(
        new BoundedChannelOptions(1024)
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleReader = true,
            SingleWriter = true
        });
    private readonly CancellationTokenSource lifetime = new();
    private Task? receiveLoop;
    private long nextRequestId;
    private bool connected;
    private bool disposed;

    public XassAiProtocolClient(XassAiRuntimeEndpoint endpoint)
        : this(endpoint, CreateHttpClient(), new XassAiClientWebSocket()) { }

    internal XassAiProtocolClient(XassAiRuntimeEndpoint endpoint, HttpClient http, IXassAiWebSocket socket)
    {
        this.endpoint = endpoint ?? throw new ArgumentNullException(nameof(endpoint));
        this.http = http ?? throw new ArgumentNullException(nameof(http));
        this.socket = socket ?? throw new ArgumentNullException(nameof(socket));
        EnsureLoopback(endpoint.BaseUri);
    }

    public bool IsConnected => connected && socket.State == WebSocketState.Open && !disposed;

    public async Task ConnectAsync(CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        if (IsConnected) return;
        await ProbeExactHealthAsync(cancellationToken).ConfigureAwait(false);
        await socket.ConnectAsync(endpoint.WebSocketUri, cancellationToken).ConfigureAwait(false);
        receiveLoop = Task.Run(() => ReceiveLoopAsync(lifetime.Token), CancellationToken.None);
        try
        {
            using var ready = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
            ready.CancelAfter(GatewayReadyTimeout);
            while (true)
            {
                XassAiGatewayEvent announced = await events.Reader.ReadAsync(ready.Token).ConfigureAwait(false);
                if (announced.Type == "gateway.ready") break;
            }
            connected = true;
        }
        catch
        {
            await CloseSocketQuietlyAsync().ConfigureAwait(false);
            throw;
        }
    }

    public async Task<string> AskAsync(string prompt, IProgress<string>? progress, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        if (!IsConnected) throw new InvalidOperationException("XASS AI is not connected.");
        prompt = prompt?.Trim() ?? string.Empty;
        if (prompt.Length == 0) throw new ArgumentException("Enter a request for XASS AI.", nameof(prompt));
        if (prompt.Length > MaximumPromptCharacters) throw new ArgumentException("The XASS AI request is too long.", nameof(prompt));

        await promptGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        string? sessionId = null;
        using var turn = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        Task<XassAiGatewayEvent>? pendingEvent = null;
        try
        {
            turn.CancelAfter(PromptTimeout);
            JsonElement created = await RequestAsync(
                "session.create",
                new Dictionary<string, object?>
                {
                    ["source"] = "xass-native",
                    ["title"] = "XASS AI",
                    ["close_on_disconnect"] = true,
                    ["follow_profile_config"] = true
                },
                RequestTimeout,
                turn.Token).ConfigureAwait(false);

            if (!created.TryGetProperty("session_id", out JsonElement sessionValue)
                || sessionValue.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionValue.GetString()))
                throw new XassAiProtocolException("XASS AI did not create a valid session.");

            sessionId = sessionValue.GetString()!;
            using var acknowledgement = CancellationTokenSource.CreateLinkedTokenSource(turn.Token);
            Task<JsonElement>? submitted = RequestAsync(
                "prompt.submit",
                new Dictionary<string, object?> { ["session_id"] = sessionId, ["text"] = prompt },
                PromptTimeout,
                acknowledgement.Token);
            var answer = new StringBuilder();

            while (true)
            {
                pendingEvent ??= events.Reader.ReadAsync(turn.Token).AsTask();
                if (submitted is not null && await Task.WhenAny(pendingEvent, submitted).ConfigureAwait(false) == submitted)
                {
                    // The RPC acknowledgement is not the completion signal. It is observed only
                    // so an explicit server rejection fails before message.complete.
                    await submitted.ConfigureAwait(false);
                    submitted = null;
                    continue;
                }

                XassAiGatewayEvent gatewayEvent = await pendingEvent.ConfigureAwait(false);
                pendingEvent = null;
                if (!string.Equals(gatewayEvent.SessionId, sessionId, StringComparison.Ordinal)) continue;
                if (gatewayEvent.Type == "message.delta")
                {
                    string chunk = ReadOptionalString(gatewayEvent.Payload, "text");
                    if (chunk.Length == 0) continue;
                    if (answer.Length + chunk.Length > MaximumAnswerCharacters)
                        throw new XassAiProtocolException("XASS AI response exceeded the safe display limit.");
                    answer.Append(chunk);
                    progress?.Report(chunk);
                    continue;
                }

                if (gatewayEvent.Type != "message.complete") continue;
                acknowledgement.Cancel();
                string status = ReadOptionalString(gatewayEvent.Payload, "status");
                if (status != "complete")
                    throw new XassAiProtocolException(status == "interrupted"
                        ? "XASS AI request was interrupted."
                        : "XASS AI could not complete the request.");

                string complete = ReadOptionalString(gatewayEvent.Payload, "text");
                if (answer.Length == 0 && complete.Length > 0)
                {
                    if (complete.Length > MaximumAnswerCharacters)
                        throw new XassAiProtocolException("XASS AI response exceeded the safe display limit.");
                    answer.Append(complete);
                    progress?.Report(complete);
                }
                return answer.ToString();
            }
        }
        catch (OperationCanceledException error)
        {
            bool confirmed = sessionId is null || await TryInterruptAsync(sessionId).ConfigureAwait(false);
            if (cancellationToken.IsCancellationRequested)
                throw new XassAiCancellationException(confirmed, cancellationToken);
            throw new TimeoutException(confirmed
                ? "XASS AI request timed out and was stopped."
                : "XASS AI request timed out; the stop was not confirmed.", error);
        }
        finally
        {
            turn.Cancel();
            if (pendingEvent is not null)
            {
                try { await pendingEvent.ConfigureAwait(false); }
                catch { /* Observe and retire this turn's read before the next turn can start. */ }
            }
            promptGate.Release();
        }
    }

    private async Task<bool> TryInterruptAsync(string sessionId)
    {
        if (disposed || socket.State != WebSocketState.Open) return false;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        bounded.CancelAfter(TimeSpan.FromSeconds(5));
        try
        {
            JsonElement result = await RequestAsync(
                "session.interrupt",
                new Dictionary<string, object?> { ["session_id"] = sessionId },
                TimeSpan.FromSeconds(5),
                bounded.Token).ConfigureAwait(false);
            return ReadOptionalString(result, "status") == "interrupted";
        }
        catch (Exception error) when (error is OperationCanceledException or XassAiProtocolException)
        {
            return false;
        }
    }

    private async Task ProbeExactHealthAsync(CancellationToken cancellationToken)
    {
        EnsureLoopback(endpoint.BaseUri);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        timeout.CancelAfter(HealthTimeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, new Uri(endpoint.BaseUri, "api/health"));
        request.Headers.Accept.ParseAdd("application/json");
        using HttpResponseMessage response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token)
            .ConfigureAwait(false);
        if (response.StatusCode != HttpStatusCode.OK)
            throw new XassAiProtocolException("XASS AI health check was rejected.");
        if (response.Content.Headers.ContentLength is > MaximumHealthBytes)
            throw new XassAiProtocolException("XASS AI health response is too large.");
        if (response.Content.Headers.ContentType is { MediaType: string mediaType }
            && !string.Equals(mediaType, "application/json", StringComparison.OrdinalIgnoreCase))
            throw new XassAiProtocolException("XASS AI health response is not JSON.");

        byte[] payload = await ReadBoundedContentAsync(response.Content, MaximumHealthBytes, timeout.Token)
            .ConfigureAwait(false);
        try
        {
            using JsonDocument document = JsonDocument.Parse(payload);
            JsonElement root = document.RootElement;
            if (!IsSupportedHealth(root))
                throw new XassAiProtocolException("XASS AI health response did not confirm local readiness.");
        }
        catch (JsonException error)
        {
            throw new XassAiProtocolException("XASS AI health response is malformed.", error);
        }
    }

    private static async Task<byte[]> ReadBoundedContentAsync(
        HttpContent content,
        int maximumBytes,
        CancellationToken cancellationToken)
    {
        await using Stream input = await content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        using var output = new MemoryStream(Math.Min(maximumBytes, 16 * 1024));
        byte[] buffer = new byte[8 * 1024];
        while (true)
        {
            int remaining = maximumBytes + 1 - checked((int)output.Length);
            int count = await input.ReadAsync(buffer.AsMemory(0, Math.Min(buffer.Length, remaining)), cancellationToken)
                .ConfigureAwait(false);
            if (count == 0) return output.ToArray();
            output.Write(buffer, 0, count);
            if (output.Length > maximumBytes)
                throw new XassAiProtocolException("XASS AI health response is too large.");
        }
    }

    private static bool IsSupportedHealth(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object) return false;
        var expected = new HashSet<string>(StringComparer.Ordinal)
        {
            "ok", "version", "displayVersion", "commit", "auth_required"
        };
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (JsonProperty property in root.EnumerateObject())
            if (!expected.Contains(property.Name) || !seen.Add(property.Name)) return false;
        if (!seen.SetEquals(expected)) return false;

        return root.GetProperty("ok").ValueKind == JsonValueKind.True
            && root.GetProperty("version").ValueKind == JsonValueKind.String
            && root.GetProperty("version").GetString() == SupportedRuntimeVersion
            && root.GetProperty("displayVersion").ValueKind == JsonValueKind.String
            && !string.IsNullOrWhiteSpace(root.GetProperty("displayVersion").GetString())
            && root.GetProperty("commit").ValueKind == JsonValueKind.String
            && root.GetProperty("commit").GetString() == SupportedRuntimeCommit
            // The supported owned runtime protects /api/ws with the ephemeral
            // in-memory token appended by ConnectAsync. An unauthenticated
            // health shape is not the runtime contract we are prepared to use.
            && root.GetProperty("auth_required").ValueKind == JsonValueKind.True;
    }

    private async Task<JsonElement> RequestAsync(
        string method,
        IReadOnlyDictionary<string, object?> parameters,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        string id = Interlocked.Increment(ref nextRequestId).ToString(CultureInfo.InvariantCulture);
        var completion = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!pending.TryAdd(id, completion)) throw new XassAiProtocolException("XASS AI request identifier collision.");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        deadline.CancelAfter(timeout);
        using CancellationTokenRegistration registration = deadline.Token.Register(() =>
        {
            if (pending.TryRemove(id, out TaskCompletionSource<JsonElement>? request))
                request.TrySetCanceled(deadline.Token);
        });
        try
        {
            string frame = JsonSerializer.Serialize(new { jsonrpc = "2.0", id, method, @params = parameters });
            await SendFrameAsync(frame, deadline.Token).ConfigureAwait(false);
            return await completion.Task.ConfigureAwait(false);
        }
        finally
        {
            pending.TryRemove(id, out _);
        }
    }

    private async Task SendFrameAsync(string frame, CancellationToken cancellationToken)
    {
        await sendGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try { await socket.SendTextAsync(frame, cancellationToken).ConfigureAwait(false); }
        finally { sendGate.Release(); }
    }

    private async Task ReceiveLoopAsync(CancellationToken cancellationToken)
    {
        Exception? failure = null;
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                XassAiSocketMessage message = await socket.ReceiveAsync(cancellationToken).ConfigureAwait(false);
                if (message.Type == WebSocketMessageType.Close)
                    throw new XassAiProtocolException("XASS AI connection closed.");
                await HandleFrameAsync(message.Text, cancellationToken).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { }
        catch (Exception error) { failure = error; }
        finally
        {
            connected = false;
            Exception terminal = failure ?? new OperationCanceledException("XASS AI connection stopped.");
            foreach (KeyValuePair<string, TaskCompletionSource<JsonElement>> pair in pending)
                if (pending.TryRemove(pair.Key, out TaskCompletionSource<JsonElement>? request)) request.TrySetException(terminal);
            events.Writer.TryComplete(failure);
        }
    }

    private async Task HandleFrameAsync(string text, CancellationToken cancellationToken)
    {
        JsonDocument document;
        try { document = JsonDocument.Parse(text); }
        catch (JsonException error) { throw new XassAiProtocolException("XASS AI returned malformed JSON-RPC.", error); }
        using (document)
        {
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("jsonrpc", out JsonElement protocol)
                || protocol.ValueKind != JsonValueKind.String
                || protocol.GetString() != "2.0")
                throw new XassAiProtocolException("XASS AI returned an invalid JSON-RPC frame.");

            if (root.TryGetProperty("id", out JsonElement idValue)
                && idValue.ValueKind is JsonValueKind.String or JsonValueKind.Number)
            {
                string id = idValue.ValueKind == JsonValueKind.String ? idValue.GetString()! : idValue.GetRawText();
                if (root.TryGetProperty("method", out JsonElement serverMethod)
                    && serverMethod.ValueKind == JsonValueKind.String)
                {
                    string response = JsonSerializer.Serialize(new
                    {
                        jsonrpc = "2.0",
                        id = idValue.Clone(),
                        error = new { code = -32601, message = "XASS AI does not support interactive server requests." }
                    });
                    await SendFrameAsync(response, cancellationToken).ConfigureAwait(false);
                    return;
                }

                if (!pending.TryRemove(id, out TaskCompletionSource<JsonElement>? completion)) return;
                if (root.TryGetProperty("error", out _))
                {
                    completion.TrySetException(new XassAiProtocolException("XASS AI rejected the request."));
                    return;
                }
                completion.TrySetResult(root.TryGetProperty("result", out JsonElement result)
                    ? result.Clone()
                    : JsonSerializer.SerializeToElement(new { }));
                return;
            }

            if (!root.TryGetProperty("method", out JsonElement methodValue)
                || methodValue.ValueKind != JsonValueKind.String
                || methodValue.GetString() != "event"
                || !root.TryGetProperty("params", out JsonElement eventValue)
                || eventValue.ValueKind != JsonValueKind.Object
                || !eventValue.TryGetProperty("type", out JsonElement typeValue)
                || typeValue.ValueKind != JsonValueKind.String)
                return;

            string? sessionId = eventValue.TryGetProperty("session_id", out JsonElement session)
                && session.ValueKind == JsonValueKind.String ? session.GetString() : null;
            JsonElement payload = eventValue.TryGetProperty("payload", out JsonElement eventPayload)
                && eventPayload.ValueKind == JsonValueKind.Object
                ? eventPayload.Clone()
                : JsonSerializer.SerializeToElement(new { });
            if (!events.Writer.TryWrite(new XassAiGatewayEvent(typeValue.GetString()!, sessionId, payload)))
                throw new XassAiProtocolException("XASS AI event buffer overflowed.");
        }
    }

    private static string ReadOptionalString(JsonElement value, string property)
        => value.ValueKind == JsonValueKind.Object
           && value.TryGetProperty(property, out JsonElement field)
           && field.ValueKind == JsonValueKind.String
            ? field.GetString() ?? string.Empty
            : string.Empty;

    private static HttpClient CreateHttpClient()
    {
        var handler = new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            UseCookies = false,
            UseProxy = false,
            AutomaticDecompression = DecompressionMethods.None,
            ConnectTimeout = HealthTimeout
        };
        return new HttpClient(handler, true) { Timeout = Timeout.InfiniteTimeSpan };
    }

    private static void EnsureLoopback(Uri uri)
    {
        if (!uri.IsAbsoluteUri
            || uri.Scheme != Uri.UriSchemeHttp
            || !IPAddress.TryParse(uri.Host, out IPAddress? address)
            || !IPAddress.IsLoopback(address)
            || !string.IsNullOrEmpty(uri.UserInfo))
            throw new XassAiProtocolException("XASS AI accepts only an authenticated loopback endpoint.");
    }

    private async Task CloseSocketQuietlyAsync()
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try { await socket.CloseAsync(timeout.Token).ConfigureAwait(false); }
        catch { }
    }

    public async ValueTask DisposeAsync()
    {
        if (disposed) return;
        disposed = true;
        connected = false;
        lifetime.Cancel();
        await CloseSocketQuietlyAsync().ConfigureAwait(false);
        if (receiveLoop is not null)
        {
            try { await receiveLoop.ConfigureAwait(false); }
            catch { }
        }
        bool promptDrained = false;
        using (var drain = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
        {
            try
            {
                await promptGate.WaitAsync(drain.Token).ConfigureAwait(false);
                promptDrained = true;
                promptGate.Release();
            }
            catch (OperationCanceledException) { }
        }
        await socket.DisposeAsync().ConfigureAwait(false);
        http.Dispose();
        if (promptDrained) lifetime.Dispose();
    }
}
