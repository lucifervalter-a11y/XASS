namespace Xass.Native.Services;

public enum NativeUpdateAvailability { Available, Current, NoRelease, OlderVersion, UnknownVersion }
public sealed record NativeUpdateCheck(NativeUpdateAvailability Availability, NativeUpdate? Update);

public static class NativeUpdateVersionPolicy
{
    // Match the companion updater: exactly three bounded numeric components. Unknown
    // formats fail closed instead of treating a lexical comparison as an upgrade.
    public static Version? Parse(string value) =>
        System.Text.RegularExpressions.Regex.IsMatch(value, @"^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$")
        && Version.TryParse(value, out var version)
            ? new Version(version.Major, version.Minor, version.Build, Math.Max(0, version.Revision)) : null;

    public static NativeUpdateCheck Evaluate(NativeUpdate? update, string installedRevision, string installedVersion, string? installedDistribution = null)
    {
        if (update is null) return new(NativeUpdateAvailability.NoRelease, null);
        if (string.Equals(update.Revision, installedRevision, StringComparison.OrdinalIgnoreCase)
            && (installedDistribution is null || installedDistribution == update.Distribution))
            return new(NativeUpdateAvailability.Current, update);
        var installed = Parse(installedVersion); var candidate = Parse(update.Version);
        if (installed is null || candidate is null) return new(NativeUpdateAvailability.UnknownVersion, update);
        return new(candidate < installed ? NativeUpdateAvailability.OlderVersion : NativeUpdateAvailability.Available, update);
    }

    public static void RequireInstallable(NativeUpdate update, NativeReleaseChannel selected, string installedRevision, string installedVersion, string? installedDistribution = null)
    {
        if (update.Distribution != selected.Distribution)
            throw new InvalidOperationException("Канал пакета изменился. Повторите проверку обновлений.");
        if (Evaluate(update, installedRevision, installedVersion, installedDistribution).Availability != NativeUpdateAvailability.Available)
            throw new InvalidOperationException("Установка старой, уже установленной или неизвестной версии заблокирована. Повторите проверку обновлений.");
    }
}

// A lease freezes the selected channel across check/confirmation/download/install.
// A second check cannot overwrite its result and selection cannot race an await.
public sealed class NativeUpdateSelection
{
    private readonly object gate = new();
    private NativeReleaseChannel selected;
    private Operation? active;
    public NativeUpdateSelection(NativeReleaseChannel selected) => this.selected = NativeReleaseChannel.FromDistribution(selected.Distribution);
    public NativeReleaseChannel Selected { get { lock (gate) return selected; } }
    public bool IsBusy { get { lock (gate) return active is not null; } }
    public bool TrySelect(NativeReleaseChannel channel, Action<NativeReleaseChannel> persist)
    {
        lock (gate)
        {
            if (active is not null) return false;
            var canonical = NativeReleaseChannel.FromDistribution(channel.Distribution);
            persist(canonical); // Failed persistence must leave the old selection intact.
            selected = canonical;
            return true;
        }
    }
    public Operation? TryBegin()
    {
        lock (gate)
        {
            if (active is not null) return null;
            return active = new Operation(this, selected);
        }
    }
    public sealed class Operation : IDisposable
    {
        private readonly NativeUpdateSelection owner;
        public NativeReleaseChannel Channel { get; }
        internal Operation(NativeUpdateSelection owner, NativeReleaseChannel channel) { this.owner = owner; Channel = channel; }
        public void Dispose() { lock (owner.gate) { if (ReferenceEquals(owner.active, this)) owner.active = null; } }
    }
}
