using System.Collections.Concurrent;
using System.Diagnostics;
using Xass.Native.Services;

if (args.Length != 1 || !Path.IsPathFullyQualified(args[0]))
    throw new ArgumentException("Pass the absolute trusted Python interpreter path.");
string python = args[0];
int passed = 0;
async Task Run(string name, Func<Task> test)
{
    await test();
    Console.WriteLine("PASS " + name);
    passed++;
}
static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
static async Task WaitUntil(Func<bool> condition)
{
    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
    while (!condition()) await Task.Delay(10, timeout.Token);
}
async Task<BackgroundVoiceClient> Start(string mode, ConcurrentQueue<BackgroundVoiceEvent> events,
    CancellationToken cancellation = default) => await BackgroundVoiceClient.StartAsync(python, mode, 1,
        new InlineProgress(events.Enqueue), cancellation);

await Run("pause acknowledgment and repeated resume retain a single process", async () =>
{
    var events = new ConcurrentQueue<BackgroundVoiceEvent>();
    var worker = await Start("command", events);
    try
    {
        await WaitUntil(() => events.Any(value => value.Value == "ready"));
        await worker.ResumeAsync(7, default);
        await WaitUntil(() => events.Any(value => value.Kind == "command" && value.Epoch == 7));
        await worker.PauseAsync(default);
        await worker.ResumeAsync(8, default);
        await WaitUntil(() => events.Any(value => value.Kind == "command" && value.Epoch == 8));
        await worker.PauseAsync(default);
        Assert(!worker.Completion.IsCompleted, "Healthy paused worker exited");
    }
    finally { worker.Stop(); await worker.ObserveExitAsync(); worker.Dispose(); }
});
await Run("stop and application cancellation terminate a stalled worker", async () =>
{
    for (int iteration = 0; iteration < 3; iteration++)
    {
        using var lifetime = new CancellationTokenSource();
        var worker = await Start("idle", new(), lifetime.Token);
        var clock = Stopwatch.StartNew();
        lifetime.Cancel();
        worker.Stop(); worker.Stop();
        await worker.ObserveExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        Assert(worker.IsStopped && clock.Elapsed < TimeSpan.FromSeconds(5), "Child did not terminate");
        worker.Dispose();
    }
});
await Run("missing pause ACK kills capture before speech could proceed", async () =>
{
    var worker = await Start("no_pause", new());
    try
    {
        bool failed = false;
        try { await worker.PauseAsync(default); } catch (OperationCanceledException) { failed = true; }
        Assert(failed && worker.IsStopped, "Missing pause ACK was accepted");
        await worker.ObserveExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
    }
    finally { worker.Stop(); await worker.ObserveExitAsync(); worker.Dispose(); }
});
foreach (string mode in new[] { "bad_json", "long_line", "error", "ambient" })
    await Run("reject malformed/unaddressed worker output: " + mode, async () =>
    {
        var worker = await Start(mode, new());
        try
        {
            if (mode == "ambient") await worker.ResumeAsync(2, default);
            Exception? failure = null;
            try { await worker.Completion.WaitAsync(TimeSpan.FromSeconds(5)); }
            catch (Exception error) { failure = error; }
            Assert(failure is not null && failure is not TimeoutException, "Malformed output was accepted or hung");
            Assert(!failure!.ToString().Contains("PRIVATE DIAGNOSTIC"), "Raw dependency error escaped");
            Assert(worker.IsStopped, "Protocol failure left the process running");
        }
        finally { worker.Stop(); await worker.ObserveExitAsync(); worker.Dispose(); }
    });
Console.WriteLine($"{passed} process/protocol lifecycle tests passed. No audio device used.");

sealed class InlineProgress(Action<BackgroundVoiceEvent> action) : IProgress<BackgroundVoiceEvent>
{
    public void Report(BackgroundVoiceEvent value) => action(value);
}
