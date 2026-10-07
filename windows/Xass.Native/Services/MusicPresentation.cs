namespace Xass.Native.Services;

public sealed record CoverPalette(uint Primary, uint Secondary)
{
    public static CoverPalette? FromBgra(byte[] pixels)
    {
        if (pixels.Length == 0 || pixels.Length % 4 != 0 || pixels.Length > 256 * 256 * 4) return null;
        var bins = new Dictionary<uint, (long R, long G, long B, int Count)>();
        for (int i = 0; i < pixels.Length; i += 4)
        {
            if (pixels[i + 3] < 128) continue;
            int b = pixels[i], g = pixels[i + 1], r = pixels[i + 2];
            uint bin = (uint)(((r / 32) << 6) | ((g / 32) << 3) | (b / 32));
            bins.TryGetValue(bin, out var value);
            bins[bin] = (value.R + r, value.G + g, value.B + b, value.Count + 1);
        }
        if (bins.Count == 0) return null;
        var colors = bins.Values.OrderByDescending(v => v.Count).Select(v =>
            (uint)(((v.R / v.Count) << 16) | ((v.G / v.Count) << 8) | (v.B / v.Count))).ToList();
        uint first = colors[0], second = colors.FirstOrDefault(c => Distance(first, c) > 80, first);
        return new(first, second);
    }
    private static int Distance(uint a, uint b) => Math.Abs((int)(a >> 16) - (int)(b >> 16)) +
        Math.Abs((int)((a >> 8) & 255) - (int)((b >> 8) & 255)) + Math.Abs((int)(a & 255) - (int)(b & 255));
    public static uint Blend(uint color, uint surface, double strength)
    {
        strength = Math.Clamp(strength, 0, 1);
        uint Channel(int shift) => (uint)Math.Round(((color >> shift) & 255) * strength + ((surface >> shift) & 255) * (1 - strength));
        return (Channel(16) << 16) | (Channel(8) << 8) | Channel(0);
    }
}

public sealed record MusicHistoryEntry(string Title, string Artist, DateTimeOffset Started)
{
    public string Display => $"{Title}\n{(string.IsNullOrWhiteSpace(Artist) ? "" : Artist + " · ")}{Started:HH:mm}";
}
public sealed class MusicSessionHistory
{
    private string previous = "";
    public List<MusicHistoryEntry> Entries { get; } = new();
    public bool Observe(string state, string identity, string title, string artist, DateTimeOffset now)
    {
        if (state != "playing" || string.IsNullOrWhiteSpace(identity) || string.IsNullOrWhiteSpace(title) || identity == previous) return false;
        previous = identity;
        Entries.Insert(0, new(title, artist, now));
        if (Entries.Count > 50) Entries.RemoveAt(Entries.Count - 1);
        return true;
    }
}
