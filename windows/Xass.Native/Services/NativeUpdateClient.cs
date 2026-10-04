using System.Net;
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
    private readonly HttpClient http = new(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromMinutes(15) };
    public NativeUpdateClient() => http.DefaultRequestHeaders.UserAgent.ParseAdd("XASS-Native-Test/1");
    private async Task<HttpResponseMessage> GetAsync(Uri url, CancellationToken token)
    {
        for (int redirects = 0; redirects <= 5; redirects++)
        {
            if (url.Scheme != "https" || !Hosts.Contains(url.Host) || !string.IsNullOrEmpty(url.UserInfo) || !url.IsDefaultPort)
                throw new InvalidOperationException("Недоверенный адрес обновления.");
            var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, token);
            if ((int)response.StatusCode is >= 300 and < 400)
            {
                var location = response.Headers.Location; response.Dispose();
                if (location is null) throw new InvalidOperationException("Некорректный адрес загрузки.");
                url = location.IsAbsoluteUri ? location : new Uri(url, location); continue;
            }
            response.EnsureSuccessStatusCode(); return response;
        }
        throw new InvalidOperationException("Слишком много перенаправлений обновления.");
    }
    private async Task<JsonDocument> JsonAsync(Uri uri, int maximum, CancellationToken token)
    {
        using var response = await GetAsync(uri, token);
        await using var stream = await response.Content.ReadAsStreamAsync(token);
        using var memory = new MemoryStream(); var buffer = new byte[8192]; int read;
        while ((read = await stream.ReadAsync(buffer, token)) > 0)
        { if (memory.Length + read > maximum) throw new InvalidOperationException("Ответ сервера слишком большой."); await memory.WriteAsync(buffer.AsMemory(0, read), token); }
        return JsonDocument.Parse(memory.ToArray());
    }
    public async Task<NativeUpdate?> CheckAsync(string installedRevision, CancellationToken token)
    {
        using var releases = await JsonAsync(new Uri($"https://api.github.com/repos/{Repo}/releases?per_page=30"), 2 * 1024 * 1024, token);
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
            using var manifest = await JsonAsync(manifestUri, 65536, token);
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
        string folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "updates");
        Directory.CreateDirectory(folder);
        string final = Path.Combine(folder, $"XASS-Native-Test-{update.Revision}.exe"), temporary = final + ".download";
        try
        {
            using var response = await GetAsync(update.Download, token);
            await using var input = await response.Content.ReadAsStreamAsync(token);
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            await using (var output = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None, 1024 * 1024, true))
            {
                long count = 0; var buffer = new byte[1024 * 1024]; int read;
                while ((read = await input.ReadAsync(buffer, token)) > 0)
                {
                    count += read; if (count > update.Size) throw new InvalidOperationException("Размер установщика не совпал.");
                    hash.AppendData(buffer.AsSpan(0, read)); await output.WriteAsync(buffer.AsMemory(0, read), token);
                    progress.Report((double)count * 100 / update.Size);
                }
                await output.FlushAsync(token);
                if (count != update.Size || !Convert.ToHexString(hash.GetHashAndReset()).Equals(update.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Установщик не прошёл проверку SHA-256.");
            }
            File.Move(temporary, final, true); return final;
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
