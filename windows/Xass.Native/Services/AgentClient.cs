using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Xass.Native.Services;

// Credentials never enter this process. A one-shot trusted Python adapter uses
// the existing agent configuration and returns only explicitly projected data.
public sealed class AgentClient
{
    public string PythonPath { get; set; } = "";
    public string SourcePath { get; set; } = "";
    public string DataPath { get; set; } = "";
    private const int MaxOutput = 524288;

    public Task<JsonElement> RequestAsync(object request, CancellationToken cancellation) =>
        // Path probes and Process.Start can stall on AV or a slow filesystem.
        // Move the complete request lifecycle off the WinUI dispatcher.
        Task.Run(() => RequestCoreAsync(request, cancellation), cancellation);

    private async Task<JsonElement> RequestCoreAsync(object request, CancellationToken cancellation)
    {
        cancellation.ThrowIfCancellationRequested();
        string helper = Path.Combine(AppContext.BaseDirectory, "runtime", "XASS.NativeHelper.exe");
        bool bundled = File.Exists(helper);
        if ((!bundled && (!Path.IsPathFullyQualified(PythonPath) || !File.Exists(PythonPath)))
            || !Path.IsPathFullyQualified(SourcePath) || !Directory.Exists(SourcePath)
            || !Path.IsPathFullyQualified(DataPath) || !Directory.Exists(DataPath))
            throw new InvalidOperationException("Укажите существующие абсолютные пути к Python, pc_client и данным агента.");
        string adapter = Path.Combine(AppContext.BaseDirectory, "bridge.py");
        if (!bundled && !File.Exists(adapter)) throw new InvalidOperationException("В сборке отсутствует bridge.py.");
        var start = new ProcessStartInfo
        {
            FileName = bundled ? helper : PythonPath, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = SourcePath
        };
        if (bundled)
        {
            start.ArgumentList.Add("--role"); start.ArgumentList.Add("agent-bridge");
        }
        else
        {
            start.ArgumentList.Add("-I"); // Ignore PYTHONPATH and user-site startup hooks.
            start.ArgumentList.Add("-B"); // Adapter must not write bytecode into the checkout.
            start.ArgumentList.Add(adapter);
        }
        start.Environment["XASS_DATA_ROOT"] = DataPath;
        start.ArgumentList.Add("--source"); start.ArgumentList.Add(SourcePath);
        start.ArgumentList.Add("--data"); start.ArgumentList.Add(DataPath);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        string requestAction = JsonSerializer.SerializeToElement(request).TryGetProperty("action", out var requestedAction)
            ? requestedAction.GetString() ?? "" : "";
        if (requestAction == "desktop_pair") timeout.CancelAfter(TimeSpan.FromSeconds(90));
        else timeout.CancelAfter(TimeSpan.FromSeconds(20));
        using var process = new Process { StartInfo = start };
        cancellation.ThrowIfCancellationRequested();
        process.Start();
        using var kill = timeout.Token.Register(() =>
        {
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        });
        try
        {
            Task<string> output = ReadBoundedAsync(process.StandardOutput, timeout.Token);
            Task<string> errors = ReadBoundedAsync(process.StandardError, timeout.Token);
            await process.StandardInput.WriteAsync(JsonSerializer.Serialize(request).AsMemory(), timeout.Token);
            process.StandardInput.Close();
            await Task.WhenAll(output, errors, process.WaitForExitAsync(timeout.Token));
            string text = await output;
            await errors; // Deliberately never display raw stderr: it can contain secret exception data.
            if (process.ExitCode != 0) throw new InvalidOperationException("Адаптер агента завершился с ошибкой. Проверьте Python и зависимости.");
            using JsonDocument response = JsonDocument.Parse(text);
            if (!response.RootElement.GetProperty("ok").GetBoolean())
                throw new InvalidOperationException("Действие не выполнено. Проверьте привязку, работающий агент и сеть.");
            return response.RootElement.GetProperty("result").Clone();
        }
        catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
        {
            throw new TimeoutException("Служба не ответила вовремя. Проверьте состояние перед повторной привязкой.");
        }
        finally
        {
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        }
    }

    private static async Task<string> ReadBoundedAsync(StreamReader reader, CancellationToken token)
    {
        var text = new StringBuilder();
        char[] buffer = new char[4096];
        int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), token)) != 0)
        {
            if (text.Length + count > MaxOutput) throw new InvalidOperationException("Ответ агента превышает допустимый размер.");
            text.Append(buffer, 0, count);
        }
        return text.ToString();
    }
}
