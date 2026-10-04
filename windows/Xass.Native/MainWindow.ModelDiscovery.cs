using Microsoft.UI.Xaml.Controls;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private async Task DiscoverAssistantModelAsync()
    {
        // A configured value wins even if it is invalid. Never silently replace the user's choice.
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("XASS_ASSISTANT_MODEL"))
            || !string.IsNullOrWhiteSpace(AssistantModelPath.Text)) return;
        // Runtime preferences are already loaded; include only their known private cache.
        string configuredData = client.DataPath;
        var roots = WhisperModelDiscovery.GetCacheRoots(name => name == "XASS_DATA_ROOT"
            && Path.IsPathFullyQualified(configuredData) ? configuredData : Environment.GetEnvironmentVariable(name));
        bool edited = false;
        void MarkEdited(object sender, TextChangedEventArgs args) => edited = true;
        AssistantModelPath.TextChanged += MarkEdited;
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        cancellation.CancelAfter(TimeSpan.FromSeconds(3));
        try
        {
            var discovered = await Task.Run(() => WhisperModelDiscovery.FindExisting(roots, cancellation.Token), cancellation.Token)
                .WaitAsync(cancellation.Token);
            // Editing then clearing the field is still an explicit manual choice. Also don't
            // change settings under a microphone operation that began while discovery ran.
            if (closed || edited || assistantRequest is not null || BackgroundListening.IsOn
                || !string.IsNullOrWhiteSpace(AssistantModelPath.Text) || discovered is null) return;
            AssistantModelPath.Text = discovered.Path;
        }
        catch (OperationCanceledException) { } // Close, deadline, or cancelled initialization.
        catch (Exception error) when (error is IOException or UnauthorizedAccessException
            or System.Security.SecurityException) { } // Manual path entry remains available.
        finally { AssistantModelPath.TextChanged -= MarkEdited; }
    }
}
