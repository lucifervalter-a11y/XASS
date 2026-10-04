using System.Diagnostics;
using System.Text.Json;

namespace Xass.Native.Services;

public sealed record PreparedNativeUpdate(string JobFolder);
public sealed class NativeUpdateCoordinator
{
    public static string UpdatesRoot => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "updates");
    public async Task<PreparedNativeUpdate> PrepareAsync(NativeUpdate update, string installer, IProgress<string> progress, CancellationToken token, bool automatic = false)
    {
        string install = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string marker = Path.Combine(install, "native-install.json"), runtime = Path.Combine(install, "runtime");
        if (!File.Exists(marker) || !File.Exists(Path.Combine(runtime, "XASS.NativeHelper.exe")))
            throw new InvalidOperationException("Автоматическое обновление поддерживается после установки полного нативного пакета.");
        using (var document = JsonDocument.Parse(await File.ReadAllTextAsync(marker, token)))
            if (document.RootElement.GetProperty("distribution").GetString() != "native-test"
                || document.RootElement.GetProperty("app_id").GetString() != "B4D7E8B9-9C58-4C36-A432-D114393006D8")
                throw new InvalidOperationException("Неизвестная установка XASS. Обновление остановлено.");
        string job = Path.Combine(UpdatesRoot, "job-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(job);
        progress.Report("Подготовка независимого помощника обновления…");
        string helperRoot = Path.Combine(job, "helper-runtime");
        await Task.Run(() => CopyRuntime(runtime, helperRoot, token), token);
        string request = Path.Combine(job, "request.json");
        using var self = Process.GetCurrentProcess();
        double created = new DateTimeOffset(self.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0;
        await File.WriteAllTextAsync(request, JsonSerializer.Serialize(new { schema = 1, job_id = Path.GetFileName(job), install_root = install,
            installer, sha256 = update.Sha256, size = update.Size, version = update.Version, revision = update.Revision,
            parent_pid = Environment.ProcessId, parent_created = created, automatic }), token);
        var start = new ProcessStartInfo(Path.Combine(helperRoot, "XASS.NativeHelper.exe")) { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = job };
        start.ArgumentList.Add("--role"); start.ArgumentList.Add("native-updater");
        start.ArgumentList.Add("--request"); start.ArgumentList.Add(request);
        using var updater = Process.Start(start) ?? throw new InvalidOperationException("Не удалось запустить помощник обновления.");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token); deadline.CancelAfter(TimeSpan.FromMinutes(15));
        try
        {
            while (!File.Exists(Path.Combine(job, "ready.json")))
            {
                if (updater.HasExited) throw new InvalidOperationException("Подготовка обновления не завершилась. Установленная версия сохранена.");
                string stateFile = Path.Combine(job, "state.json");
                try
                {
                    if (File.Exists(stateFile))
                    {
                        using var state = JsonDocument.Parse(await File.ReadAllTextAsync(stateFile, deadline.Token));
                        progress.Report(state.RootElement.GetProperty("message").GetString() ?? "Подготовка обновления…");
                    }
                }
                catch (IOException) { }
                catch (JsonException) { }
                await Task.Delay(400, deadline.Token);
            }
            using var ready = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(job, "ready.json"), deadline.Token));
            if (!ready.RootElement.GetProperty("ready").GetBoolean() || ready.RootElement.GetProperty("job_id").GetString() != Path.GetFileName(job))
                throw new InvalidOperationException("Помощник обновления не подтвердил резервную копию.");
            return new PreparedNativeUpdate(job);
        }
        catch
        {
            await File.WriteAllTextAsync(Path.Combine(job, "cancel"), "cancel", CancellationToken.None);
            throw;
        }
    }
    private static void CopyRuntime(string source, string destination, CancellationToken token)
    {
        Directory.CreateDirectory(destination);
        foreach (string path in Directory.EnumerateFileSystemEntries(source))
        {
            token.ThrowIfCancellationRequested();
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException("Неожиданная ссылка в среде XASS.");
            string target = Path.Combine(destination, Path.GetFileName(path));
            if ((attributes & FileAttributes.Directory) != 0) CopyRuntime(path, target, token);
            else File.Copy(path, target, false);
        }
    }
    public static async Task WriteHealthAcknowledgmentAsync(string[] args, string revision)
    {
        int index = Array.IndexOf(args, "--native-update-health");
        if (index < 0 || index + 2 >= args.Length) return;
        string job = Path.GetFullPath(args[index + 1]), nonce = args[index + 2];
        if (!System.Text.RegularExpressions.Regex.IsMatch(nonce, "^[a-f0-9]{32}$")
            || !System.Text.RegularExpressions.Regex.IsMatch(Path.GetFileName(job), "^job-[a-f0-9]{32}$")
            || !string.Equals(Path.GetDirectoryName(job), Path.GetFullPath(UpdatesRoot), StringComparison.OrdinalIgnoreCase)) return;
        string target = Path.Combine(job, "health-" + nonce + ".json");
        await File.WriteAllTextAsync(target + ".tmp", JsonSerializer.Serialize(new { nonce, ready = true, revision, pid = Environment.ProcessId }));
        File.Move(target + ".tmp", target, true);
    }
}
