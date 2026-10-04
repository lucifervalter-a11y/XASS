using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Xass.Native.Services;

// Separate short-lived worker: no microphone, model, agent or credentials at startup.
public sealed class AssistantClient
{
    public Task<JsonElement> RequestAsync(string python, object request, IProgress<string> progress,
        CancellationToken cancellation) => Task.Run(async () =>
    {
        if (!Path.IsPathFullyQualified(python) || !File.Exists(python))
            throw new InvalidOperationException("Укажите полный путь к Python для голосового помощника.");
        string script = Path.Combine(AppContext.BaseDirectory, "assistant_bridge.py");
        if (!File.Exists(script)) throw new InvalidOperationException("В сборке отсутствует assistant_bridge.py.");
        var start = new ProcessStartInfo
        {
            FileName = python, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = AppContext.BaseDirectory
        };
        start.ArgumentList.Add("-I"); start.ArgumentList.Add("-B"); start.ArgumentList.Add(script);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        timeout.CancelAfter(TimeSpan.FromSeconds(180));
        using var process = new Process { StartInfo = start };
        cancellation.ThrowIfCancellationRequested();
        process.Start();
        using var registration = timeout.Token.Register(() =>
        {
            try { if (!process.HasExited) process.Kill(); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        });
        try
        {
            var errors = DrainErrorsAsync(process.StandardError, timeout.Token);
            await process.StandardInput.WriteAsync(JsonSerializer.Serialize(request).AsMemory(), timeout.Token);
            process.StandardInput.Close();
            JsonElement? final = null;
            int total = 0;
            while (true)
            {
                string? line = await ReadLineAsync(process.StandardOutput, timeout.Token);
                if (line is null) break;
                total += line.Length;
                if (total > 65536) throw new InvalidOperationException("Помощник превысил размер ответа.");
                using var document = JsonDocument.Parse(line);
                var value = document.RootElement;
                if (value.GetProperty("type").GetString() == "progress")
                    progress.Report(value.GetProperty("state").GetString() ?? "working");
                else if (value.GetProperty("type").GetString() == "result" && final is null)
                    final = value.Clone();
                else throw new InvalidOperationException("Неверный ответ помощника.");
            }
            await Task.WhenAll(errors, process.WaitForExitAsync(timeout.Token));
            if (process.ExitCode != 0 || final is null)
                throw new InvalidOperationException("Процесс помощника завершился без результата.");
            if (!final.Value.GetProperty("ok").GetBoolean())
                throw new InvalidOperationException(final.Value.GetProperty("error").GetString());
            return final.Value.GetProperty("result").Clone();
        }
        catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
        {
            throw new TimeoutException("Помощник не завершил операцию за 180 секунд.");
        }
        finally
        {
            // Stop capture/inference; leave an already dispatched application alone.
            try { if (!process.HasExited) process.Kill(); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        }
    }, cancellation);

    private static async Task<string?> ReadLineAsync(StreamReader reader, CancellationToken cancellation)
    {
        var line = new StringBuilder();
        var buffer = new char[1];
        while (await reader.ReadAsync(buffer.AsMemory(), cancellation) != 0)
        {
            if (buffer[0] == '\n') return line.ToString();
            if (line.Length >= 16384) throw new InvalidOperationException("Слишком длинный ответ помощника.");
            line.Append(buffer[0]);
        }
        return line.Length == 0 ? null : line.ToString();
    }

    private static async Task DrainErrorsAsync(StreamReader reader, CancellationToken cancellation)
    {
        // Do not surface dependency traces or paths. Bound output while draining
        // to avoid a pipe deadlock during model initialization.
        var buffer = new char[4096];
        int total = 0, count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), cancellation)) != 0)
        {
            total += count;
            if (total > 65536) throw new InvalidOperationException("Слишком большой журнал процесса помощника.");
        }
    }
}
