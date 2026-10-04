using System.Text.Json;
namespace Xass.Native.Services;

public static class NativeUpdatePreferences
{
    private static string FilePath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "native-updater.json");
    public static bool Load()
    {
        try
        {
            using var data = JsonDocument.Parse(File.ReadAllText(FilePath));
            return data.RootElement.GetProperty("schema").GetInt32() == 1 && data.RootElement.GetProperty("automatic_enabled").GetBoolean();
        }
        catch { return false; } // Legacy auto-update preferences never silently opt in.
    }
    public static void Save(bool enabled)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath + ".tmp", JsonSerializer.Serialize(new { schema = 1, automatic_enabled = enabled }));
        File.Move(FilePath + ".tmp", FilePath, true);
    }
}
