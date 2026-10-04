using System.Text;
using System.Text.Json;
using Xass.Native.Services;

internal static class Program
{
    private static int passed;
    private static readonly JsonElement Preview = JsonSerializer.SerializeToElement(new { server = "https://import.invalid", name = "Import PC", expires_at = "2099-01-01" });
    private static readonly JsonElement Paired = JsonSerializer.SerializeToElement(new { paired = true, server = "https://manual.invalid", name = "Manual PC" });
    private static string Profile(string code) => JsonSerializer.Serialize(new { format = "xass-connect", version = 1, server_url = "https://import.invalid", source_name = "Import PC", pair_code = code, expires_at = "2099-01-01T00:00:00Z" });
    private static void Check(bool condition, string name)
    { if (!condition) throw new Exception("FAILED: " + name); passed++; Console.WriteLine("PASS: " + name); }
    private static async Task Throws<T>(Func<Task> run, string name) where T : Exception
    {
        try { await run(); }
        catch (T) { Check(true, name); return; }
        throw new Exception("FAILED: " + name);
    }
    private static Task<bool> Confirm(JsonElement _, CancellationToken __) => Task.FromResult(true);
    private static Task<string?> Source(string? content) => Task.FromResult(content);
    private sealed class Peer
    {
        public readonly List<JsonElement> Requests = new();
        public string? FailAction;
        public Task<JsonElement> Send(object request, CancellationToken cancellation)
        {
            cancellation.ThrowIfCancellationRequested();
            JsonElement payload = JsonSerializer.SerializeToElement(request);
            Requests.Add(payload);
            string action = payload.GetProperty("action").GetString()!;
            if (action == FailAction) throw new IOException("Synthetic service failure");
            return Task.FromResult(action == "desktop_profile" ? Preview : Paired);
        }
    }
    private static async Task AssertManual(DesktopPairingFlow flow, Peer peer, string name)
    {
        var result = await flow.PairManualAsync(" https://manual.invalid ", " Manual PC ", "fresh-manual", peer.Send, default);
        JsonElement sent = peer.Requests[^1];
        Check(result is not null && sent.GetProperty("action").GetString() == "desktop_pair"
            && sent.GetProperty("server").GetString() == "https://manual.invalid"
            && sent.GetProperty("name").GetString() == "Manual PC"
            && sent.GetProperty("code").GetString() == "fresh-manual"
            && sent.EnumerateObject().Count() == 4 && !flow.IsBusy, name);
    }
    public static async Task<int> Main()
    {
        string root = Path.Combine(Path.GetTempPath(), "xass-pairing-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var flow = new DesktopPairingFlow(); var peer = new Peer();
            await AssertManual(flow, peer, "manual sends only visible input, with no profile source");
            foreach (string dismissal in new[] { "Cancel", "Close", "Escape" })
            {
                peer.Requests.Clear();
                var result = await flow.ImportAsync(_ => Source(Profile("discarded-import")), peer.Send,
                    (_, _) => Task.FromResult(false), default);
                Check(result is null && peer.Requests.Count == 1 && !flow.IsBusy, dismissal + " never pairs and releases import");
                await AssertManual(flow, peer, dismissal + " then manual uses fresh visible input");
            }
            peer.Requests.Clear();
            Check(await flow.ImportAsync(_ => Source(null), peer.Send, Confirm, default) is null
                && peer.Requests.Count == 0 && !flow.IsBusy, "cancelled picker performs no preview or pairing");
            await AssertManual(flow, peer, "picker cancellation leaves manual mode available");

            foreach (string failure in new[] { "read", "desktop_profile", "confirm", "desktop_pair_profile" })
            {
                peer.Requests.Clear(); peer.FailAction = failure;
                await Throws<IOException>(() => flow.ImportAsync(
                    _ => failure == "read" ? throw new IOException("Synthetic read failure") : Source(Profile("failed-import")),
                    peer.Send, (_, _) => failure == "confirm" ? throw new IOException("Synthetic dialog failure") : Task.FromResult(true), default),
                    failure + " propagates without retaining input");
                Check(!flow.IsBusy, failure + " releases operation gate");
                peer.FailAction = null;
                await AssertManual(flow, peer, failure + " then manual sends visible input");
                string fresh = Profile("fresh-" + failure);
                await flow.ImportAsync(_ => Source(fresh), peer.Send, Confirm, default);
                Check(peer.Requests[^1].GetProperty("text").GetString() == fresh
                    && peer.Requests[^1].GetProperty("confirmed").GetBoolean(), failure + " then JSON retry uses new confirmed profile");
            }
            peer.Requests.Clear(); peer.FailAction = "desktop_pair";
            await Throws<IOException>(() => flow.PairManualAsync("https://manual.invalid", "PC", "first-manual", peer.Send, default), "manual failure releases gate");
            peer.FailAction = null;
            await AssertManual(flow, peer, "manual retry uses newly entered code");

            string file = Path.Combine(root, "fixture.xass");
            string original = Profile("original-file"); await File.WriteAllTextAsync(file, original, new UTF8Encoding(true));
            peer.Requests.Clear();
            await flow.ImportAsync(async token => await DesktopPairingFlow.ReadProfileFileAsync(file, token), peer.Send,
                async (_, _) => { await File.WriteAllTextAsync(file, Profile("changed-on-disk")); return true; }, default);
            Check(peer.Requests.Count == 2 && peer.Requests.All(r => r.GetProperty("text").GetString() == original)
                && !peer.Requests[^1].TryGetProperty("path", out _), "file import pairs exact reviewed snapshot despite disk change");
            peer.Requests.Clear();
            await flow.ImportAsync(async token => await DesktopPairingFlow.ReadProfileFileAsync(file, token), peer.Send, Confirm, default);
            Check(peer.Requests[^1].GetProperty("text").GetString() == Profile("changed-on-disk"), "fresh file import reads new file content");
            foreach (string extension in new[] { ".xass", ".xass-connect", ".JSON" })
            {
                string selected = Path.Combine(root, "fixture" + extension);
                await File.WriteAllTextAsync(selected, original);
                Check(await DesktopPairingFlow.ReadProfileFileAsync(selected, default) == original, extension + " file format is accepted");
            }
            await Throws<InvalidOperationException>(() => DesktopPairingFlow.ReadProfileFileAsync("relative.xass", default), "relative import path rejected");
            await Throws<InvalidOperationException>(() => DesktopPairingFlow.ReadProfileFileAsync(Path.Combine(root, "fixture.exe"), default), "non-profile extension rejected");
            await Throws<FileNotFoundException>(() => DesktopPairingFlow.ReadProfileFileAsync(Path.Combine(root, "missing.xass"), default), "missing file is retryable error");
            await File.WriteAllBytesAsync(file, new byte[] { 0xff, 0xff });
            await Throws<DecoderFallbackException>(() => DesktopPairingFlow.ReadProfileFileAsync(file, default), "invalid UTF-8 file rejected");
            await File.WriteAllTextAsync(file, new string('x', 65537));
            await Throws<InvalidOperationException>(() => DesktopPairingFlow.ReadProfileFileAsync(file, default), "oversized file bounded and rejected");
            peer.Requests.Clear();
            await Throws<InvalidOperationException>(() => flow.ImportAsync(_ => Source(new string('Я', 32769)), peer.Send, Confirm, default), "clipboard UTF-8 byte limit enforced");
            Check(peer.Requests.Count == 0 && !flow.IsBusy, "oversized clipboard never reaches service");

            foreach (string phase in new[] { "before", "read", "preview", "confirm" })
            {
                using var cancel = new CancellationTokenSource(); peer.Requests.Clear();
                if (phase == "before") cancel.Cancel();
                await Throws<OperationCanceledException>(() => flow.ImportAsync(
                    _ => { if (phase == "read") cancel.Cancel(); return Source(original); },
                    async (request, token) => { var value = await peer.Send(request, token); if (phase == "preview") cancel.Cancel(); return value; },
                    (_, _) => { if (phase == "confirm") cancel.Cancel(); return Task.FromResult(true); }, cancel.Token),
                    phase + " cancellation stops import");
                Check(!flow.IsBusy && !peer.Requests.Any(r => r.GetProperty("action").GetString() == "desktop_pair_profile"), phase + " cancellation prevents pairing");
                await AssertManual(flow, peer, phase + " cancellation allows new manual attempt");
            }
            // System.Text.Json escapes more than the UTF-8 profile itself. This is
            // the real serializer used by AgentClient, not a regex/source assertion.
            string unicode = Profile("unicode-fixture")[..^1] + ",\"padding\":\"" + new string('Я', 32000) + "\"}";
            peer.Requests.Clear();
            await flow.ImportAsync(_ => Source(unicode), peer.Send, Confirm, default);
            string envelope = peer.Requests[^1].GetRawText();
            Check(Encoding.UTF8.GetByteCount(unicode) <= 65536 && envelope.Length > 96 * 1024 && envelope.Length < 512 * 1024,
                "real C# Unicode serialization fits bounded bridge envelope");

            var picked = new TaskCompletionSource<string?>(TaskCreationOptions.RunContinuationsAsynchronously);
            peer.Requests.Clear();
            Task<JsonElement?> first = flow.ImportAsync(_ => picked.Task, peer.Send, Confirm, default);
            Check(flow.IsBusy, "gate covers source selection before preview");
            Check(await flow.PairManualAsync("https://other.invalid", "Other PC", "other-code", peer.Send, default) is null,
                "manual click cannot interleave pending import");
            Check(await flow.ImportAsync(_ => throw new Exception("Second source must not be read"), peer.Send, Confirm, default) is null,
                "repeated import cannot replace pending source");
            picked.SetResult(original); await first;
            Check(peer.Requests.Count == 2 && !flow.IsBusy, "single import completes once and gate reopens");

            var decision = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            peer.Requests.Clear();
            first = flow.ImportAsync(_ => Source(original), peer.Send, (_, _) => decision.Task, default);
            Check(flow.IsBusy && await flow.PairManualAsync("https://other.invalid", "PC", "other", peer.Send, default) is null,
                "manual click cannot bypass pending confirmation");
            decision.SetResult(false); await first;
            await AssertManual(flow, peer, "dismissed confirmation reopens clean manual attempt");

            Console.WriteLine($"Pairing flow: {passed} executable checks passed. No live server, credentials or PC actions.");
            return 0;
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
