using System.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly InfoBar xassAiStatus = new() { IsOpen = true, IsClosable = false };
    private readonly TextBox xassAiTranscript = new()
    {
        AcceptsReturn = true,
        IsReadOnly = true,
        MinHeight = 300,
        MaxHeight = 560,
        TextWrapping = TextWrapping.Wrap,
        PlaceholderText = "Здесь появится диалог с XASS AI."
    };
    private readonly TextBox xassAiPrompt = new()
    {
        AcceptsReturn = true,
        Header = "Запрос",
        MaxLength = 64 * 1024,
        MinHeight = 96,
        TextWrapping = TextWrapping.Wrap,
        PlaceholderText = "Что нужно сделать или узнать?"
    };
    private readonly Button xassAiCheck = new() { Content = "Проверить доступность" };
    private readonly Button xassAiSend = new() { Content = "Отправить", IsEnabled = false };
    private readonly Button xassAiCancel = new() { Content = "Отменить", IsEnabled = false };
    private XassAiProtocolClient? xassAiProtocol;
    private XassAiOwnedRuntime? xassAiRuntime;
    private CancellationTokenSource? xassAiRequest;
    private bool xassAiBusy;

    private void InitializeXassAi()
    {
        StackPanel page = AddDesktopPage("xass-ai", "XASS AI", Symbol.Message);
        page.Children.Add(DLabel(
            "Локальный агент внутри XASS. Подключение разрешено только к процессу, который запустил и которым владеет сам XASS; чужие процессы не используются."));
        page.Children.Add(xassAiStatus);
        page.Children.Add(xassAiTranscript);
        page.Children.Add(xassAiPrompt);
        page.Children.Add(DActions(xassAiSend, xassAiCancel, xassAiCheck));
        page.Children.Add(DLabel(
            "Доступ к сети, учётным данным и настройкам не изменяется этой страницей. Если защищённый локальный runtime недоступен, запрос не отправляется."));

        ScrollViewer.SetVerticalScrollBarVisibility(xassAiTranscript, ScrollBarVisibility.Auto);
        ScrollViewer.SetVerticalScrollBarVisibility(xassAiPrompt, ScrollBarVisibility.Auto);
        AutomationProperties.SetName(xassAiTranscript, "Диалог XASS AI");
        AutomationProperties.SetName(xassAiPrompt, "Запрос XASS AI");
        AutomationProperties.SetName(xassAiSend, "Отправить запрос XASS AI");
        AutomationProperties.SetName(xassAiCancel, "Отменить запрос XASS AI");
        AutomationProperties.SetName(xassAiCheck, "Проверить доступность XASS AI");

        xassAiCheck.Click += async (_, _) => await RefreshXassAiAvailabilityAsync();
        xassAiSend.Click += async (_, _) => await SendXassAiAsync();
        xassAiCancel.Click += (_, _) => xassAiRequest?.Cancel();
        xassAiPrompt.TextChanged += (_, _) =>
            xassAiSend.IsEnabled = !xassAiBusy
                && xassAiProtocol?.IsConnected == true
                && !string.IsNullOrWhiteSpace(xassAiPrompt.Text);
        ApplyXassAiAvailability(XassAiInstallationProbe.Probe());
    }

    private async Task RefreshXassAiAvailabilityAsync()
    {
        if (closed || xassAiBusy) return;
        xassAiCheck.IsEnabled = false;
        try
        {
            if (xassAiProtocol?.IsConnected == true)
            {
                ApplyXassAiAvailability(new XassAiAvailability(
                    XassAiAvailabilityKind.Ready,
                    "Защищённый локальный runtime готов. Запросы отправляются напрямую через XASS AI."));
                return;
            }
            XassAiAvailability availability = await Task.Run(XassAiInstallationProbe.Probe);
            if (!closed) ApplyXassAiAvailability(availability);
        }
        finally
        {
            if (!closed) xassAiCheck.IsEnabled = true;
        }
    }

    private void ApplyXassAiAvailability(XassAiAvailability availability)
    {
        bool ready = availability.Kind == XassAiAvailabilityKind.Ready && xassAiProtocol?.IsConnected == true;
        xassAiStatus.Title = availability.Kind switch
        {
            XassAiAvailabilityKind.Ready => "XASS AI готов",
            XassAiAvailabilityKind.InstalledUnavailable => "XASS AI установлен, но недоступен",
            XassAiAvailabilityKind.TestMode => "XASS AI отключён для теста",
            _ => "XASS AI не установлен"
        };
        xassAiStatus.Message = availability.Message;
        xassAiStatus.Severity = ready
            ? InfoBarSeverity.Success
            : availability.Kind == XassAiAvailabilityKind.NotInstalled
                ? InfoBarSeverity.Error
                : InfoBarSeverity.Warning;
        xassAiPrompt.IsEnabled = ready && !xassAiBusy;
        xassAiSend.IsEnabled = ready && !xassAiBusy && !string.IsNullOrWhiteSpace(xassAiPrompt.Text);
        xassAiCancel.IsEnabled = xassAiBusy;
    }

    /// <summary>
    /// Future native launcher handoff. The runtime object can only wrap the Process instance
    /// created by XASS and carries its private token in memory; this page never discovers or
    /// attaches to an already-running process.
    /// </summary>
    internal async Task BindOwnedXassAiRuntimeAsync(XassAiOwnedRuntime runtime, CancellationToken cancellationToken)
    {
        if (NativeRuntimeMode.IsTestMode)
            throw new InvalidOperationException("XASS AI is disabled in isolated test mode.");
        if (xassAiRuntime is not null || xassAiProtocol is not null)
            throw new InvalidOperationException("XASS AI runtime is already bound.");

        XassAiProtocolClient? protocol = null;
        try
        {
            XassAiRuntimeEndpoint endpoint = await runtime.WaitForEndpointAsync(cancellationToken);
            protocol = new XassAiProtocolClient(endpoint);
            await protocol.ConnectAsync(cancellationToken);
            xassAiRuntime = runtime;
            xassAiProtocol = protocol;
            ApplyXassAiAvailability(new XassAiAvailability(
                XassAiAvailabilityKind.Ready,
                "Защищённый локальный runtime готов. Запросы отправляются напрямую через XASS AI."));
        }
        catch
        {
            if (protocol is not null) await protocol.DisposeAsync();
            await runtime.DisposeAsync();
            ApplyXassAiAvailability(XassAiInstallationProbe.Probe());
            throw;
        }
    }

    private async Task SendXassAiAsync()
    {
        if (closed || xassAiBusy || xassAiProtocol?.IsConnected != true) return;
        string prompt = xassAiPrompt.Text.Trim();
        if (prompt.Length == 0) return;

        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        xassAiRequest = cancellation;
        xassAiBusy = true;
        xassAiPrompt.IsEnabled = xassAiSend.IsEnabled = xassAiCheck.IsEnabled = false;
        xassAiCancel.IsEnabled = true;
        xassAiTranscript.Text = "Вы:\n" + prompt + "\n\nXASS AI:\n";

        var stream = new XassAiStreamBuffer();
        var flush = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(75) };
        flush.Tick += (_, _) => FlushXassAiStream(stream);
        flush.Start();
        try
        {
            await xassAiProtocol.AskAsync(prompt, stream, cancellation.Token);
            FlushXassAiStream(stream);
            xassAiPrompt.Text = string.Empty;
        }
        catch (XassAiCancellationException cancellationResult)
        {
            FlushXassAiStream(stream);
            xassAiTranscript.Text += cancellationResult.Confirmed
                ? "\n\nXASS AI подтвердил остановку запроса."
                : "\n\nОтмена отправлена, но остановка не подтверждена. Состояние запроса неизвестно.";
        }
        catch (OperationCanceledException)
        {
            FlushXassAiStream(stream);
            xassAiTranscript.Text += "\n\nЗапрос отменён до отправки.";
        }
        catch (Exception error) when (error is XassAiProtocolException or InvalidOperationException or TimeoutException)
        {
            FlushXassAiStream(stream);
            xassAiTranscript.Text += "\n\n" + error.Message;
            await RefreshXassAiAvailabilityAsync();
        }
        finally
        {
            flush.Stop();
            xassAiRequest = null;
            xassAiBusy = false;
            if (!closed)
            {
                xassAiCheck.IsEnabled = true;
                await RefreshXassAiAvailabilityAsync();
            }
        }
    }

    private void FlushXassAiStream(XassAiStreamBuffer stream)
    {
        string delta = stream.Take();
        if (delta.Length > 0) xassAiTranscript.Text += delta;
    }

    private async void OnXassAiNavigated(string page)
    {
        if (page == "xass-ai") await RefreshXassAiAvailabilityAsync();
    }

    private void DisposeXassAi()
    {
        xassAiRequest?.Cancel();
        XassAiProtocolClient? protocol = xassAiProtocol;
        XassAiOwnedRuntime? runtime = xassAiRuntime;
        xassAiProtocol = null;
        xassAiRuntime = null;
        if (protocol is not null) _ = protocol.DisposeAsync();
        if (runtime is not null) _ = runtime.DisposeAsync();
    }

    private sealed class XassAiStreamBuffer : IProgress<string>
    {
        private readonly object gate = new();
        private readonly StringBuilder pending = new();

        public void Report(string value)
        {
            if (string.IsNullOrEmpty(value)) return;
            lock (gate) pending.Append(value);
        }

        public string Take()
        {
            lock (gate)
            {
                if (pending.Length == 0) return string.Empty;
                string value = pending.ToString();
                pending.Clear();
                return value;
            }
        }
    }
}
