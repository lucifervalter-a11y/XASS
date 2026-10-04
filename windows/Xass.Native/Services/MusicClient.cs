using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Xass.Native.Services;

// Dedicated, UI-owned audio process. Never spawned by the one-shot AgentClient.
// Closing a request never kills audio; only final UI quit disposes this owner.
public sealed class MusicClient : IAsyncDisposable
{
    private Process? process;
    private readonly SemaphoreSlim gate = new(1, 1);
    private int sequence;
    private bool disposed;
    private const int MaxOutput = 2 * 1024 * 1024;
    private string context = "";

    public Task<JsonElement> RequestAsync(object request, string python, string source, string data, CancellationToken token) =>
        Task.Run(() => RequestCoreAsync(request, python, source, data, token), token);

    private async Task<JsonElement> RequestCoreAsync(object request, string python, string source, string data, CancellationToken token)
    {
        await gate.WaitAsync(token).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(disposed, this);
            string nextContext = Path.GetFullPath(data) + "\n" + Path.GetFullPath(source);
            if (process is not null && (process.HasExited || nextContext != context)) StopProcess();
            if (process is null)
            {
                string helper = Path.Combine(AppContext.BaseDirectory, "runtime", "XASS.NativeHelper.exe");
                var start = new ProcessStartInfo { UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
                if (File.Exists(helper))
                {
                    start.FileName = helper;
                    start.ArgumentList.Add("--role"); start.ArgumentList.Add("desktop-music");
                }
                else
                {
                    if (!Path.IsPathFullyQualified(python) || !File.Exists(python))
                        throw new InvalidOperationException("Музыкальный модуль не установлен. Проверьте установку XASS или путь Python.");
                    start.FileName = python;
                    start.ArgumentList.Add("-I"); start.ArgumentList.Add("-B");
                    start.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "desktop_music_service.py"));
                }
                if (!Path.IsPathFullyQualified(source) || !Directory.Exists(source) || !Path.IsPathFullyQualified(data))
                    throw new InvalidOperationException("Проверьте локальные папки XASS в настройках подключения.");
                start.ArgumentList.Add("--source"); start.ArgumentList.Add(source);
                start.ArgumentList.Add("--data"); start.ArgumentList.Add(data);
                start.WorkingDirectory = AppContext.BaseDirectory;
                process = new Process { StartInfo = start };
                process.Start();
                process.BeginErrorReadLine(); // Drain silently; raw errors may contain private paths.
                context = nextContext;
            }
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
            timeout.CancelAfter(TimeSpan.FromSeconds(25));
            JsonObject body = JsonSerializer.SerializeToNode(request)?.AsObject() ?? throw new InvalidOperationException();
            body["version"] = 1; body["id"] = ++sequence;
            string input = body.ToJsonString();
            if (input.Length > 192 * 1024) throw new InvalidOperationException("Выбрано слишком много файлов.");
            try
            {
                await process.StandardInput.WriteLineAsync(input.AsMemory(), timeout.Token).ConfigureAwait(false);
                await process.StandardInput.FlushAsync(timeout.Token).ConfigureAwait(false);
                string output = await ReadLineBoundedAsync(process.StandardOutput, timeout.Token).ConfigureAwait(false);
                using JsonDocument parsed = JsonDocument.Parse(output);
                JsonElement envelope = parsed.RootElement;
                if (envelope.GetProperty("version").GetInt32() != 1 || envelope.GetProperty("id").GetInt32() != sequence)
                    throw new InvalidOperationException("Ответ плеера устарел. Повторите действие.");
                if (!envelope.GetProperty("ok").GetBoolean())
                    throw new InvalidOperationException(envelope.GetProperty("error").GetString() ?? "Не удалось выполнить действие плеера.");
                return envelope.GetProperty("result").Clone();
            }
            catch (OperationCanceledException)
            {
                // A partially consumed JSONL response cannot be reused. This
                // stops this UI's local audio only, never the background agent.
                StopProcess();
                throw;
            }
            catch (IOException) { StopProcess(); throw new InvalidOperationException("Музыкальный модуль отключился. Повторите действие."); }
        }
        finally { gate.Release(); }
    }

    private static async Task<string> ReadLineBoundedAsync(StreamReader reader, CancellationToken token)
    {
        var result = new StringBuilder();
        char[] next = new char[4096];
        int count;
        while ((count = await reader.ReadAsync(next.AsMemory(), token).ConfigureAwait(false)) != 0)
        {
            int newline = Array.IndexOf(next, '\n', 0, count);
            int take = newline < 0 ? count : newline;
            if (result.Length + take > MaxOutput) throw new IOException("Music response too large");
            result.Append(next, 0, take);
            if (newline >= 0)
            {
                if (newline != count - 1) throw new IOException("Unexpected music response");
                return result.ToString();
            }
        }
        throw new IOException("Music service exited");
    }

    private void StopProcess()
    {
        Process? old = process; process = null;
        if (old is null) return;
        try { old.StandardInput.Close(); if (!old.WaitForExit(1500)) old.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
        finally { old.Dispose(); }
    }

    public async ValueTask DisposeAsync()
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try { disposed = true; await Task.Run(StopProcess).ConfigureAwait(false); }
        finally { gate.Release(); }
    }
}
