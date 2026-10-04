using System.Text.Json;
namespace Xass.Native.Services;

// Only opt-in and bounded public release hashes are stored here. A manual retry
// bypasses rejection for that one requested attempt; it never clears history.
public sealed class NativeUpdatePreferencesStore
{
    private readonly string path;
    private readonly object gate = new();
    private sealed record State(bool Enabled, List<string> Rejected);
    public NativeUpdatePreferencesStore(string path) => this.path = path;
    private State Read()
    {
        if (!File.Exists(path)) return new(false, new());
        if (new FileInfo(path).Length > 65536) throw new InvalidDataException("Настройки обновления слишком большие.");
        using var data = JsonDocument.Parse(File.ReadAllText(path));
        if (data.RootElement.GetProperty("schema").GetInt32() != 1) throw new InvalidDataException("Неизвестная версия настроек обновления.");
        bool enabled = data.RootElement.GetProperty("automatic_enabled").GetBoolean();
        var rejected = new List<string>();
        if (data.RootElement.TryGetProperty("rejected_sha256", out var stored))
        {
            if (stored.ValueKind != JsonValueKind.Array || stored.GetArrayLength() > 32) throw new InvalidDataException("Некорректный список отклонённых обновлений.");
            foreach (var item in stored.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.String || !NativeAutomaticUpdatePolicy.ValidSha256(item.GetString()))
                    throw new InvalidDataException("Некорректная контрольная сумма обновления.");
                string value = item.GetString()!.ToLowerInvariant();
                if (!rejected.Contains(value)) rejected.Add(value);
            }
        }
        return new(enabled, rejected);
    }
    private void Write(State state)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = path + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(stream, new { schema = 1, automatic_enabled = state.Enabled, rejected_sha256 = state.Rejected });
            stream.Flush(flushToDisk: true);
        }
        File.Move(temporary, path, true);
    }
    public bool Load()
    { lock (gate) { try { return Read().Enabled; } catch { return false; } } }
    public void Save(bool enabled)
    { lock (gate) { State state = Read(); Write(state with { Enabled = enabled }); } }
    public void RememberRejected(string sha256)
    {
        if (!NativeAutomaticUpdatePolicy.ValidSha256(sha256)) throw new InvalidDataException("Неверная контрольная сумма обновления.");
        lock (gate)
        {
            State state = Read(); string normalized = sha256.ToLowerInvariant();
            if (state.Rejected.Contains(normalized)) return;
            state.Rejected.Add(normalized);
            if (state.Rejected.Count > 32) state.Rejected.RemoveAt(0);
            Write(state);
        }
    }
    public bool IsRejected(string sha256)
    {
        if (!NativeAutomaticUpdatePolicy.ValidSha256(sha256)) return true;
        lock (gate) { try { return Read().Rejected.Contains(sha256.ToLowerInvariant()); } catch { return true; } }
    }
}

public static class NativeUpdatePreferences
{
    private static readonly NativeUpdatePreferencesStore Store = new(Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "native-updater.json"));
    public static bool Load() => Store.Load();
    public static void Save(bool enabled) => Store.Save(enabled);
    public static void RememberRejected(string sha256) => Store.RememberRejected(sha256);
    public static bool IsRejected(string sha256) => Store.IsRejected(sha256);
}
