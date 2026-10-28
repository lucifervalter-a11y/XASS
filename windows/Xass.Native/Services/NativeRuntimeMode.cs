using System.Security.Cryptography;
using System.Text;

namespace Xass.Native.Services;

/// <summary>
/// Keeps development and acceptance runs physically separate from the installed
/// XASS profile. Test mode is opt-in and fails closed on unsafe roots.
/// </summary>
public static class NativeRuntimeMode
{
    public const string TestRootVariable = "XASS_NATIVE_TEST_ROOT";
    private static readonly string? testRoot = ResolveTestRoot();

    public static bool IsTestMode => testRoot is not null;
    public static bool AllowBackgroundServices => !IsTestMode;
    public static bool AllowUpdates => !IsTestMode;
    public static string DataRoot => IsTestMode
        ? Path.Combine(testRoot!, "data")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS");
    public static string PreferencesRoot => IsTestMode
        ? Path.Combine(testRoot!, "preferences")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native");
    public static string RuntimePreferencesFile => Path.Combine(PreferencesRoot, "runtime.json");
    public static string UpdatePreferencesFile => Path.Combine(PreferencesRoot, "native-updater.json");
    public static string UpdatesRoot => Path.Combine(PreferencesRoot, "updates");
    public static string FailureLogFile => Path.Combine(PreferencesRoot, "native-failure.txt");
    public static string InstanceName => IsTestMode
        ? "XASS.Native.Test." + RootFingerprint(testRoot!) + ".Activation"
        : "XASS.Native.Test.Activation";

    private static string? ResolveTestRoot()
    {
        string? value = Environment.GetEnvironmentVariable(TestRootVariable);
        if (string.IsNullOrWhiteSpace(value)) return null;
        if (!Path.IsPathFullyQualified(value))
            throw new InvalidOperationException($"{TestRootVariable} must be an absolute path.");

        string root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(value));
        string? volume = Path.GetPathRoot(root);
        string profile = Path.TrimEndingDirectorySeparator(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        string local = Path.TrimEndingDirectorySeparator(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData));
        string installedData = Path.Combine(local, "XASS");
        string installedPreferences = Path.Combine(local, "XASS.Native");

        if (string.IsNullOrEmpty(volume)
            || SamePath(root, Path.TrimEndingDirectorySeparator(volume))
            || SamePath(root, profile)
            || SamePath(root, local)
            || IsSameOrChild(root, installedData)
            || IsSameOrChild(root, installedPreferences))
            throw new InvalidOperationException($"{TestRootVariable} points to a protected or installed XASS location.");
        return root;
    }

    private static bool SamePath(string left, string right) =>
        string.Equals(left, right, StringComparison.OrdinalIgnoreCase);

    private static bool IsSameOrChild(string candidate, string parent)
    {
        string normalizedParent = Path.TrimEndingDirectorySeparator(Path.GetFullPath(parent));
        return SamePath(candidate, normalizedParent)
            || candidate.StartsWith(normalizedParent + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase);
    }

    private static string RootFingerprint(string root)
    {
        byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(root.ToUpperInvariant()));
        return Convert.ToHexString(digest)[..12];
    }
}
