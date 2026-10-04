using System.Text.Json;
using System.Text.RegularExpressions;

namespace Xass.Native.Services;

public sealed record NativeAutomaticGuards(bool Closed, bool Quitting, bool Pairing, bool Enabled,
    bool WindowVisible, bool MusicBusy, string MusicState, bool ArchiveBusy, bool SettingsBusy,
    bool AssistantBusy, bool CancellationRequested);

public static class NativeAutomaticUpdatePolicy
{
    public const int MaximumStateLength = 65536;
    public static bool AllowsShutdown(NativeAutomaticGuards state) =>
        !state.Closed && !state.Quitting && !state.Pairing && state.Enabled && !state.WindowVisible
        && !state.MusicBusy && state.MusicState is not ("playing" or "paused" or "loading" or "stopping")
        && !state.ArchiveBusy && !state.SettingsBusy && !state.AssistantBusy && !state.CancellationRequested;

    // The last synchronous guard and shutdown invocation share one dispatcher
    // continuation. No await permits queued UI work between that check and Quit.
    public static async Task<bool> FinishPreparationAsync(Func<Task<bool>> refreshRemoteGuards,
        Func<bool> currentGuards, Func<Task> cancelPrepared, Func<Task> shutdown)
    {
        bool committed = false;
        try
        {
            if (!await refreshRemoteGuards() || !currentGuards()) return false;
            await shutdown(); committed = true; return true;
        }
        finally
        {
            if (!committed) await cancelPrepared();
        }
    }

    public static bool MayAttemptRelease(string sha256, bool automatic, Func<string, bool> isRejected) =>
        !automatic || (ValidSha256(sha256) && !isRejected(sha256));

    public static string? RejectedSha256(string stateJson)
    {
        if (stateJson.Length > MaximumStateLength) throw new InvalidDataException("Состояние обновления слишком большое.");
        using var state = JsonDocument.Parse(stateJson);
        if (state.RootElement.ValueKind != JsonValueKind.Object) throw new InvalidDataException("Некорректное состояние обновления.");
        if (!state.RootElement.TryGetProperty("rejected_sha256", out var rejected)) return null;
        if (rejected.ValueKind != JsonValueKind.String || !ValidSha256(rejected.GetString()))
            throw new InvalidDataException("Некорректная контрольная сумма отклонённого обновления.");
        return rejected.GetString()!.ToLowerInvariant();
    }
    public static async Task<string?> ReadRejectedStateAsync(string path, CancellationToken token)
    {
        if (!File.Exists(path)) return null;
        using var reader = new StreamReader(new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete));
        var value = new System.Text.StringBuilder(); var buffer = new char[4096]; int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), token)) != 0)
        {
            if (value.Length + count > MaximumStateLength) throw new InvalidDataException("Состояние обновления слишком большое.");
            value.Append(buffer, 0, count);
        }
        return RejectedSha256(value.ToString());
    }
    public static bool ValidSha256(string? value) => value is { Length: 64 } && Regex.IsMatch(value, "^[a-fA-F0-9]{64}$");
}
