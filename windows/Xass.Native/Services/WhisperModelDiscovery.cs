using System.Diagnostics;
using System.Text.Json;

namespace Xass.Native.Services;

public sealed record WhisperCacheRoot(string Path, string Source);
public sealed record DiscoveredWhisperModel(string Path, string Repository, string Source);

/// <summary>Read-only, bounded probes of existing Hugging Face model snapshots. Never downloads.</summary>
public static class WhisperModelDiscovery
{
    public const int MaximumCacheRoots = 9;
    public const int MaximumRepositoriesPerRoot = 128;
    public const int MaximumSnapshotsPerRepository = 16;
    public const int MaximumSnapshotProbes = 256;
    private const int MaximumConfigBytes = 1024 * 1024;
    private static readonly StringComparer PathComparer = OperatingSystem.IsWindows()
        ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
    private static readonly string[] PreferredRepositories =
        { "models--Systran--faster-whisper-small", "models--guillaumekln--faster-whisper-small" };

    public static IReadOnlyList<WhisperCacheRoot> GetCacheRoots(
        Func<string, string?>? environment = null, string? userProfile = null)
    {
        environment ??= Environment.GetEnvironmentVariable;
        userProfile ??= Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var roots = new List<WhisperCacheRoot>();
        Add(environment("HF_HUB_CACHE"), "HF_HUB_CACHE");
        Add(environment("HUGGINGFACE_HUB_CACHE"), "HUGGINGFACE_HUB_CACHE");
        AddHome(environment("HF_HOME"), "hub", "HF_HOME");
        AddHome(environment("XDG_CACHE_HOME"), Path.Combine("huggingface", "hub"), "XDG_CACHE_HOME");
        AddHome(userProfile, Path.Combine(".cache", "huggingface", "hub"), "user cache");
        AddXass(environment("XASS_DATA_ROOT"), "XASS_DATA_ROOT");
        string? local = environment("LOCALAPPDATA");
        if (string.IsNullOrWhiteSpace(local) && !string.IsNullOrWhiteSpace(userProfile))
            local = Path.Combine(userProfile, "AppData", "Local");
        string? localPath = NormalizeLocalPath(local, userProfile);
        if (localPath is not null) AddXass(Path.Combine(localPath, "XASS"), "XASS transcription cache");
        return roots;

        void AddXass(string? dataRoot, string source)
        {
            AddHome(dataRoot, Path.Combine("transcription", "models", "whisper"), source);
            AddHome(dataRoot, Path.Combine("transcription", "models", "hf", "hub"), source);
        }
        void AddHome(string? home, string suffix, string source)
        {
            string? normalized = NormalizeLocalPath(home, userProfile);
            if (normalized is not null) Add(Path.Combine(normalized, suffix), source);
        }
        void Add(string? path, string source)
        {
            string? normalized = NormalizeLocalPath(path, userProfile);
            if (normalized is not null && !roots.Any(root => PathComparer.Equals(root.Path, normalized)))
                roots.Add(new(normalized, source));
        }
    }

    public static DiscoveredWhisperModel? FindExisting(CancellationToken cancellationToken = default) =>
        FindExisting(GetCacheRoots(), cancellationToken);

