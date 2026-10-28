using System.Diagnostics;
using System.Text;

namespace Xass.Native.Services;

internal static class AssistantProcessStart
{
    public static ProcessStartInfo Create(string python, bool background)
    {
        string helper = Path.Combine(AppContext.BaseDirectory, "runtime", "XASS.NativeHelper.exe");
        bool bundled = File.Exists(helper);
        string script = Path.Combine(AppContext.BaseDirectory,
            background ? "background_voice_bridge.py" : "assistant_bridge.py");
        if (!bundled)
        {
            if (!Path.IsPathFullyQualified(python) || !File.Exists(python))
                throw new InvalidOperationException("Встроенный помощник отсутствует. Укажите полный путь к Python для помощника.");
            if (!File.Exists(script)) throw new InvalidOperationException("В сборке отсутствует локальный мост помощника.");
        }
        var start = new ProcessStartInfo
        {
            FileName = bundled ? helper : python, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = AppContext.BaseDirectory
        };
        if (bundled)
        {
            start.ArgumentList.Add("--role");
            start.ArgumentList.Add(background ? "listener" : "assistant");
        }
        else
        {
            start.ArgumentList.Add("-I");
            start.ArgumentList.Add("-B");
            start.ArgumentList.Add(script);
        }
        return start;
    }
}
