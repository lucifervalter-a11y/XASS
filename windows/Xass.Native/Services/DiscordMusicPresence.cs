using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace Xass.Native.Services;

public sealed record DiscordPresencePreferences(bool Enabled = false, string ApplicationId = "")
{
    public static bool ValidId(string value) => value.Length is >= 16 and <= 20 &&
        value.All(c => c is >= '0' and <= '9') && ulong.TryParse(value, out ulong id) && id > 0;
    public DiscordPresencePreferences Normalize() => ValidId(ApplicationId)
        ? this : new(false, "");
}

public sealed class DiscordPresenceStore
{
    private readonly string path;
    public DiscordPresenceStore(string? file = null) => path = file ?? Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "discord-presence.json");
    public DiscordPresencePreferences Load()
    {
        try
        {
            using var file = File.OpenRead(path);
            if (file.Length > 4096) return new();
            using var doc = JsonDocument.Parse(file);
            var root = doc.RootElement;
            if (root.GetProperty("version").GetInt32() != 1) return new();
            return new DiscordPresencePreferences(root.GetProperty("enabled").GetBoolean(),
                root.GetProperty("application_id").GetString() ?? "").Normalize();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException or KeyNotFoundException or FormatException) { return new(); }
    }
    public void Save(DiscordPresencePreferences preferences)
    {
        if (preferences.Enabled && !DiscordPresencePreferences.ValidId(preferences.ApplicationId))
            throw new InvalidDataException("Нужен публичный Application ID приложения XASS.");
        var clean = preferences.Normalize();
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                JsonSerializer.Serialize(stream, new { version = 1, enabled = clean.Enabled, application_id = clean.ApplicationId });
                stream.Flush(true);
            }
            File.Move(temporary, path, true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}

// This is the complete publication boundary. Never pass the player snapshot
// (queue paths, lyrics, signed artwork URLs or image bytes) to the RPC client.
public sealed record DiscordMusicTrack(string Title, string Artist, double Position, double Duration)
{
    private static string Label(JsonElement snapshot, string key)
    {
        if (!snapshot.TryGetProperty(key, out var field) || field.ValueKind != JsonValueKind.String) return "";
        string raw = field.GetString() ?? "";
        if (raw.Contains("://") || raw.Contains('\\') || raw.StartsWith('/') ||
            (raw.Length > 1 && char.IsLetter(raw[0]) && raw[1] == ':')) return "";
        var text = new StringBuilder();
        foreach (var rune in raw.EnumerateRunes())
        {
            if (Rune.IsControl(rune)) continue;
            if (text.Length + rune.Utf16SequenceLength > 128) break;
            text.Append(rune);
        }
        return text.ToString().Trim();
    }
    public static DiscordMusicTrack? FromSnapshot(JsonElement snapshot)
    {
        if (snapshot.ValueKind != JsonValueKind.Object || !snapshot.TryGetProperty("state", out var state) ||
            state.ValueKind != JsonValueKind.String || state.GetString() != "playing") return null;
        string title = Label(snapshot, "title"), artist = Label(snapshot, "artist");
        if (title.Length == 0) return null;
        double Number(string key) => snapshot.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.Number &&
            value.TryGetDouble(out double number) && double.IsFinite(number) ? Math.Clamp(number, 0, 86400) : 0;
        double duration = Number("duration"), position = Number("position");
        return new(title, artist, duration > 0 ? Math.Min(position, duration) : position, duration);
    }
    public object Activity(DateTimeOffset now)
    {
        long start = now.ToUnixTimeSeconds() - (long)Position;
        var activity = new Dictionary<string, object> { ["type"] = 2, ["details"] = Title };
        if (Artist.Length > 0) activity["state"] = Artist;
        var timestamps = new Dictionary<string, long> { ["start"] = start };
        if (Duration > Position) timestamps["end"] = start + (long)Duration;
        activity["timestamps"] = timestamps;
        return activity;
    }
}

public interface IDiscordPresenceConnection : IDisposable
{
    Task SetActivityAsync(DiscordMusicTrack? track, DateTimeOffset now, CancellationToken token);
}

