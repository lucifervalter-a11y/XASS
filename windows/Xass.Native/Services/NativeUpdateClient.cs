using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Xass.Native.Services;

public sealed record NativeUpdate(string Version, string Revision, string Tag, Uri Download, long Size, string Sha256);
public sealed class NativeUpdateClient
{
    private const string Repo = "lucifervalter-a11y/XASS";
    private const string InstallerName = "XASS-Native-Test-Setup.exe";
    private static readonly HashSet<string> Hosts = new(StringComparer.OrdinalIgnoreCase)
    { "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com" };
    private readonly HttpClient http;
    private readonly TimeSpan checkTimeout, downloadTimeout, idleTimeout;
    private readonly string updatesFolder;

    public NativeUpdateClient() : this(
        new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = Timeout.InfiniteTimeSpan },
        TimeSpan.FromMinutes(2), TimeSpan.FromMinutes(15), TimeSpan.FromSeconds(30),
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "updates")) { }

    // The transport and deadlines can be replaced by deterministic, offline tests.
    // Production keeps the same strict HTTPS/repository/redirect checks below.
    internal NativeUpdateClient(HttpClient http, TimeSpan checkTimeout, TimeSpan downloadTimeout,
        TimeSpan idleTimeout, string updatesFolder)
    {
        this.http = http;
        this.checkTimeout = checkTimeout;
        this.downloadTimeout = downloadTimeout;
        this.idleTimeout = idleTimeout;
        this.updatesFolder = updatesFolder;
        http.DefaultRequestHeaders.UserAgent.ParseAdd("XASS-Native-Test/1");
    }

    private sealed class TransferDeadline : IDisposable
    {
        private readonly CancellationTokenSource total, idle;
        private readonly TimeSpan idleTimeout;
        public CancellationToken Token => idle.Token;
        public bool TotalExpired => total.IsCancellationRequested;
        public TransferDeadline(CancellationToken caller, TimeSpan totalTimeout, TimeSpan idleTimeout)
        {
            this.idleTimeout = idleTimeout;
            total = CancellationTokenSource.CreateLinkedTokenSource(caller);
            total.CancelAfter(totalTimeout);
            idle = CancellationTokenSource.CreateLinkedTokenSource(total.Token);
            idle.CancelAfter(idleTimeout);
        }
        public void RestartIdle()
        {
            Token.ThrowIfCancellationRequested();
            idle.CancelAfter(idleTimeout);
        }
        public void Dispose() { idle.Dispose(); total.Dispose(); }
    }

    private static TimeoutException TimedOut(TransferDeadline deadline, OperationCanceledException error) => new(
        deadline.TotalExpired
            ? "Превышено общее время получения обновления. Проверьте сеть и повторите попытку."
            : "Сервер обновлений перестал передавать данные. Проверьте сеть и повторите попытку.", error);

    private async Task<HttpResponseMessage> GetAsync(Uri url, TransferDeadline deadline)
    {
        for (int redirects = 0; redirects <= 5; redirects++)
        {
            if (url.Scheme != "https" || !Hosts.Contains(url.Host) || !string.IsNullOrEmpty(url.UserInfo) || !url.IsDefaultPort)
                throw new InvalidOperationException("Недоверенный адрес обновления.");
            deadline.RestartIdle();
            var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false);
            if ((int)response.StatusCode is >= 300 and < 400)
            {
                var location = response.Headers.Location; response.Dispose();
                if (location is null) throw new InvalidOperationException("Некорректный адрес загрузки.");
                url = location.IsAbsoluteUri ? location : new Uri(url, location); continue;
            }
            try { response.EnsureSuccessStatusCode(); return response; }
            catch { response.Dispose(); throw; }
        }
        throw new InvalidOperationException("Слишком много перенаправлений обновления.");
    }
    private async Task<JsonDocument> JsonAsync(Uri uri, int maximum, TransferDeadline deadline)
    {
        using var response = await GetAsync(uri, deadline).ConfigureAwait(false);
        deadline.RestartIdle();
        await using var stream = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
        using var memory = new MemoryStream(); var buffer = new byte[8192];
        while (true)
        {
            deadline.RestartIdle();
            int read = await stream.ReadAsync(buffer, deadline.Token).ConfigureAwait(false);
            if (read == 0) break;
            if (memory.Length + read > maximum) throw new InvalidOperationException("Ответ сервера слишком большой.");
            await memory.WriteAsync(buffer.AsMemory(0, read), deadline.Token).ConfigureAwait(false);
        }
        deadline.Token.ThrowIfCancellationRequested();
        return JsonDocument.Parse(memory.ToArray());
    }
    public async Task<NativeUpdate?> CheckAsync(string installedRevision, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        using var deadline = new TransferDeadline(token, checkTimeout, idleTimeout);
        try { return await CheckCoreAsync(installedRevision, deadline).ConfigureAwait(false); }
        catch (OperationCanceledException error)
        {
            token.ThrowIfCancellationRequested();
            throw TimedOut(deadline, error);
        }
    }
    private async Task<NativeUpdate?> CheckCoreAsync(string installedRevision, TransferDeadline deadline)
    {
        using var releases = await JsonAsync(new Uri($"https://api.github.com/repos/{Repo}/releases?per_page=30"), 2 * 1024 * 1024, deadline).ConfigureAwait(false);
        foreach (var release in releases.RootElement.EnumerateArray())
        {
            if (release.GetProperty("draft").GetBoolean() || !release.GetProperty("prerelease").GetBoolean()) continue;
            string tag = release.GetProperty("tag_name").GetString() ?? "";
            if (!tag.StartsWith("native-test-", StringComparison.Ordinal)) continue;
            var assets = release.GetProperty("assets").EnumerateArray().ToArray();
            var manifestAsset = assets.FirstOrDefault(a => a.GetProperty("name").GetString() == "native-test-update.json");
            var installerAsset = assets.FirstOrDefault(a => a.GetProperty("name").GetString() == InstallerName);
            if (manifestAsset.ValueKind == JsonValueKind.Undefined || installerAsset.ValueKind == JsonValueKind.Undefined) continue;
            var manifestUri = new Uri(manifestAsset.GetProperty("browser_download_url").GetString()!);
            string prefix = $"https://github.com/{Repo}/releases/download/{Uri.EscapeDataString(tag)}/";
            if (manifestUri.AbsoluteUri != prefix + "native-test-update.json") throw new InvalidOperationException("Некорректный манифест выпуска.");
            using var manifest = await JsonAsync(manifestUri, 65536, deadline).ConfigureAwait(false);
            var m = manifest.RootElement;
            if (m.GetProperty("distribution").GetString() != "native-test" || m.GetProperty("filename").GetString() != InstallerName)
                throw new InvalidOperationException("Этот пакет не предназначен для нативного тестового клиента.");
            string version = m.GetProperty("version").GetString() ?? "", revision = m.GetProperty("revision").GetString() ?? "";
            string sha = m.GetProperty("sha256").GetString() ?? "";
            long size = m.GetProperty("size").GetInt64();
            var uri = new Uri(installerAsset.GetProperty("browser_download_url").GetString()!);
            if (uri.AbsoluteUri != prefix + InstallerName || !Regex.IsMatch(sha, "^[a-fA-F0-9]{64}$")
                || !Regex.IsMatch(revision, "^[a-fA-F0-9]{40}$") || !Regex.IsMatch(version, "^[0-9A-Za-z.+-]{1,64}$")
                || size < 1024 || size > 8L * 1024 * 1024 * 1024 || installerAsset.GetProperty("size").GetInt64() != size)
                throw new InvalidOperationException("Манифест не прошёл проверку размера, версии или контрольной суммы.");
            if (string.Equals(installedRevision, revision, StringComparison.OrdinalIgnoreCase)) return null;
            return new NativeUpdate(version, revision, tag, uri, size, sha.ToLowerInvariant());
        }
        return null;
    }
    public async Task<string> DownloadAsync(NativeUpdate update, IProgress<double> progress, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        using var deadline = new TransferDeadline(token, downloadTimeout, idleTimeout);
        Directory.CreateDirectory(updatesFolder);
        string final = Path.Combine(updatesFolder, $"XASS-Native-Test-{update.Revision}.exe"), temporary = final + ".download";
        try
        {
            using var response = await GetAsync(update.Download, deadline).ConfigureAwait(false);
            deadline.RestartIdle();
            await using var input = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            await using (var output = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None, 1024 * 1024, true))
            {
                long count = 0; var buffer = new byte[1024 * 1024];
                progress.Report(0);
                while (true)
                {
                    deadline.RestartIdle();
                    int read = await input.ReadAsync(buffer, deadline.Token).ConfigureAwait(false);
                    if (read == 0) break;
                    count += read; if (count > update.Size) throw new InvalidOperationException("Размер установщика не совпал.");
                    hash.AppendData(buffer.AsSpan(0, read));
                    await output.WriteAsync(buffer.AsMemory(0, read), deadline.Token).ConfigureAwait(false);
                    progress.Report((double)count * 100 / update.Size);
                }
                await output.FlushAsync(deadline.Token).ConfigureAwait(false);
                if (count != update.Size || !Convert.ToHexString(hash.GetHashAndReset()).Equals(update.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Установщик не прошёл проверку SHA-256.");
            }
            deadline.Token.ThrowIfCancellationRequested();
            File.Move(temporary, final, true); return final;
        }
        catch (OperationCanceledException error)
        {
            token.ThrowIfCancellationRequested();
            throw TimedOut(deadline, error);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
