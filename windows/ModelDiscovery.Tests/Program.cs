using Xass.Native.Services;

int passed = 0;
string suite = Path.Combine(Path.GetTempPath(), "xass-model-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(suite);
try
{
    Test("environment cache roots, precedence and deduplication", folder =>
    {
        var env = new Dictionary<string, string?> {
            ["HF_HUB_CACHE"] = Path.Combine(folder, "custom"),
            ["HUGGINGFACE_HUB_CACHE"] = Path.Combine(folder, "custom"),
            ["HF_HOME"] = Path.Combine(folder, "home"),
            ["XDG_CACHE_HOME"] = Path.Combine(folder, "xdg") };
        var roots = WhisperModelDiscovery.GetCacheRoots(key => env.GetValueOrDefault(key), folder);
        Require(roots.Count == 6 && roots[0].Source == "HF_HUB_CACHE", "precedence/deduplication");
        Require(roots[1].Path == Path.Combine(folder, "home", "hub"), "HF_HOME/hub");
        Require(roots[2].Path == Path.Combine(folder, "xdg", "huggingface", "hub"), "XDG root");
        Require(roots[3].Path == Path.Combine(folder, ".cache", "huggingface", "hub"), "default root");
    });
    Test("XASS private transcription roots", folder =>
    {
        string data = Path.Combine(folder, "custom-data"), local = Path.Combine(folder, "Local");
        var roots = WhisperModelDiscovery.GetCacheRoots(key => key switch
        {
            "XASS_DATA_ROOT" => data, "LOCALAPPDATA" => local, _ => null
        }, folder);
        string expected = Path.Combine(data, "transcription", "models", "whisper");
        string snapshot = Model(expected);
        Require(roots.Any(root => root.Path == expected), "custom data root");
        Require(roots.Any(root => root.Path == Path.Combine(local, "XASS", "transcription", "models", "hf", "hub")), "private HF root");
        Require(WhisperModelDiscovery.FindExisting(roots)?.Path == snapshot, "existing app cache");
    });
    Test("unsafe and relative roots rejected without probing", folder =>
    {
        foreach (string value in new[] { "relative", "\\\\server\\share", "//server/share", "\0invalid" })
        {
            var roots = WhisperModelDiscovery.GetCacheRoots(_ => value, "");
            Require(roots.Count == 0, "rejected " + value);
        }
        var tilde = WhisperModelDiscovery.GetCacheRoots(key => key == "HF_HOME" ? "~/.hf" : null, folder);
        Require(tilde[0].Path == Path.Combine(folder, ".hf", "hub"), "tilde expansion");
    });
    Test("missing and non-directory roots are harmless", folder =>
    {
        File.WriteAllText(Path.Combine(folder, "file"), "x");
        Require(Find(Path.Combine(folder, "absent")) is null && Find(Path.Combine(folder, "file")) is null, "missing roots");
    });
    Test("complete small snapshot returns actual absolute folder", folder =>
    {
        string snapshot = Model(folder);
        var result = Find(folder);
        Require(result?.Path == snapshot && Path.IsPathFullyQualified(result.Path), "snapshot path");
        Require(result?.Repository == "models--Systran--faster-whisper-small", "repository identity");
    });
    foreach (string required in new[] { "model.bin", "config.json", "tokenizer.json" })
    {
        Test("reject missing " + required, folder =>
        {
            string snapshot = Model(folder); File.Delete(Path.Combine(snapshot, required));
            Require(Find(folder) is null, "incomplete folder");
        });
        Test("reject empty " + required, folder =>
        {
            string snapshot = Model(folder); File.WriteAllText(Path.Combine(snapshot, required), "");
            Require(Find(folder) is null, "empty file");
        });
    }
    Test("invalid and oversized config rejected", folder =>
    {
        string snapshot = Model(folder);
        foreach (string config in new[] { "broken", "[]", "{}", "{\"lang_ids\":[1]}", new string(' ', 1024 * 1024 + 1) })
        {
            File.WriteAllText(Path.Combine(snapshot, "config.json"), config);
            Require(Find(folder) is null, "invalid config");
        }
    });
    Test("English-only repository ignored", folder =>
    {
        Model(folder, "models--Systran--faster-whisper-small.en");
        Require(Find(folder) is null, "English-only name");
    });
    Test("small preferred across cache roots", folder =>
    {
        string first = Path.Combine(folder, "first"), second = Path.Combine(folder, "second");
        Model(first, "models--Systran--faster-whisper-base");
        string small = Model(second);
        Require(WhisperModelDiscovery.FindExisting(new[] { new WhisperCacheRoot(first, "first"),
            new WhisperCacheRoot(second, "second") })?.Path == small, "small priority");
    });
    Test("cache order wins equal-model ties", folder =>
    {
        string first = Path.Combine(folder, "first"), second = Path.Combine(folder, "second");
        string expected = Model(first); Model(second);
        Require(WhisperModelDiscovery.FindExisting(new[] { new WhisperCacheRoot(first, "first"),
            new WhisperCacheRoot(second, "second") })?.Path == expected, "cache priority");
    });
    Test("refs/main preferred over newer snapshot", folder =>
    {
        string main = Model(folder, revision: new string('a', 40));
        string newest = Model(folder, revision: new string('b', 40));
        Directory.SetLastWriteTimeUtc(main, DateTime.UtcNow.AddDays(-5));
        Directory.SetLastWriteTimeUtc(newest, DateTime.UtcNow);
        Ref(folder, new string('a', 40));
        Require(Find(folder)?.Path == main, "main revision");
    });
    Test("invalid or incomplete refs/main falls back locally", folder =>
    {
        string incomplete = Model(folder, revision: new string('a', 40));
        File.Delete(Path.Combine(incomplete, "tokenizer.json"));
        string valid = Model(folder, revision: new string('b', 40));
        Ref(folder, new string('a', 40));
        Require(Find(folder)?.Path == valid, "incomplete main fallback");
        Ref(folder, "../../outside");
        Require(Find(folder)?.Path == valid, "untrusted ref path rejected");
    });
    Test("no recursive, non-model or loose-folder scan", folder =>
    {
        Model(Path.Combine(folder, "nested"));
        Model(folder, "datasets--Systran--faster-whisper-small");
        Model(folder, "models--unrelated--classifier");
        foreach (string name in new[] { "model.bin", "tokenizer.json", "config.json" }) File.WriteAllText(Path.Combine(folder, name), "x");
        Require(Find(folder) is null, "no recursive discovery");
    });
    Test("bounded root count", folder =>
    {
        var roots = Enumerable.Range(0, WhisperModelDiscovery.MaximumCacheRoots + 1)
            .Select(index => new WhisperCacheRoot(Path.Combine(folder, index.ToString()), "test")).ToArray();
        Model(roots[^1].Path);
        Require(WhisperModelDiscovery.FindExisting(roots) is null, "root limit");
    });
    Test("bounded snapshot count", folder =>
    {
        string repository = Path.Combine(folder, "models--Systran--faster-whisper-small");
        string snapshots = Path.Combine(repository, "snapshots");
        for (int i = 0; i <= WhisperModelDiscovery.MaximumSnapshotsPerRepository; i++)
            Directory.CreateDirectory(Path.Combine(snapshots, i.ToString("x40")));
        var visited = Directory.EnumerateDirectories(snapshots).Take(WhisperModelDiscovery.MaximumSnapshotsPerRepository).ToHashSet();
        string outside = Directory.EnumerateDirectories(snapshots).Single(path => !visited.Contains(path));
        Model(folder, revision: Path.GetFileName(outside));
        Require(Find(folder) is null, "snapshot limit");
        Ref(folder, Path.GetFileName(outside));
        Require(Find(folder)?.Path == outside, "refs/main is bounded direct probe");
    });
    Test("cancellation exits immediately", folder =>
    {
        Model(folder);
        using var cancellation = new CancellationTokenSource(); cancellation.Cancel();
        bool threw = false;
        try { WhisperModelDiscovery.FindExisting(new[] { new WhisperCacheRoot(folder, "test") }, cancellation.Token); }
        catch (OperationCanceledException) { threw = true; }
        Require(threw, "cancellation");
    });
    // Linux CI permits symlinks. Windows can require Developer Mode/admin, so enable there explicitly.
    if (!OperatingSystem.IsWindows() || Environment.GetEnvironmentVariable("XASS_TEST_SYMLINKS") == "1")
    {
        Test("standard Hugging Face blob symlinks accepted", folder =>
        {
            string snapshot = Model(folder);
            string repository = Directory.GetParent(snapshot)!.Parent!.FullName;
            string blobs = Path.Combine(repository, "blobs"); Directory.CreateDirectory(blobs);
            foreach (string filename in new[] { "model.bin", "config.json", "tokenizer.json" })
            {
                File.Move(Path.Combine(snapshot, filename), Path.Combine(blobs, filename));
                File.CreateSymbolicLink(Path.Combine(snapshot, filename), Path.Combine("..", "..", "blobs", filename));
            }
            Require(Find(folder)?.Path == snapshot, "HF links");
        });
        Test("dangling and outside-repository file links rejected", folder =>
        {
            string snapshot = Model(folder), binary = Path.Combine(snapshot, "model.bin");
            File.Delete(binary); File.CreateSymbolicLink(binary, "missing");
            Require(Find(folder) is null, "dangling link");
            File.Delete(binary); string outside = Path.Combine(folder, "outside.bin"); File.WriteAllText(outside, "binary");
            File.CreateSymbolicLink(binary, outside);
            Require(Find(folder) is null, "outside blob link");
        });
        Test("directory links are not traversed", folder =>
        {
            string actual = Path.Combine(folder, "actual"), cache = Path.Combine(folder, "cache");
            Model(actual); Directory.CreateDirectory(cache);
            Directory.CreateSymbolicLink(Path.Combine(cache, "models--Systran--faster-whisper-small"),
                Path.Combine(actual, "models--Systran--faster-whisper-small"));
            Require(Find(cache) is null, "repository link");
            Directory.CreateSymbolicLink(Path.Combine(folder, "linked-cache"), actual);
            Require(Find(Path.Combine(folder, "linked-cache")) is null, "cache link");
        });
    }
    Console.WriteLine($"Model discovery tests passed: {passed} cases; no downloads or model inference.");
}
finally { Directory.Delete(suite, recursive: true); }

void Test(string label, Action<string> test)
{
    string folder = Path.Combine(suite, passed.ToString()); Directory.CreateDirectory(folder);
    test(folder); passed++; Console.WriteLine("PASS " + label);
}
static void Require(bool condition, string label)
{
    if (!condition) throw new InvalidOperationException(label);
}
static DiscoveredWhisperModel? Find(string root) => WhisperModelDiscovery.FindExisting(new[] { new WhisperCacheRoot(root, "test") });
static string Model(string root, string repository = "models--Systran--faster-whisper-small", string? revision = null)
{
    string folder = Path.Combine(root, repository, "snapshots", revision ?? new string('a', 40));
    Directory.CreateDirectory(folder);
    File.WriteAllText(Path.Combine(folder, "model.bin"), "fixture, not real model weights");
    File.WriteAllText(Path.Combine(folder, "tokenizer.json"), "{\"version\":\"1.0\"}");
    File.WriteAllText(Path.Combine(folder, "config.json"), "{\"lang_ids\":[50259,50260,50263]}");
    return folder;
}
static void Ref(string root, string revision)
{
    string refs = Path.Combine(root, "models--Systran--faster-whisper-small", "refs");
    Directory.CreateDirectory(refs); File.WriteAllText(Path.Combine(refs, "main"), revision);
}
