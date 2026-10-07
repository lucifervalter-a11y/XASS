using Microsoft.UI.Xaml;
using Xass.Native.Services;
namespace Xass.Native;
public partial class App : Application
{
    private MainWindow? window;
    private NativeInstance? instance;
    public App()
    {
        UnhandledException += (_, error) => WriteStartupFailure(error.Exception);
        try
        {
            InitializeComponent();
        }
        catch (Exception error) { WriteStartupFailure(error); throw; }
    }
    private static void WriteStartupFailure(Exception error)
    {
        // Exception messages can contain user data. Keep only code locations.
        try
        {
            string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native");
            Directory.CreateDirectory(directory);
            File.WriteAllText(Path.Combine(directory, "native-failure.txt"),
                $"{DateTimeOffset.UtcNow:o}\n{error.GetType().FullName}\nHRESULT {error.HResult:X8}\n{error.StackTrace}");
        }
        catch { }
    }
    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        instance = new NativeInstance();
        string[] arguments = Environment.GetCommandLineArgs().Skip(1).ToArray();
        if (!instance.Primary)
        {
            try { await instance.ForwardAsync(arguments); }
            catch { /* Never open a competing UI/agent when activation times out. */ }
            instance.Dispose(); instance = null; Exit(); return;
        }
        AppearanceResources.Register(Resources);
        window = new MainWindow();
        window.Closed += (_, _) => { instance?.Dispose(); instance = null; };
        _ = instance.ListenAsync(values => window.DispatcherQueue.TryEnqueue(() => window.AcceptDesktopActivation(values)));
        window.Activate();
    }
}
