using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Xass.Native.Services;

// Long-lived child owned directly by the native window. Cancelling a single
// request never kills a dispatched agent or a durable archive copy.
public sealed class DesktopHostClient : IAsyncDisposable
{
    private Process? process;
    private readonly SemaphoreSlim gate = new(1, 1);
    private int serial;
    private Task? drain;
    public bool IsRunning => process is { HasExited: false };

    public async Task StartAsync(string runtime, string source, string data, CancellationToken token)
    {
        await gate.WaitAsync(token);
        try
        {
            if (IsRunning) return;
            if (!Path.IsPathFullyQualified(runtime) || !File.Exists(runtime)
                || !Path.IsPathFullyQualified(source) || !Directory.Exists(source)
                || !Path.IsPathFullyQualified(data) || !Directory.Exists(data))
                throw new InvalidOperationException("Выберите установленную среду XASS и папку данных.");
            var start = new ProcessStartInfo(runtime)
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = data
            };
            if (Path.GetFileName(runtime).Equals("XASS.NativeHelper.exe", StringComparison.OrdinalIgnoreCase))
            { start.ArgumentList.Add("--role"); start.ArgumentList.Add("background-agent"); }
            else
            {
                start.ArgumentList.Add("-I"); start.ArgumentList.Add("-B");
                start.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "background_agent.py"));
            }
            start.ArgumentList.Add("--source"); start.ArgumentList.Add(source);
            start.ArgumentList.Add("--data"); start.ArgumentList.Add(data);
            start.Environment["XASS_DATA_ROOT"] = data;
            start.Environment["PYTHONIOENCODING"] = "utf-8";
            process?.Dispose(); process = new Process { StartInfo = start };
            await Task.Run(() => { token.ThrowIfCancellationRequested(); process.Start(); }, token);
            drain = DrainErrorsAsync(process.StandardError);
        }
        finally { gate.Release(); }
    }

    public async Task<JsonElement> RequestAsync(object request, CancellationToken token)
    {
        await gate.WaitAsync(token);
        try
        {
            if (!IsRunning) throw new InvalidOperationException("Фоновая служба не запущена.");
            int id = ++serial;
            var body = JsonSerializer.SerializeToNode(request)?.AsObject() ?? throw new InvalidOperationException();
            body["id"] = id;
            string wire = body.ToJsonString();
            if (wire.Length > 96 * 1024) throw new InvalidOperationException("Слишком большой запрос.");
            await process!.StandardInput.WriteLineAsync(wire);
            await process.StandardInput.FlushAsync();
            // Consume this response even if caller's page is cancelled, keeping
            // the next request aligned. Only final disposal closes the host.
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(45));
            string line = await ReadLineBoundedAsync(process.StandardOutput, deadline.Token);
            using var doc = JsonDocument.Parse(line);
            if (doc.RootElement.GetProperty("id").GetInt32() != id)
                throw new InvalidOperationException("Ответ службы не совпал с запросом.");
            token.ThrowIfCancellationRequested();
            if (!doc.RootElement.GetProperty("ok").GetBoolean())
                throw new InvalidOperationException("Действие не выполнено. Проверьте состояние агента и повторите.");
            return doc.RootElement.GetProperty("result").Clone();
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            // Protocol no longer aligned: close stdin so host performs cleanup.
            process?.StandardInput.Close();
            throw new TimeoutException("Служба не ответила вовремя. Перезапустите приложение.");
        }
        finally { gate.Release(); }
    }

    private static async Task<string> ReadLineBoundedAsync(StreamReader reader, CancellationToken token)
    {
        var value = new StringBuilder(); var buffer = new char[1];
        while (await reader.ReadAsync(buffer.AsMemory(), token) != 0)
        {
            if (buffer[0] == '\n') return value.ToString();
            value.Append(buffer[0]);
            if (value.Length > 524288) throw new InvalidOperationException("Слишком большой ответ службы.");
        }
        throw new InvalidOperationException("Фоновая служба завершилась.");
    }
    private static async Task DrainErrorsAsync(StreamReader reader)
    { var chars = new char[4096]; while (await reader.ReadAsync(chars.AsMemory()) > 0) { } }
    public async Task CloseAsync()
    {
        if (IsRunning)
        {
            try { await RequestAsync(new { action = "host_quit" }, CancellationToken.None); }
            catch { /* The closed input also triggers the host's finally. */ }
            try { process!.StandardInput.Close(); } catch { }
            try { using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15)); await process!.WaitForExitAsync(timeout.Token); }
            catch { try { if (IsRunning) process!.Kill(entireProcessTree: true); } catch { } }
        }
        process?.Dispose(); process = null;
    }
    public async ValueTask DisposeAsync() => await CloseAsync();
}