    public static DiscoveredWhisperModel? FindExisting(IEnumerable<WhisperCacheRoot> cacheRoots,
        CancellationToken cancellationToken = default)
    {
        var elapsed = Stopwatch.StartNew();
        int probes = 0;
        // Order is small, base, tiny, medium, larger/other multilingual Whisper conversions;
        // configured cache order wins ties. An existing small installation is preferred.
        (DiscoveredWhisperModel Model, int Rank)? best = null;
        foreach (var root in cacheRoots.Take(MaximumCacheRoots))
        {
            if (Stop()) break;
            string? path = NormalizeLocalPath(root.Path, null);
            if (path is null || !IsPlainDirectoryPath(path)) continue;
            var repositories = PreferredRepositories.Select(name => Path.Combine(path, name))
                .Concat(ChildDirectories(path, MaximumRepositoriesPerRoot))
                .Distinct(PathComparer)
                .Where(repo => IsWhisperRepository(Path.GetFileName(repo)))
                .OrderBy(repo => ModelRank(Path.GetFileName(repo)))
                .ThenBy(repo => repo, PathComparer);
            foreach (string repository in repositories)
            {
                if (Stop()) break;
                if (!IsPlainDirectoryPath(repository)) continue;
                string snapshots = Path.Combine(repository, "snapshots");
                if (!IsPlainDirectoryPath(snapshots)) continue;
                string? main = MainRevision(repository);
                var revisions = (main is null ? Array.Empty<string>() : new[] { Path.Combine(snapshots, main) })
                    .Concat(ChildDirectories(snapshots, MaximumSnapshotsPerRepository)
                        .Where(snapshot => IsRevision(Path.GetFileName(snapshot)))
                        .OrderByDescending(SafeLastWriteUtc).ThenBy(snapshot => snapshot, PathComparer))
                    .Distinct(PathComparer);
                foreach (string snapshot in revisions)
                {
                    if (Stop()) break;
                    probes++;
                    if (!IsPlainDirectoryPath(snapshot) || !HasLocalFiles(snapshot, repository)) continue;
                    int rank = ModelRank(Path.GetFileName(repository));
                    if (best is null || rank < best.Value.Rank)
                        best = (new(snapshot, Path.GetFileName(repository), root.Source), rank);
                    if (rank == 0) return best.Value.Model;
                    // The first valid revision is refs/main when present, otherwise newest within the bound.
                    break;
                }
            }
            if (best is { Rank: 0 }) break;
        }
        return best?.Model;

        bool Stop()
        {
            cancellationToken.ThrowIfCancellationRequested();
            return probes >= MaximumSnapshotProbes || elapsed.Elapsed >= TimeSpan.FromSeconds(2);
        }
    }

    private static bool IsWhisperRepository(string name) =>
        name.StartsWith("models--", StringComparison.OrdinalIgnoreCase)
        && name.Contains("whisper", StringComparison.OrdinalIgnoreCase)
        && !name.Contains(".en", StringComparison.OrdinalIgnoreCase);

    private static int ModelRank(string name)
    {
        foreach (var (suffix, rank) in new[] { ("--faster-whisper-small", 0), ("--faster-whisper-base", 1),
                     ("--faster-whisper-tiny", 2), ("--faster-whisper-medium", 3) })
            if (name.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) return rank;
        return 4;
    }

    private static string? MainRevision(string repository)
    {
        try
        {
            string refs = Path.Combine(repository, "refs");
            if (!IsPlainDirectoryPath(refs)) return null;
            string path = Path.Combine(refs, "main");
            if (!TryReadableFile(path, repository, out string resolved, out long length) || length > 128) return null;
            string? contents = ReadBoundedText(resolved, 128);
            if (contents is null) return null;
            string revision = contents.Trim();
            return IsRevision(revision) ? revision : null;
        }
        catch (Exception error) when (IsFileError(error)) { return null; }
    }

    private static bool IsRevision(string name) => name.Length is 40 or 64
        && name.All(character => char.IsAsciiHexDigit(character));

    private static bool HasLocalFiles(string snapshot, string repository)
    {
        try
        {
            if (!TryReadableFile(Path.Combine(snapshot, "model.bin"), repository, out _, out _) ||
                !TryReadableFile(Path.Combine(snapshot, "tokenizer.json"), repository, out _, out _) ||
                !TryReadableFile(Path.Combine(snapshot, "config.json"), repository, out string config, out long size) ||
                size > MaximumConfigBytes) return false;
            string? contents = ReadBoundedText(config, MaximumConfigBytes);
            if (contents is null) return false;
            using var document = JsonDocument.Parse(contents);
            var data = document.RootElement;
            // Whisper's CTranslate2 config carries language token IDs. English-only models cannot
            // serve XASS's Russian transcription. This also rejects unrelated model.bin files.
            return data.ValueKind == JsonValueKind.Object && data.TryGetProperty("lang_ids", out var languages)
                && languages.ValueKind == JsonValueKind.Array && languages.GetArrayLength() > 1;
        }
        catch (Exception error) when (IsFileError(error) || error is JsonException) { return false; }
    }

