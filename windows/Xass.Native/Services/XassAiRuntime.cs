using System.Diagnostics;
using System.Text.Json;

namespace Xass.Native.Services;

public enum XassAiAvailabilityKind
{
    NotInstalled,
    InstalledUnavailable,
    TestMode,
    Ready
}

public sealed record XassAiAvailability(XassAiAvailabilityKind Kind, string Message);

public static class XassAiInstallationProbe
{
    public static XassAiAvailability Probe()
    {
        if (NativeRuntimeMode.IsTestMode)
            return new XassAiAvailability(
                XassAiAvailabilityKind.TestMode,
                "XASS AI отключён в изолированном тестовом режиме.");

        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string installedSource = Path.Combine(local, "hermes", "hermes-agent");
        return Directory.Exists(installedSource)
            ? new XassAiAvailability(
                XassAiAvailabilityKind.InstalledUnavailable,
                "XASS AI установлен, но сейчас недоступен. XASS не подключается к чужим процессам и ожидает собственный защищённый локальный запуск.")
            : new XassAiAvailability(
                XassAiAvailabilityKind.NotInstalled,
                "XASS AI не установлен. Чат останется отключённым до установки локального компонента.");
    }
}

/// <summary>
/// Lifecycle handoff for a child that the XASS process has just created. This class never
/// discovers, attaches to, or opens another process by PID, and it contains no launch path.
/// A future launcher may hand over its Process object, private ready file and in-memory token.
/// </summary>
internal sealed class XassAiOwnedRuntime : IAsyncDisposable
{
    private const int MaximumReadyFileBytes = 4096;
    private static readonly TimeSpan ReadyTimeout = TimeSpan.FromSeconds(90);
    private Process? ownedChild;
    private readonly string readyFile;
    private readonly string sessionToken;

    internal XassAiOwnedRuntime(Process newlyStartedChild, string readyFile, string sessionToken)
    {
        if (NativeRuntimeMode.IsTestMode)
            throw new InvalidOperationException("XASS AI runtime is disabled in isolated test mode.");
        ownedChild = newlyStartedChild ?? throw new ArgumentNullException(nameof(newlyStartedChild));
        this.readyFile = Path.GetFullPath(readyFile ?? throw new ArgumentNullException(nameof(readyFile)));
        this.sessionToken = string.IsNullOrWhiteSpace(sessionToken)
            ? throw new ArgumentException("An in-memory session token is required.", nameof(sessionToken))
            : sessionToken;
    }

    internal async Task<XassAiRuntimeEndpoint> WaitForEndpointAsync(CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(ReadyTimeout);
        while (true)
        {
            timeout.Token.ThrowIfCancellationRequested();
            Process child = ownedChild ?? throw new ObjectDisposedException(nameof(XassAiOwnedRuntime));
            if (child.HasExited) throw new XassAiProtocolException("XASS AI stopped before becoming ready.");
            int? port = ReadReadyPort(readyFile);
            if (port.HasValue) return new XassAiRuntimeEndpoint(port.Value, sessionToken);
            await Task.Delay(50, timeout.Token).ConfigureAwait(false);
        }
    }

    private static int? ReadReadyPort(string path)
    {
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists || info.Length is <= 0 or > MaximumReadyFileBytes) return null;
            using FileStream input = new(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using JsonDocument document = JsonDocument.Parse(input);
            if (document.RootElement.ValueKind != JsonValueKind.Object
                || !document.RootElement.TryGetProperty("port", out JsonElement value)
                || !value.TryGetInt32(out int port)
                || port is < 1 or > 65535)
                return null;
            return port;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            // A writer may still be replacing the small ready file. Retry until the bounded deadline.
            return null;
        }
    }

    public async ValueTask DisposeAsync()
    {
        Process? child = Interlocked.Exchange(ref ownedChild, null);
        if (child is not null)
        {
            try
            {
                if (!child.HasExited) child.Kill(entireProcessTree: true);
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                await child.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
            catch (Exception error) when (error is InvalidOperationException or NotSupportedException or System.ComponentModel.Win32Exception or OperationCanceledException) { }
            finally { child.Dispose(); }
        }
        try { File.Delete(readyFile); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }
}
