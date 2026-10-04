using Microsoft.UI.Xaml;
using Xass.Native.Services;
namespace Xass.Native;
public partial class App : Application
{
    private MainWindow? window;
    private NativeInstance? instance;
    public App() => InitializeComponent();
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
        window = new MainWindow();
        window.Closed += (_, _) => { instance?.Dispose(); instance = null; };
        _ = instance.ListenAsync(values => window.DispatcherQueue.TryEnqueue(() => window.AcceptDesktopActivation(values)));
        window.Activate();
    }
}