    private static bool TryReadableFile(string path, string repository, out string resolved, out long length)
    {
        resolved = path;
        length = 0;
        var file = new FileInfo(path);
        string? linkTarget = file.LinkTarget;
        if (linkTarget is not null)
        {
            // HF normally links snapshot files to ../../blobs/<hash>. Permit only that local,
            // single-hop shape; don't follow arbitrary links, junctions, or network locations.
            if (string.IsNullOrWhiteSpace(linkTarget)) return false;
            string? target = NormalizeLocalPath(Path.GetFullPath(linkTarget, file.DirectoryName!), null);
            string blobs = Path.Combine(repository, "blobs");
            if (target is null || !PathComparer.Equals(Path.GetDirectoryName(target), blobs)
                || !IsPlainDirectoryPath(blobs)) return false;
            file = new FileInfo(target);
            if (!file.Exists || (file.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0) return false;
            resolved = target;
        }
        if (!file.Exists || (file.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0) return false;
        using var stream = new FileStream(resolved, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        length = stream.Length;
        return length > 0 && stream.ReadByte() >= 0;
    }

    private static IReadOnlyList<string> ChildDirectories(string parent, int maximum)
    {
        var result = new List<string>();
        try
        {
            // No recursion; a large cache cannot cause an unbounded materialization or sort.
            foreach (string child in Directory.EnumerateDirectories(parent, "*", new EnumerationOptions
            {
                RecurseSubdirectories = false, IgnoreInaccessible = true,
                AttributesToSkip = FileAttributes.ReparsePoint | FileAttributes.System
            }).Take(maximum)) result.Add(child);
        }
        catch (Exception error) when (IsFileError(error)) { }
        return result;
    }

    private static DateTime SafeLastWriteUtc(string path)
    {
        try { return Directory.GetLastWriteTimeUtc(path); }
        catch (Exception error) when (IsFileError(error)) { return DateTime.MinValue; }
    }

    private static bool IsPlainDirectoryPath(string path)
    {
        try
        {
            // Check from root to leaf before touching a redirected descendant. HF blob *file*
            // links are handled separately; directories, junctions and mount redirections are skipped.
            var ancestors = new Stack<DirectoryInfo>();
            for (DirectoryInfo? directory = new(path); directory is not null; directory = directory.Parent)
                ancestors.Push(directory);
            foreach (var directory in ancestors)
            {
                var attributes = directory.Attributes;
                if ((attributes & FileAttributes.ReparsePoint) != 0 || (attributes & FileAttributes.Directory) == 0)
                    return false;
            }
            return true;
        }
        catch (Exception error) when (IsFileError(error)) { return false; }
    }

    private static string? NormalizeLocalPath(string? path, string? userProfile)
    {
        if (string.IsNullOrWhiteSpace(path) || path.Length > 4096) return null;
        try
        {
            path = path.Trim();
            if (path.StartsWith("~/", StringComparison.Ordinal) || path.StartsWith("~\\", StringComparison.Ordinal))
            {
                if (string.IsNullOrWhiteSpace(userProfile)) return null;
                path = Path.Combine(userProfile, path[2..]);
            }
            // Reject UNC and device namespaces before even querying their filesystem.
            if (path.StartsWith("\\\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal)
                || !Path.IsPathFullyQualified(path)) return null;
            path = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
            if (OperatingSystem.IsWindows() && new DriveInfo(Path.GetPathRoot(path)!).DriveType == DriveType.Network)
                return null;
            return path;
        }
        catch (Exception error) when (IsFileError(error)) { return null; }
    }

    private static string? ReadBoundedText(string path, int maximumBytes)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        byte[] buffer = new byte[maximumBytes + 1];
        int count = 0;
        while (count < buffer.Length)
        {
            int read = stream.Read(buffer, count, buffer.Length - count);
            if (read == 0) break;
            count += read;
        }
        return count > maximumBytes ? null : System.Text.Encoding.UTF8.GetString(buffer, 0, count);
    }

    private static bool IsFileError(Exception error) => error is IOException or UnauthorizedAccessException
        or ArgumentException or NotSupportedException or System.Security.SecurityException;
}
