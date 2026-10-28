using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Xass.Native.Services;

public sealed class VoiceShutdownException : InvalidOperationException
{
    public VoiceShutdownException() : base("Не удалось подтвердить остановку микрофона. Закройте XASS перед повторным запуском.") { }
}

public sealed record BackgroundVoiceEvent(string Kind, string Value, int Epoch = 0);

// One isolated, long-lived local model process, never an agent or remote command transport.
public sealed class BackgroundVoiceClient : IDisposable
{
    private readonly Process process;
    private readonly CancellationTokenSource stop;
    private readonly CancellationTokenRegistration killOnCancellation;
    private readonly SemaphoreSlim writes = new(1, 1);
    private readonly ConcurrentDictionary<int, TaskCompletionSource> pauses = new();
    private readonly IProgress<BackgroundVoiceEvent> progress;
    private int nextId;
    private int stopped;
    public bool IsStopped => Volatile.Read(ref stopped) != 0;
    public Task Completion { get; private set; } = Task.CompletedTask;

    private BackgroundVoiceClient(Process process, CancellationToken lifetime, IProgress<BackgroundVoiceEvent> progress)
    {
        this.process = process;
        this.progress = progress;
        stop = CancellationTokenSource.CreateLinkedTokenSource(lifetime);
        killOnCancellation = stop.Token.Register(Kill);
    }

    public static Task<BackgroundVoiceClient> StartAsync(string python, string modelPath, int epoch,
        IProgress<BackgroundVoiceEvent> progress, CancellationToken cancellation) => Task.Run(async () =>
    {
        var start = AssistantProcessStart.Create(python, background: true);
        var process = new Process { StartInfo = start };
        cancellation.ThrowIfCancellationRequested();
        process.Start();
        var client = new BackgroundVoiceClient(process, cancellation, progress);
        client.Completion = client.ReadAsync();
        try
        {
            await client.SendAsync(new { operation = "start", model_path = modelPath, epoch, paused = true }, cancellation);
            return client;
        }
        catch { client.Stop(); await client.EnsureStoppedAsync(); client.Dispose(); throw; }
    }, cancellation);

    public async Task PauseAsync(CancellationToken cancellation)
    {
        if (Volatile.Read(ref stopped) != 0) { await EnsureStoppedAsync(); return; }
        int id = Interlocked.Increment(ref nextId);
        var ack = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        pauses[id] = ack;
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation, stop.Token);
        deadline.CancelAfter(TimeSpan.FromSeconds(3));
        try
        {
            await SendAsync(new { operation = "pause", id }, deadline.Token);
            await ack.Task.WaitAsync(deadline.Token);
        }
        catch
        {
            // Never play synthesized speech while microphone shutdown is uncertain.
            Stop();
            throw;
        }
        finally { pauses.TryRemove(id, out _); }
    }

    public Task ResumeAsync(int epoch, CancellationToken cancellation) =>
        SendAsync(new { operation = "resume", epoch }, cancellation);

    private async Task SendAsync(object request, CancellationToken cancellation)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellation, stop.Token);
        linked.CancelAfter(TimeSpan.FromSeconds(3));
        await writes.WaitAsync(linked.Token);
        try
        {
            await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(request).AsMemory(), linked.Token);
            await process.StandardInput.FlushAsync(linked.Token);
        }
        finally { writes.Release(); }
    }

    private async Task ReadAsync()
    {
        Task errors = DrainErrorsAsync();
        try
        {
            // Model loading is bounded, but an explicitly enabled idle listener is not.
            using var loading = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
            loading.CancelAfter(TimeSpan.FromSeconds(180));
            bool ready = false;
            while (true)
            {
                var token = ready ? stop.Token : loading.Token;
                string? line = await ReadLineAsync(process.StandardOutput, token);
                if (line is null) break;
                using var json = JsonDocument.Parse(line);
                var value = json.RootElement;
                string? type = value.GetProperty("type").GetString();
                if (type == "state")
                {
                    string state = value.GetProperty("state").GetString() ?? "";
                    if (state is not ("loading_model" or "ready" or "listening" or "paused" or "transcribing" or "stopped"))
                        throw new InvalidOperationException("Неизвестное состояние локального микрофона.");
                    if (state is "ready" or "listening" or "transcribing") ready = true;
                    if (state == "paused" && value.TryGetProperty("id", out var id) && pauses.TryRemove(id.GetInt32(), out var ack))
                        ack.TrySetResult();
                    progress.Report(new("state", state));
                }
                else if (type == "command")
                {
                    string text = value.GetProperty("text").GetString() ?? "";
                    int epoch = value.GetProperty("epoch").GetInt32();
                    if (text.Length is < 1 or > 512 || text.Any(char.IsControl)
                        || !Regex.IsMatch(text, @"^\s*Джарвис(?:[\s,.:;!?—–-]+)\S", RegexOptions.IgnoreCase))
                        throw new InvalidOperationException("Неверная адресованная команда микрофона.");
                    progress.Report(new("command", text, epoch));
                }
                else if (type == "error")
                    throw new InvalidOperationException("Фоновый микрофон остановлен. Проверьте Python, локальную модель, устройство и разрешения Windows.");
                else throw new InvalidOperationException("Неверный ответ фонового микрофона.");
            }
            await process.WaitForExitAsync(stop.Token);
            await errors;
            if (!stop.IsCancellationRequested)
                throw new InvalidOperationException("Фоновый микрофон завершился. Включите его снова после проверки настроек.");
        }
        finally
        {
            Stop();
            try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3)); } catch { }
            try { await errors; } catch { }
            foreach (var pending in pauses.Values) pending.TrySetCanceled();
            pauses.Clear();
        }
    }

    private static async Task<string?> ReadLineAsync(StreamReader reader, CancellationToken token)
    {
        var line = new StringBuilder();
        var buffer = new char[1];
        while (await reader.ReadAsync(buffer.AsMemory(), token) != 0)
        {
            if (buffer[0] == '\n') return line.ToString();
            if (line.Length >= 8192) throw new InvalidOperationException("Ответ микрофона слишком большой.");
            line.Append(buffer[0]);
        }
        return line.Length == 0 ? null : line.ToString();
    }

    private async Task DrainErrorsAsync()
    {
        // Discard private dependency errors, bounded to prevent unbounded output.
        var buffer = new char[2048];
        int total = 0, count;
        while ((count = await process.StandardError.ReadAsync(buffer.AsMemory(), stop.Token)) != 0)
        {
            total += count;
            if (total > 65536) { Stop(); throw new InvalidOperationException("Процесс микрофона превысил лимит ошибок."); }
        }
    }

    private void Kill()
    {
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
    }

    public void Stop()
    {
        if (Interlocked.Exchange(ref stopped, 1) == 0) stop.Cancel();
        else Kill();
    }

    public async Task EnsureStoppedAsync()
    {
        await ObserveExitAsync();
        if (!process.HasExited)
            throw new VoiceShutdownException();
    }

    public async Task ObserveExitAsync() { try { await Completion; } catch { } }

    public void Dispose()
    {
        Stop();
        if (!process.HasExited) throw new VoiceShutdownException();
        // Caller awaits Completion before disposal. Stop itself always signals termination synchronously.
        killOnCancellation.Dispose();
        stop.Dispose();
        process.Dispose();
        writes.Dispose();
    }
}
