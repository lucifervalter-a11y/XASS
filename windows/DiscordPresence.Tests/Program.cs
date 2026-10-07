using System.Buffers.Binary;
using System.Text.Json;
using Xass.Native.Services;

int checks = 0;
void Require(bool condition, string label) { checks++; if (!condition) throw new Exception(label); }
async Task Reject(Func<Task> action, string label) { bool rejected = false; try { await action(); } catch { rejected = true; } Require(rejected, label); }
DiscordMusicTrack? Parse(object value) => DiscordMusicTrack.FromSnapshot(JsonSerializer.SerializeToElement(value));
const string AppId = "123456789012345678"; // Synthetic ID, never used with real Discord.
Require(!new DiscordPresencePreferences(true, "").Normalize().Enabled, "missing ID disables publication");
foreach (string id in new[] { "", "123", "../discord", "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧٨", "18446744073709551616", "token-secret", "1234567890123456\n" })
    Require(!DiscordPresencePreferences.ValidId(id), "invalid public ID");
Require(DiscordPresencePreferences.ValidId(AppId), "valid public ID");
var song = Parse(new { state = "playing", title = "Test song", artist = "Test artist", position = 25.0, duration = 180.0,
    queue = new[] { "C:\\private\\song.flac" }, lyrics = "PRIVATE LYRICS", cover = "PRIVATE IMAGE", artwork_url = "https://host/cover?ticket=PRIVATE" })!;
var now = DateTimeOffset.FromUnixTimeSeconds(2_000_000_000);
var activity = JsonSerializer.SerializeToElement(song.Activity(now));
Require(activity.GetProperty("type").GetInt32() == 2, "Listening activity");
Require(activity.EnumerateObject().Select(p => p.Name).Order().SequenceEqual(new[] { "details", "state", "timestamps", "type" }), "strict activity field whitelist");
Require(!activity.GetRawText().Contains("PRIVATE"), "no snapshot data leak");
Require(activity.GetProperty("timestamps").GetProperty("start").GetInt64() == 1_999_999_975, "elapsed timer");
Require(activity.GetProperty("timestamps").GetProperty("end").GetInt64() == 2_000_000_155, "remaining timer");
foreach (string state in new[] { "paused", "idle", "stopped", "ended", "error", "loading", "offline" })
    Require(Parse(new { state, title = "Track" }) is null, "non-playing clears presence");
foreach (string title in new[] { "C:\\private\\song.flac", "https://host/ticket?secret=abc", "/private/file", "" })
    Require(Parse(new { state = "playing", title }) is null, "reject path or URL in title");
Require(Parse(new { state = "playing", title = "Song\n\0", artist = "https://private", position = -1, duration = 90000 }) is { Title: "Song", Artist: "", Position: 0, Duration: 86400 }, "metadata bounds");
Require(Parse(new { state = "playing", title = string.Concat(Enumerable.Repeat("🎵", 100)) })!.Title.Length == 128, "unicode truncation retains complete runes");
Require(Parse(new { state = true, title = "Song" }) is null, "malformed state");

var directory = Path.Combine(Path.GetTempPath(), "xass-discord-tests-" + Guid.NewGuid().ToString("N"));
try
{
    var file = Path.Combine(directory, "settings.json"); var store = new DiscordPresenceStore(file);
    Require(!store.Load().Enabled, "first use opt in");
    store.Save(new(true, AppId)); Require(store.Load() == new DiscordPresencePreferences(true, AppId), "settings roundtrip");
    Require(JsonDocument.Parse(File.ReadAllText(file)).RootElement.EnumerateObject().Count() == 3, "settings whitelist");
    File.WriteAllText(file, "broken"); Require(!store.Load().Enabled, "corrupt settings fail closed");
    await Reject(() => { store.Save(new(true, "bad")); return Task.CompletedTask; }, "reject enabling invalid ID");
}
finally { if (Directory.Exists(directory)) Directory.Delete(directory, true); }

var connections = new List<FakeConnection>(); int attempts = 0;
var presence = new DiscordMusicPresence((id, token) => { attempts++; var c = new FakeConnection(); connections.Add(c); return Task.FromResult<IDiscordPresenceConnection>(c); }, () => now);
await presence.PublishAsync(song); Require(attempts == 0, "off never opens IPC");
await presence.ConfigureAsync(new(true, AppId)); await presence.PublishAsync(song);
Require(attempts == 1 && connections[0].Tracks.Count == 1, "enabled publishes playing");
await presence.PublishAsync(song); Require(connections[0].Tracks.Count == 1, "rate limited");
await presence.PublishAsync(null); Require(connections[0].Tracks.Last() is null && connections[0].Closed, "pause immediately clears and disconnects");
now = now.AddSeconds(15); await presence.PublishAsync(song); Require(attempts == 2, "resume reconnects");
await presence.ConfigureAsync(new(false, AppId)); Require(connections[1].Tracks.Last() is null && connections[1].Closed, "disable clears");
await presence.PublishAsync(song); Require(attempts == 2, "disabled remains silent");
await presence.ConfigureAsync(new(true, AppId)); await presence.PublishAsync(song); await presence.DisposeAsync();
Require(connections.Last().Tracks.Last() is null && connections.Last().Closed, "exit clears");
await presence.PublishAsync(song); Require(attempts == 3, "disposed stays silent");
var failed = new DiscordMusicPresence((id, token) => { attempts++; throw new IOException(); }, () => now);
await failed.ConfigureAsync(new(true, AppId)); await failed.PublishAsync(song); int afterFailure = attempts;
await failed.PublishAsync(song); Require(attempts == afterFailure && !failed.Status.Contains("принял"), "failed IPC backoff and honest status");
await failed.DisposeAsync();

