using Xass.Native.Services;

static void Require(bool condition, string label)
{
    if (!condition) throw new InvalidOperationException(label);
}

Require(new AppearancePreferences("invalid", "#xyzxyz").Normalize() == AppearancePreferences.Defaults, "invalid values");
Require(new AppearancePreferences("dark", "#aBcDef").Normalize() == new AppearancePreferences("dark", "#ABCDEF"), "hex normalization");
foreach (string? bad in new string?[] { null, "", "#123", "#80112233", "red", "#GG0000", "112233" })
    Require(!AccentPalette.TryParse(bad, out _), "reject non-opaque/non-hex color");
for (uint r = 0; r <= 255; r += 17)
for (uint g = 0; g <= 255; g += 17)
for (uint b = 0; b <= 255; b += 17)
foreach (double shift in new[] { 0.0, -0.08, -0.16 })
{
    uint background = AccentPalette.Shift((r << 16) | (g << 8) | b, shift);
    Require(AccentPalette.Contrast(background, AccentPalette.TextOn(background)) >= 4.5, "text contrast");
    foreach (uint surface in new[] { 0x202020u, 0xFFFFFFu, 0x000000u })
    {
        uint readable = AccentPalette.ReadableAccent(background, surface);
        Require(AccentPalette.Contrast(readable, surface) >= 4.5, "accent text contrast");
        if (AccentPalette.Contrast(background, surface) >= 4.5)
            Require(readable == background, "keep a readable user color");
    }
}
string directory = Path.Combine(Path.GetTempPath(), "xass-appearance-tests-" + Guid.NewGuid().ToString("N"));
try
{
    string path = Path.Combine(directory, "appearance.json");
    var store = new AppearanceStore(path);
    Require(store.Load(out bool recovered) == AppearancePreferences.Defaults && !recovered, "missing file");
    var expected = new AppearancePreferences("light", "#123ABC");
    store.Save(expected);
    Require(store.Load(out recovered) == expected && !recovered, "saved round trip");
    var json = System.Text.Json.JsonDocument.Parse(File.ReadAllText(path));
    Require(json.RootElement.EnumerateObject().Select(p => p.Name).Order().SequenceEqual(new[] { "accent", "theme", "version" }), "allowlisted fields");
    File.WriteAllText(path, "broken json");
    Require(store.Load(out recovered) == AppearancePreferences.Defaults && recovered, "corrupt file");
    Require(File.ReadAllText(path) == "broken json", "reading never overwrites corruption");
    File.WriteAllText(path, "{\"version\":99,\"theme\":\"dark\"}");
    Require(store.Load(out recovered) == AppearancePreferences.Defaults && recovered, "future schema");
    File.WriteAllText(path, new string('a', 4097));
    Require(store.Load(out recovered) == AppearancePreferences.Defaults && recovered, "oversized file");
    store.Save(expected);
    store.Save(AppearancePreferences.Defaults);
    Require(store.Load(out recovered) == AppearancePreferences.Defaults && !recovered, "restore defaults");
    Require(Directory.GetFiles(directory).Length == 1, "temporary file cleanup");
}
finally { if (Directory.Exists(directory)) Directory.Delete(directory, recursive: true); }
Console.WriteLine("Appearance tests passed: parsing, 12,288 fill + 36,864 accent text contrast cases, persistence, corruption, restore.");
