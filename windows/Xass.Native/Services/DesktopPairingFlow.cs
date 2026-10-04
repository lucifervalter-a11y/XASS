using System.Text;
using System.Text.Json;

namespace Xass.Native.Services;

/// <summary>
/// One operation owns its input from selection through confirmation and pairing.
/// No profile source or pairing code survives as mutable shared state.
/// </summary>
public sealed class DesktopPairingFlow
{
    public delegate Task<JsonElement> SendRequest(object request, CancellationToken cancellation);
    private const int MaxProfileBytes = 65536;
    private int busy;
    public bool IsBusy => Volatile.Read(ref busy) != 0;

    public Task<JsonElement?> PairManualAsync(string server, string name, string code,
        SendRequest send, CancellationToken cancellation) => RunAsync(async () =>
    {
        cancellation.ThrowIfCancellationRequested();
        return await send(new { action = "desktop_pair", server = server.Trim(), name = name.Trim(), code }, cancellation);
    });

    public Task<JsonElement?> ImportAsync(Func<CancellationToken, Task<string?>> readSource,
        SendRequest send, Func<JsonElement, CancellationToken, Task<bool>> confirm,
        CancellationToken cancellation) => RunAsync(async () =>
    {
        cancellation.ThrowIfCancellationRequested();
        // Snapshot once, including files: the exact reviewed JSON is sent after confirmation.
        string? content = await readSource(cancellation);
        cancellation.ThrowIfCancellationRequested();
        if (content is null) return null; // Picker closed or cancelled.
        ValidateContent(content);
        JsonElement preview = await send(new { action = "desktop_profile", text = content }, cancellation);
        cancellation.ThrowIfCancellationRequested();
        if (!await confirm(preview, cancellation)) return null; // Close, Cancel and Escape all decline.
        cancellation.ThrowIfCancellationRequested();
        return await send(new { action = "desktop_pair_profile", text = content, confirmed = true }, cancellation);
    });

    private async Task<JsonElement?> RunAsync(Func<Task<JsonElement?>> operation)
    {
        if (Interlocked.CompareExchange(ref busy, 1, 0) != 0) return null;
        try { return await operation(); }
        finally { Volatile.Write(ref busy, 0); }
    }

    private static void ValidateContent(string content)
    {
        if (Encoding.UTF8.GetByteCount(content) > MaxProfileBytes)
            throw new InvalidOperationException("Файл подключения слишком большой.");
    }

    public static async Task<string> ReadProfileFileAsync(string path, CancellationToken cancellation)
    {
        if (!Path.IsPathFullyQualified(path) || !new[] { ".xass", ".xass-connect", ".json" }
            .Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase))
            throw new InvalidOperationException("Выберите файл подключения .xass, .xass-connect или JSON.");
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            4096, FileOptions.Asynchronous | FileOptions.SequentialScan);
        // Bound the read even if the file changes size. Never log the path or contents.
        byte[] bytes = new byte[MaxProfileBytes + 1];
        int length = 0;
        while (length < bytes.Length)
        {
            int read = await file.ReadAsync(bytes.AsMemory(length), cancellation);
            if (read == 0) break;
            length += read;
        }
        if (length > MaxProfileBytes) throw new InvalidOperationException("Файл подключения слишком большой.");
        string content = new UTF8Encoding(false, true).GetString(bytes, 0, length);
        if (content.StartsWith('\uFEFF')) content = content[1..];
        ValidateContent(content);
        return content;
    }
}