var wire = new ScriptedStream { PingFirst = true };
using (var rpc = await DiscordRpcConnection.HandshakeAsync(wire, AppId, CancellationToken.None))
{
    await rpc.SetActivityAsync(song, now, CancellationToken.None);
    await rpc.SetActivityAsync(null, now, CancellationToken.None);
    Require(wire.Opcodes.Contains(4), "respond to IPC ping");
    Require(wire.Commands.SequenceEqual(new[] { "SET_ACTIVITY", "SET_ACTIVITY" }), "only allowed RPC command");
    Require(wire.ActivityNull.Last(), "null activity clear frame");
}
foreach (string mode in new[] { "error", "oversize", "negative", "close", "truncated" })
    await Reject(async () => { using var rpc = await DiscordRpcConnection.HandshakeAsync(new ScriptedStream { Failure = mode }, AppId, CancellationToken.None); }, "reject malformed IPC " + mode);
var errorWire = new ScriptedStream();
using (var rpc = await DiscordRpcConnection.HandshakeAsync(errorWire, AppId, CancellationToken.None))
{
    errorWire.Failure = "error";
    await Reject(() => rpc.SetActivityAsync(song, now, CancellationToken.None), "do not claim success on Discord error");
}
Require(MicrophonePresentation.For(MicrophonePhase.Off, false).CanStop == false, "off has no misleading stop action");
Require(MicrophonePresentation.For(MicrophonePhase.Preparing, true).Action == "Отменить подготовку", "preparing cancels prep");
Require(MicrophonePresentation.For(MicrophonePhase.Processing, true).Action == "Отменить обработку", "processing cancels recognition");
Require(MicrophonePresentation.For(MicrophonePhase.Listening, true).Action == "Выключить микрофон", "active capture stop");
Require(MicrophonePresentation.For(MicrophonePhase.Failed, false).CanStop, "failed shutdown remains actionable");
Require(!MicrophonePresentation.For(MicrophonePhase.Stopping, true).CanStop, "pending stop is not off");
Console.WriteLine($"{checks} checks passed: Discord whitelist, framing, lifecycle, preferences, privacy and microphone presentation. No real Discord or microphone used.");

sealed class FakeConnection : IDiscordPresenceConnection
{
    public List<DiscordMusicTrack?> Tracks { get; } = new(); public bool Closed;
    public Task SetActivityAsync(DiscordMusicTrack? track, DateTimeOffset now, CancellationToken token) { Tracks.Add(track); return Task.CompletedTask; }
    public void Dispose() => Closed = true;
}
sealed class ScriptedStream : Stream
{
    readonly MemoryStream written = new(); readonly Queue<byte> replies = new();
    public string Failure = ""; public bool PingFirst;
    public List<int> Opcodes = new(); public List<string> Commands = new(); public List<bool> ActivityNull = new();
    void Frame(int opcode, byte[] payload, int? declared = null)
    {
        byte[] header = new byte[8]; BinaryPrimitives.WriteInt32LittleEndian(header, opcode); BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(4), declared ?? payload.Length);
        foreach (byte b in header.Concat(payload)) replies.Enqueue(b);
    }
    public override void Flush()
    {
        byte[] data = written.ToArray(); written.SetLength(0);
        int opcode = BinaryPrimitives.ReadInt32LittleEndian(data); Opcodes.Add(opcode);
        if (opcode == 4) return;
        using var payload = JsonDocument.Parse(data.AsMemory(8));
        if (Failure == "oversize") { Frame(1, Array.Empty<byte>(), 65537); return; }
        if (Failure == "negative") { Frame(1, Array.Empty<byte>(), -1); return; }
        if (Failure == "close") { Frame(2, Array.Empty<byte>()); return; }
        if (Failure == "truncated") { Frame(1, new byte[] { 123 }, 10); return; }
        if (Failure == "error") { Frame(1, JsonSerializer.SerializeToUtf8Bytes(new { evt = "ERROR" })); return; }
        if (opcode == 0)
        {
            if (PingFirst) Frame(3, new byte[] { 1, 2, 3 });
            Frame(1, JsonSerializer.SerializeToUtf8Bytes(new { evt = "READY", data = new { user = new { username = "NEVER LOG" } } }));
        }
        else
        {
            var root = payload.RootElement; string command = root.GetProperty("cmd").GetString()!;
            Commands.Add(command); ActivityNull.Add(root.GetProperty("args").GetProperty("activity").ValueKind == JsonValueKind.Null);
            Frame(1, JsonSerializer.SerializeToUtf8Bytes(new { cmd = command, nonce = root.GetProperty("nonce").GetString() }));
        }
    }
    public override Task FlushAsync(CancellationToken token) { token.ThrowIfCancellationRequested(); Flush(); return Task.CompletedTask; }
    public override int Read(byte[] buffer, int offset, int count) { int n = Math.Min(count, replies.Count); for (int i = 0; i < n; i++) buffer[offset + i] = replies.Dequeue(); return n; }
    public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken token = default) { token.ThrowIfCancellationRequested(); int n = Math.Min(buffer.Length, replies.Count); for (int i = 0; i < n; i++) buffer.Span[i] = replies.Dequeue(); return ValueTask.FromResult(n); }
    public override void Write(byte[] buffer, int offset, int count) => written.Write(buffer, offset, count);
    public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken token = default) { token.ThrowIfCancellationRequested(); written.Write(buffer.Span); return ValueTask.CompletedTask; }
    public override bool CanRead => true; public override bool CanWrite => true; public override bool CanSeek => false;
    public override long Length => throw new NotSupportedException(); public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException(); public override void SetLength(long value) => throw new NotSupportedException();
}
