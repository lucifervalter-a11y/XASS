using System.Diagnostics;
using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using Xass.Native.Services;

// All responses use an in-memory handler. No network, installed runtime or user data.
const string revision = "2222222222222222222222222222222222222222";
const string tag = "native-test-timeout-fixture";
const string filename = "XASS-Native-Test-Setup.exe";
const string prefix = "https://github.com/lucifervalter-a11y/XASS/releases/download/" + tag + "/";
byte[] installer = Enumerable.Range(0, 4096).Select(n => (byte)(n % 251)).ToArray();
string hash = Convert.ToHexString(SHA256.HashData(installer)).ToLowerInvariant();
var update = new NativeUpdate("1.2.3", revision, tag, new Uri(prefix + filename), installer.Length, hash);
byte[] manifest = JsonSerializer.SerializeToUtf8Bytes(new
{
    distribution = "native-test", filename, version = update.Version, revision, sha256 = hash, size = installer.Length
});
byte[] releases = JsonSerializer.SerializeToUtf8Bytes(new[]
{
    new { draft = false, prerelease = true, tag_name = tag, assets = new[]
    {
        new { name = "native-test-update.json", browser_download_url = prefix + "native-test-update.json", size = manifest.Length },
        new { name = filename, browser_download_url = prefix + filename, size = installer.Length }
    } }
});
TimeSpan fastIdle = TimeSpan.FromMilliseconds(150), generous = TimeSpan.FromSeconds(5);
string temporaryRoot = Path.Combine(Path.GetTempPath(), "xass-update-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temporaryRoot);
int passed = 0;

async Task Run(string name, Func<Task> test)
{
    var clock = Stopwatch.StartNew();
    await test().WaitAsync(TimeSpan.FromSeconds(10));
    Console.WriteLine($"PASS {name} ({clock.ElapsedMilliseconds} ms)");
    passed++;
}
static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
static async Task<T> Fails<T>(Func<Task> action) where T : Exception
{
    try { await action().WaitAsync(TimeSpan.FromSeconds(5)); }
    catch (T error) { return error; }
    throw new Exception($"Expected {typeof(T).Name}");
}
Fixture Create(TimeSpan? total = null, TimeSpan? idle = null) => new(
    Path.Combine(temporaryRoot, Guid.NewGuid().ToString("N")), total ?? generous, idle ?? fastIdle);
static void NoPartial(Fixture fixture) => Assert(!Directory.EnumerateFiles(fixture.Folder, "*.download").Any(), "Partial download survived");
static void NoInstaller(Fixture fixture) => Assert(!Directory.EnumerateFiles(fixture.Folder, "*.exe").Any(), "Unverified installer was promoted");
static void IdleTimeout(TimeoutException error) => Assert(error.Message.Contains("перестал передавать"), "Not classified as idle timeout");
static void TotalTimeout(TimeoutException error) => Assert(error.Message.Contains("общее время"), "Not classified as total timeout");

try
{
    foreach (bool stallManifest in new[] { false, true })
    {
        await Run($"{(stallManifest ? "manifest" : "release list")} headers then stalled body times out and retries", async () =>
        {
            using var fixture = Create();
            if (stallManifest) fixture.Handler.Enqueue(releases);
            var body = new SyntheticBody(stallManifest ? manifest : releases, stallAfter: stallManifest ? 1 : 0, chunkSize: 1);
            fixture.Handler.Enqueue(body);
            IdleTimeout(await Fails<TimeoutException>(() => fixture.Client.CheckAsync("", default)));
            Assert(body.CancellationObserved && body.Disposed, "Stalled JSON body was not canceled and disposed");
            fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
            NativeUpdate? found = await fixture.Client.CheckAsync("", default);
            Assert(found == update, "The same client could not retry after JSON timeout");
            fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
            Assert(await fixture.Client.CheckAsync(revision, default) is null, "Installed revision was offered again");
        });
    }
    await Run("manifest trickle progress cannot extend the total deadline", async () =>
    {
        using var fixture = Create(TimeSpan.FromSeconds(2), TimeSpan.FromMilliseconds(500));
        fixture.Handler.Enqueue(releases);
        var body = new SyntheticBody(manifest, chunkSize: 1, delay: TimeSpan.FromMilliseconds(25));
        fixture.Handler.Enqueue(body);
        TotalTimeout(await Fails<TimeoutException>(() => fixture.Client.CheckAsync("", default)));
        // The deadline can be observed between reads or inside a pending read.
        // Both must stop before the complete body, dispose it and permit retry.
        Assert(body.BytesRead > 1 && body.BytesRead < manifest.Length && body.Disposed, "Trickling JSON did not make bounded progress");
        fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
        Assert(await fixture.Client.CheckAsync("", default) == update, "Total timeout poisoned retry");
    });
    await Run("release list and manifest share one total check budget", async () =>
    {
        using var fixture = Create(TimeSpan.FromMilliseconds(450), generous);
        foreach (byte[] bytes in new[] { releases, manifest })
            fixture.Handler.Enqueue(async (_, token) =>
            {
                await Task.Delay(TimeSpan.FromMilliseconds(300), token);
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(bytes) };
            });
        TotalTimeout(await Fails<TimeoutException>(() => fixture.Client.CheckAsync("", default)));
        Assert(fixture.Handler.Requests.Count == 2, "Did not exercise cumulative requests");
    });
    await Run("healthy manifest progress renews the idle deadline", async () =>
    {
        using var fixture = Create(idle: TimeSpan.FromMilliseconds(300));
        fixture.Handler.Enqueue(releases);
        var body = new SyntheticBody(manifest, chunkSize: 16, delay: TimeSpan.FromMilliseconds(35));
        fixture.Handler.Enqueue(body);
        var clock = Stopwatch.StartNew();
        Assert(await fixture.Client.CheckAsync("", default) == update, "Healthy manifest failed");
        Assert(clock.Elapsed > TimeSpan.FromMilliseconds(300), "Did not exercise manifest idle renewal");
        Assert(body.Disposed && !body.CancellationObserved, "Healthy manifest was canceled or leaked");
    });
    await Run("installer headers then stalled body cleans partial file and retries", async () =>
    {
        using var fixture = Create();
        var body = new SyntheticBody(installer, stallAfter: 512, chunkSize: 512);
        fixture.Handler.Enqueue(body);
        var progress = new ProgressRecorder();
        IdleTimeout(await Fails<TimeoutException>(() => fixture.Client.DownloadAsync(update, progress, default)));
        Assert(body.CancellationObserved && body.Disposed, "Stalled installer body was not canceled and disposed");
        Assert(progress.Values.Any(p => p > 0), "No partial progress was exercised");
        NoPartial(fixture); NoInstaller(fixture);
        fixture.Handler.Enqueue(installer);
        int nextProgress = progress.Values.Count;
        string target = await fixture.Client.DownloadAsync(update, progress, default);
        Assert(File.ReadAllBytes(target).SequenceEqual(installer), "Retry did not verify and promote correct bytes");
        Assert(progress.Values[nextProgress] == 0 && progress.Values.Last() == 100, "Retry did not reset/finish progress");
        NoPartial(fixture);
    });
    await Run("installer trickle progress is bounded by total deadline", async () =>
    {
        using var fixture = Create(TimeSpan.FromMilliseconds(450), TimeSpan.FromMilliseconds(250));
        var body = new SyntheticBody(installer, chunkSize: 16, delay: TimeSpan.FromMilliseconds(25));
        fixture.Handler.Enqueue(body);
        TotalTimeout(await Fails<TimeoutException>(() => fixture.Client.DownloadAsync(update, new ProgressRecorder(), default)));
        Assert(body.BytesRead > 16 && body.CancellationObserved && body.Disposed, "Trickling installer was not canceled");
        NoPartial(fixture); NoInstaller(fixture);
        fixture.Handler.Enqueue(installer);
        Assert(File.Exists(await fixture.Client.DownloadAsync(update, new ProgressRecorder(), default)), "Total timeout prevented retry");
    });
    foreach (bool download in new[] { false, true })
    {
        await Run($"user cancellation during {(download ? "installer" : "manifest")} body stays cancellation and allows retry", async () =>
        {
            using var fixture = Create(idle: generous);
            using var caller = new CancellationTokenSource();
            if (!download) fixture.Handler.Enqueue(releases);
            var body = new SyntheticBody(download ? installer : manifest, stallAfter: 1, chunkSize: 1);
            fixture.Handler.Enqueue(body);
            Task operation = download
                ? fixture.Client.DownloadAsync(update, new ProgressRecorder(), caller.Token)
                : fixture.Client.CheckAsync("", caller.Token);
            await body.Stalled.Task.WaitAsync(TimeSpan.FromSeconds(2));
            caller.Cancel();
            var error = await Fails<OperationCanceledException>(() => operation);
            Assert(error.CancellationToken == caller.Token, "Caller cancellation identity was lost");
            Assert(body.Disposed && body.CancellationObserved, "Canceled response remained open");
            NoPartial(fixture); NoInstaller(fixture);
            if (download)
            {
                fixture.Handler.Enqueue(installer);
                await fixture.Client.DownloadAsync(update, new ProgressRecorder(), default);
            }
            else
            {
                fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
                Assert(await fixture.Client.CheckAsync("", default) == update, "Canceled manifest poisoned retry");
            }
        });
    }
    await Run("pre-canceled caller performs no HTTP request or file write", async () =>
    {
        using var fixture = Create(); using var caller = new CancellationTokenSource(); caller.Cancel();
        foreach (Func<Task> action in new Func<Task>[] {
            () => fixture.Client.CheckAsync("", caller.Token),
            () => fixture.Client.DownloadAsync(update, new ProgressRecorder(), caller.Token) })
        {
            var error = await Fails<OperationCanceledException>(action);
            Assert(error.CancellationToken == caller.Token, "Pre-canceled token identity was lost");
        }
        Assert(fixture.Handler.Requests.Count == 0, "Pre-canceled call sent a request");
        NoPartial(fixture); NoInstaller(fixture);
    });
    await Run("healthy chunked download outlives idle interval without false timeout", async () =>
    {
        using var fixture = Create(idle: TimeSpan.FromMilliseconds(300));
        var body = new SyntheticBody(installer, chunkSize: 256, delay: TimeSpan.FromMilliseconds(35));
        fixture.Handler.Enqueue(body);
        var clock = Stopwatch.StartNew();
        string path = await fixture.Client.DownloadAsync(update, new ProgressRecorder(), default);
        Assert(clock.Elapsed > TimeSpan.FromMilliseconds(300), "Did not exercise idle deadline renewal");
        Assert(File.ReadAllBytes(path).SequenceEqual(installer), "Healthy streamed bytes changed");
        Assert(body.Disposed && !body.CancellationObserved, "Healthy transfer was canceled or leaked");
        NoPartial(fixture);
    });
    await Run("stalled headers share the bounded transfer deadline", async () =>
    {
        using var fixture = Create();
        fixture.Handler.Enqueue(async (_, token) => { await Task.Delay(Timeout.InfiniteTimeSpan, token); throw new Exception("Unreachable"); });
        IdleTimeout(await Fails<TimeoutException>(() => fixture.Client.CheckAsync("", default)));
        fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
        Assert(await fixture.Client.CheckAsync("", default) == update, "Header timeout prevented retry");
    });
    await Run("size and digest failures clean partial files and preserve prior verified bytes", async () =>
    {
        using var fixture = Create();
        string target = Path.Combine(fixture.Folder, $"XASS-Native-Test-{revision}.exe");
        File.WriteAllText(target, "previous verified fixture");
        foreach (byte[] invalid in new[] { installer[..^1], installer.Concat(new byte[] { 1 }).ToArray(), new byte[installer.Length] })
        {
            fixture.Handler.Enqueue(invalid);
            await Fails<InvalidOperationException>(() => fixture.Client.DownloadAsync(update, new ProgressRecorder(), default));
            NoPartial(fixture);
            Assert(File.ReadAllText(target) == "previous verified fixture", "Failed download replaced the prior installer");
        }
    });
    await Run("redirects keep the HTTPS host and credential restrictions", async () =>
    {
        using var fixture = Create();
        foreach (string destination in new[] { "https://example.org/file", "http://github.com/file", "https://user:secret@github.com/file", "https://github.com:444/file" })
        {
            fixture.Handler.Enqueue((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Redirect)
                { Headers = { Location = new Uri(destination) } }));
            int requests = fixture.Handler.Requests.Count;
            await Fails<InvalidOperationException>(() => fixture.Client.DownloadAsync(update, new ProgressRecorder(), default));
            Assert(fixture.Handler.Requests.Count == requests + 1, "Untrusted redirect reached the transport");
            NoPartial(fixture); NoInstaller(fixture);
        }
        fixture.Handler.Enqueue((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Redirect)
            { Headers = { Location = new Uri("https://release-assets.githubusercontent.com/fixture") } }));
        fixture.Handler.Enqueue(installer);
        await fixture.Client.DownloadAsync(update, new ProgressRecorder(), default);
        Assert(fixture.Handler.Requests.Last().Host == "release-assets.githubusercontent.com", "Trusted redirect failed");
    });
    await Run("HTTP failure disposes response and permits retry", async () =>
    {
        using var fixture = Create();
        var body = new SyntheticBody(Array.Empty<byte>());
        fixture.Handler.Enqueue((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.ServiceUnavailable) { Content = new StreamContent(body) }));
        await Fails<HttpRequestException>(() => fixture.Client.CheckAsync("", default));
        Assert(body.Disposed, "Failed HTTP response leaked its body");
        fixture.Handler.Enqueue(releases); fixture.Handler.Enqueue(manifest);
        Assert(await fixture.Client.CheckAsync("", default) == update, "HTTP failure poisoned retry");
    });
    Console.WriteLine($"{passed} native update timeout, cancellation, integrity and retry tests passed. No network used.");
}
finally { Directory.Delete(temporaryRoot, recursive: true); }