// Only tokenless desktop Rich Presence is implemented: HANDSHAKE + SET_ACTIVITY.
// No AUTHORIZE, account tokens, subscriptions, voice actions, HTTP or shell.
public sealed class DiscordRpcConnection : IDiscordPresenceConnection
{
    private readonly Stream stream;
    private DiscordRpcConnection(Stream stream) => this.stream = stream;
    public static async Task<IDiscordPresenceConnection> ConnectAsync(string applicationId, CancellationToken token)
    {
        if (!DiscordPresencePreferences.ValidId(applicationId)) throw new InvalidDataException();
        for (int index = 0; index < 10; index++)
        {
            var pipe = new NamedPipeClientStream(".", $"discord-ipc-{index}", PipeDirection.InOut, PipeOptions.Asynchronous);
            try
            {
                await pipe.ConnectAsync(150, token).ConfigureAwait(false);
                return await HandshakeAsync(pipe, applicationId, token).ConfigureAwait(false);
            }
            catch (Exception error) when (error is IOException or TimeoutException) { pipe.Dispose(); }
            catch { pipe.Dispose(); throw; }
        }
        throw new IOException("Discord desktop unavailable");
    }
    // Stream injection exercises the actual framing and validation in tests.
    public static async Task<DiscordRpcConnection> HandshakeAsync(Stream stream, string applicationId, CancellationToken token)
    {
        if (!DiscordPresencePreferences.ValidId(applicationId)) throw new InvalidDataException();
        var connection = new DiscordRpcConnection(stream);
        try
        {
            await connection.WriteAsync(0, JsonSerializer.SerializeToUtf8Bytes(new { v = 1, client_id = applicationId }), token).ConfigureAwait(false);
            using var reply = await connection.ReadJsonAsync(token).ConfigureAwait(false);
            if (!reply.RootElement.TryGetProperty("evt", out var evt) || evt.GetString() != "READY") throw new InvalidDataException();
            return connection;
        }
        catch { connection.Dispose(); throw; }
    }
    public async Task SetActivityAsync(DiscordMusicTrack? track, DateTimeOffset now, CancellationToken token)
    {
        string nonce = Guid.NewGuid().ToString("N");
        byte[] data = JsonSerializer.SerializeToUtf8Bytes(new { cmd = "SET_ACTIVITY", args = new { pid = Environment.ProcessId, activity = track?.Activity(now) }, nonce });
        await WriteAsync(1, data, token).ConfigureAwait(false);
        for (int ignored = 0; ignored < 8; ignored++)
        {
            using var reply = await ReadJsonAsync(token).ConfigureAwait(false);
            var root = reply.RootElement;
            if (root.TryGetProperty("evt", out var evt) && evt.ValueKind == JsonValueKind.String && evt.GetString() == "ERROR") throw new InvalidDataException();
            if (root.TryGetProperty("nonce", out var echo) && echo.GetString() == nonce &&
                root.TryGetProperty("cmd", out var command) && command.GetString() == "SET_ACTIVITY") return;
        }
        throw new InvalidDataException();
    }
    private async Task WriteAsync(int opcode, byte[] data, CancellationToken token)
    {
        if (data.Length > 65536) throw new InvalidDataException();
        byte[] header = new byte[8];
        BinaryPrimitives.WriteInt32LittleEndian(header, opcode);
        BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(4), data.Length);
        await stream.WriteAsync(header, token).ConfigureAwait(false);
        await stream.WriteAsync(data, token).ConfigureAwait(false);
        await stream.FlushAsync(token).ConfigureAwait(false);
    }
    private async Task<JsonDocument> ReadJsonAsync(CancellationToken token)
    {
        for (int pings = 0; pings < 8; pings++)
        {
            byte[] header = new byte[8];
            await stream.ReadExactlyAsync(header, token).ConfigureAwait(false);
            int opcode = BinaryPrimitives.ReadInt32LittleEndian(header), length = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(4));
            if (length < 0 || length > 65536) throw new InvalidDataException();
            byte[] data = new byte[length];
            await stream.ReadExactlyAsync(data, token).ConfigureAwait(false);
            if (opcode == 3) { await WriteAsync(4, data, token).ConfigureAwait(false); continue; }
            if (opcode != 1) throw new IOException("Discord IPC closed");
            return JsonDocument.Parse(data, new JsonDocumentOptions { MaxDepth = 16 });
        }
        throw new InvalidDataException();
    }
    public void Dispose() => stream.Dispose();
}

public sealed class DiscordMusicPresence : IAsyncDisposable
{
    private readonly SemaphoreSlim gate = new(1, 1);
    private readonly Func<string, CancellationToken, Task<IDiscordPresenceConnection>> connect;
    private readonly Func<DateTimeOffset> clock;
    private IDiscordPresenceConnection? connection;
    private DiscordPresencePreferences preferences = new();
    private bool published, disposed;
    private DateTimeOffset nextUpdate;
    public string Status { get; private set; } = "Показ музыки в Discord выключен.";
    public DiscordMusicPresence(Func<string, CancellationToken, Task<IDiscordPresenceConnection>>? factory = null, Func<DateTimeOffset>? now = null)
    { connect = factory ?? DiscordRpcConnection.ConnectAsync; clock = now ?? (() => DateTimeOffset.UtcNow); }
    public async Task ConfigureAsync(DiscordPresencePreferences selected)
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            await ClearAsync().ConfigureAwait(false);
            preferences = selected.Normalize(); nextUpdate = DateTimeOffset.MinValue;
            Status = preferences.Enabled ? "Ожидаем воспроизведение и запущенный Discord." : "Показ музыки в Discord выключен.";
        }
        finally { gate.Release(); }
    }
    public async Task PublishAsync(DiscordMusicTrack? track)
    {
        if (!await gate.WaitAsync(0).ConfigureAwait(false)) return;
        try
        {
            if (disposed || !preferences.Enabled) return;
            if (track is null)
            {
                await ClearAsync().ConfigureAwait(false);
                Status = "Ничего не публикуется: воспроизведение остановлено или на паузе.";
                return;
            }
            if (clock() < nextUpdate) return;
            nextUpdate = clock().AddSeconds(15);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            try
            {
                connection ??= await connect(preferences.ApplicationId, deadline.Token).ConfigureAwait(false);
                await connection.SetActivityAsync(track, clock(), deadline.Token).ConfigureAwait(false);
                published = true;
                Status = "Discord принял статус трека. Видимость зависит от настроек активности Discord.";
            }
            catch
            {
                connection?.Dispose(); connection = null; published = false;
                Status = "Статус не отправлен. Проверьте запущенный Discord и Application ID; повторим автоматически.";
            }
        }
        finally { gate.Release(); }
    }
    private async Task ClearAsync()
    {
        try
        {
            if (connection is not null && published)
            {
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                await connection.SetActivityAsync(null, clock(), deadline.Token).ConfigureAwait(false);
            }
        }
        catch { /* Closing the owned IPC connection also removes its activity. */ }
        finally { connection?.Dispose(); connection = null; published = false; }
    }
    public async ValueTask DisposeAsync()
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try { disposed = true; await ClearAsync().ConfigureAwait(false); }
        finally { gate.Release(); }
    }
}
