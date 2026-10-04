using System.Globalization;
using System.Text.Json;

namespace Xass.Native.Services;

// Only visual preferences belong here. Never serialize agent or assistant settings.
public sealed record AppearancePreferences(string Theme = "system", string Accent = "system")
{
    public const int SchemaVersion = 1;
    public static AppearancePreferences Defaults => new();

    public AppearancePreferences Normalize() => new(
        Theme is "light" or "dark" ? Theme : "system",
        Accent == "system" ? Accent : AccentPalette.TryParse(Accent, out var rgb)
            ? $"#{rgb:X6}" : "system");
}

public static class AccentPalette
{
    public static bool TryParse(string? value, out uint rgb)
    {
        rgb = 0;
        return value is { Length: 7 } && value[0] == '#' &&
            uint.TryParse(value.AsSpan(1), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out rgb);
    }

    public static double Luminance(uint rgb)
    {
        static double Linear(uint value)
        {
            double channel = value / 255.0;
            return channel <= 0.04045 ? channel / 12.92 : Math.Pow((channel + 0.055) / 1.055, 2.4);
        }
        return 0.2126 * Linear((rgb >> 16) & 255) +
               0.7152 * Linear((rgb >> 8) & 255) + 0.0722 * Linear(rgb & 255);
    }

    public static double Contrast(uint first, uint second)
    {
        double a = Luminance(first), b = Luminance(second);
        return (Math.Max(a, b) + 0.05) / (Math.Min(a, b) + 0.05);
    }

    // One of opaque black and white always exceeds WCAG 4.5:1 for an opaque sRGB fill.
    public static uint TextOn(uint background) => Contrast(background, 0) >= Contrast(background, 0xFFFFFF)
        ? 0u : 0xFFFFFFu;

    public static uint Shift(uint rgb, double amount)
    {
        static uint Channel(uint value, double amount) => (uint)Math.Clamp(
            Math.Round(amount >= 0 ? value + (255 - value) * amount : value * (1 + amount)), 0, 255);
        return (Channel((rgb >> 16) & 255, amount) << 16) |
               (Channel((rgb >> 8) & 255, amount) << 8) | Channel(rgb & 255, amount);
    }
}

public sealed class AppearanceStore
{
    private const int MaxBytes = 4096;
    public string FilePath { get; }
    public AppearanceStore(string? path = null) => FilePath = path ?? Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "XASS.Native", "appearance.json");

    public AppearancePreferences Load(out bool recovered)
    {
        recovered = false;
        try
        {
            if (!File.Exists(FilePath)) return AppearancePreferences.Defaults;
            using var stream = new FileStream(FilePath, FileMode.Open, FileAccess.Read, FileShare.Read);
            if (stream.Length > MaxBytes) throw new InvalidDataException();
            using var document = JsonDocument.Parse(stream);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("version", out var version) ||
                !version.TryGetInt32(out int schema) || schema != AppearancePreferences.SchemaVersion)
                throw new InvalidDataException();
            string ReadString(string key) => root.TryGetProperty(key, out var field) &&
                field.ValueKind == JsonValueKind.String ? field.GetString() ?? "system" : "system";
            var supplied = new AppearancePreferences(ReadString("theme"), ReadString("accent"));
            var normalized = supplied.Normalize();
            recovered = normalized != supplied;
            return normalized;
        }
        catch (Exception error) when (error is IOException or InvalidDataException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            recovered = true;
            return AppearancePreferences.Defaults;
        }
    }

    public void Save(AppearancePreferences preferences)
    {
        var clean = preferences.Normalize();
        string directory = Path.GetDirectoryName(FilePath)!;
        Directory.CreateDirectory(directory);
        string temporary = Path.Combine(directory, $".appearance-{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                JsonSerializer.Serialize(stream, new { version = AppearancePreferences.SchemaVersion,
                    theme = clean.Theme, accent = clean.Accent });
                stream.Flush(flushToDisk: true);
            }
            // Same-directory atomic replacement. A failed write leaves the old settings intact.
            File.Move(temporary, FilePath, overwrite: true);
        }
        finally
        {
            try { File.Delete(temporary); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}