sealed class Fixture : IDisposable
{
    public string Folder { get; }
    public FakeHttpHandler Handler { get; } = new();
    public NativeUpdateClient Client { get; }
    private readonly HttpClient http;
    public Fixture(string folder, TimeSpan total, TimeSpan idle)
    {
        Folder = folder; Directory.CreateDirectory(folder);
        http = new HttpClient(Handler) { Timeout = Timeout.InfiniteTimeSpan };
        Client = new NativeUpdateClient(http, total, total, idle, folder);
    }
    public void Dispose() => http.Dispose();
}
sealed class ProgressRecorder : IProgress<double>
{
    public List<double> Values { get; } = new();
    public void Report(double value) => Values.Add(value);
}
sealed class FakeHttpHandler : HttpMessageHandler
{
    private readonly Queue<Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>>> responses = new();
    public List<Uri> Requests { get; } = new();
    public void Enqueue(byte[] bytes) => Enqueue(new SyntheticBody(bytes));
    public void Enqueue(SyntheticBody body) => Enqueue((_, _) => Task.FromResult(
        new HttpResponseMessage(HttpStatusCode.OK) { Content = new StreamContent(body) }));
    public void Enqueue(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> response) => responses.Enqueue(response);
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        if (request.Headers.Authorization is not null) throw new Exception("Updater sent credentials");
        if (request.RequestUri is null) throw new Exception("Missing request URI");
        Requests.Add(request.RequestUri);
        if (responses.Count == 0) throw new Exception("Unexpected HTTP request");
        return responses.Dequeue()(request, cancellationToken);
    }
}
sealed class SyntheticBody(byte[] bytes, int? stallAfter = null, int chunkSize = int.MaxValue, TimeSpan delay = default) : Stream
{
    public TaskCompletionSource Stalled { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    public bool Disposed { get; private set; }
    public bool CancellationObserved { get; private set; }
    public int BytesRead { get; private set; }
    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        try
        {
            if (stallAfter is not null && BytesRead >= stallAfter.Value)
            {
                Stalled.TrySetResult();
                await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            }
            if (BytesRead == bytes.Length) return 0;
            if (delay != TimeSpan.Zero) await Task.Delay(delay, cancellationToken);
            int count = Math.Min(buffer.Length, Math.Min(chunkSize, bytes.Length - BytesRead));
            bytes.AsMemory(BytesRead, count).CopyTo(buffer); BytesRead += count;
            return count;
        }
        catch (OperationCanceledException) { CancellationObserved = true; throw; }
    }
    public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) => ReadAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();
    protected override void Dispose(bool disposing) { Disposed = true; base.Dispose(disposing); }
    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override void Flush() => throw new NotSupportedException();
}
