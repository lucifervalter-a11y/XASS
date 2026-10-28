using System.Collections.ObjectModel;

namespace Xass.Native.Services;

// Canonical native copy of pc_client/themes.py. Keeping this as a pure .NET
// catalog makes normalization and persistence testable without WinUI.
public sealed record AppearanceThemePalette(
    string Name,
    string Background,
    string Card,
    string Hover,
    string Line,
    string Sidebar,
    string Field,
    string Well,
    string Note,
    string Text,
    string Muted,
    string Accent,
    string AccentSoft,
    string Lilac,
    string Green,
    string Amber,
    string Red,
    string SidebarCard,
    string SidebarBorder,
    string NavigationForeground,
    string NavigationActiveBackground,
    string NavigationSelectedBackground,
    uint DwmCaption,
    uint DwmBorder,
    uint DwmText,
    string Backdrop);

public static class AppearanceThemeCatalog
{
    public const string Classic = "classic";
    public const string Cyber = "cyber";
    public const string Tokyo = "tokyo";
    public const string Nordic = "nordic";
    public const string Matrix = "matrix";

    private static readonly IReadOnlyDictionary<string, AppearanceThemePalette> themes =
        new ReadOnlyDictionary<string, AppearanceThemePalette>(
            new Dictionary<string, AppearanceThemePalette>(StringComparer.OrdinalIgnoreCase)
            {
                [Classic] = new(
                    "Classic", "#202022", "#2b2b2f", "#34343a", "#43434f", "#141418",
                    "#1a1a1e", "#35353f", "#1a1a1e", "#f5f5f7", "#b1b1bb",
                    "#829cff", "#9aafff", "#c495f4", "#61c554", "#efb65c", "#f36b76",
                    "#1c1c24", "#2e2e3a", "#a3a3b0", "#2a2a34", "#24242e",
                    0x00181414, 0x003a2e2e, 0x00f7f5f5, "mica_alt"),
                [Cyber] = new(
                    "Cyber", "#0d1017", "#161b26", "#1f2738", "#2d374d", "#090c12",
                    "#121620", "#1c2333", "#121620", "#f0f6fc", "#8b949e",
                    "#00f0ff", "#5ce1e6", "#e056fd", "#00ff9f", "#fbc531", "#ff4757",
                    "#121622", "#232a3d", "#7d8590", "#1f2738", "#1a2232",
                    0x00120c09, 0x003d2a23, 0x00fcf6f0, "acrylic"),
                [Tokyo] = new(
                    "Tokyo", "#1a1b26", "#24283b", "#2f354f", "#414868", "#16161e",
                    "#1f2335", "#292e42", "#1f2335", "#c0caf5", "#7aa2f7",
                    "#bb9af7", "#cfbbf9", "#b4f9f8", "#9ece6a", "#e0af68", "#f7768e",
                    "#1c1e2b", "#343b59", "#7982a9", "#2a2f47", "#23273c",
                    0x001e1616, 0x00593b34, 0x00f5cac0, "mica"),
                [Nordic] = new(
                    "Nordic", "#242933", "#2e3440", "#3b4252", "#434c5e", "#1e222a",
                    "#282d38", "#3b4252", "#282d38", "#eceff4", "#d8dee9",
                    "#88c0d0", "#81a1c1", "#b48ead", "#a3be8c", "#ebcb8b", "#bf616a",
                    "#272c37", "#39404f", "#949ba8", "#343b49", "#2c3340",
                    0x002a221e, 0x004f4039, 0x00f4effc, "mica"),
                [Matrix] = new(
                    "Matrix", "#0a0f0d", "#101814", "#18241e", "#21332a", "#060907",
                    "#0d1410", "#16221b", "#0d1410", "#e0f8eb", "#6bb286",
                    "#00ff66", "#33ff88", "#66ffcc", "#00e676", "#ffd54f", "#ff5252",
                    "#0e1511", "#1c2b23", "#5d8a6f", "#16231b", "#121c16",
                    0x00070906, 0x00232b1c, 0x00ebf8e0, "acrylic"),
            });

    private static readonly IReadOnlyDictionary<string, string> aliases =
        new ReadOnlyDictionary<string, string>(new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["classic"] = Classic,
            ["default"] = Classic,
            ["xass"] = Classic,
            ["cyber"] = Cyber,
            ["cyberpunk"] = Cyber,
            ["neon"] = Cyber,
            ["tokyo"] = Tokyo,
            ["tokyo_night"] = Tokyo,
            ["tokyonight"] = Tokyo,
            ["nordic"] = Nordic,
            ["nord"] = Nordic,
            ["matrix"] = Matrix,
            ["hacker"] = Matrix,
            ["terminal"] = Matrix,
        });

    public static IReadOnlyList<string> PresetNames { get; } =
        Array.AsReadOnly(new[] { Classic, Cyber, Tokyo, Nordic, Matrix });

    public static string NormalizeThemeName(string? candidate)
    {
        string raw = (candidate ?? "").Trim().ToLowerInvariant();
        // `dark` is intentionally reserved for the existing WinUI Dark mode,
        // even though the legacy PC client used it as a Classic alias.
        if (raw is "system" or "light" or "dark") return raw;
        return aliases.TryGetValue(raw, out string? canonical) ? canonical : "system";
    }

    public static bool IsPreset(string? candidate) =>
        themes.ContainsKey(NormalizeThemeName(candidate));

    public static bool TryGetPalette(string? candidate, out AppearanceThemePalette palette)
    {
        if (themes.TryGetValue(NormalizeThemeName(candidate), out AppearanceThemePalette? found))
        {
            palette = found;
            return true;
        }
        palette = null!;
        return false;
    }
}
