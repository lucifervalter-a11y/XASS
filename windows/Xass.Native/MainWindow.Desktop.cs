using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage;
using Windows.Storage.Pickers;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly DesktopHostClient desktopHost = new();
    private readonly Dictionary<string, ScrollViewer> desktopPages = new();
    private readonly DispatcherTimer desktopTimer = new() { Interval = TimeSpan.FromSeconds(5) };
    private readonly SemaphoreSlim desktopStartGate = new(1, 1);
    private WindowsTray? desktopTray;
    private bool desktopReady, desktopPolling, desktopQuitting, desktopPairingUi, desktopRuntimeChanging;
    private readonly DesktopPairingFlow desktopPairingFlow = new();
    private CancellationTokenSource? desktopPairingCancellation;
    private bool desktopPairing => desktopPairingUi || desktopPairingFlow.IsBusy;
    private string desktopPage = "overview", fileRelative = "", archivePath = "";
    private int fileGeneration;
    private TextBlock desktopGreeting = new(), desktopSummary = new(), desktopMetrics = new(), desktopEvents = new();
    private TextBlock processStatus = new(), fileStatus = new(), archiveStatus = new(), archiveJob = new(), updateStatus = new(), pairingStatus = new(), settingsStatus = new(), transcriptStatus = new();
    private ListView processList = new(), fileList = new(), archiveList = new();
    private TextBox pairServer = new(), pairName = new(), ownerName = new(), interval = new(), archiveFolder = new(), archiveLimit = new(), archiveRetention = new(), journal = new();
    private PasswordBox pairCode = new();
    private ComboBox fileRoot = new();
    private CheckBox autoUpdates = new(), transcription = new();
    private Button pairButton = new(), saveSettings = new(), installUpdate = new();
    private readonly NativeUpdateClient nativeUpdates = new();
    private NativeUpdate? availableNativeUpdate;
    private readonly NativeUpdateCoordinator updateCoordinator = new();
    private readonly CheckBox nativeAutomatic = new() { Content = "Автоматически обновлять нативную тестовую версию после закрытия окна в трей" };
    private readonly DispatcherTimer automaticUpdateTimer = new() { Interval = TimeSpan.FromMinutes(30) };
    private bool nativeUpdateRunning;
    private bool nativeUpdateCommitAuthorized;
    private int desktopArchiveMutationPending;
    private CancellationTokenSource? nativeUpdateCancellation;
    private ProgressBar updateProgress = new() { Minimum = 0, Maximum = 100, Visibility = Visibility.Collapsed };
    private readonly string runtimePreferences = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS.Native", "runtime.json");

    private static TextBlock DLabel(string value, double size = 14) => new() { Text = value, FontSize = size, TextWrapping = TextWrapping.Wrap };
    private static string DValue(JsonElement row, string key, string fallback = "") => row.TryGetProperty(key, out var v) && v.ValueKind != JsonValueKind.Null ? v.ToString() : fallback;
    private static StackPanel DStack(params UIElement[] children)
    { var panel = new StackPanel { Spacing = 12 }; foreach (var child in children) panel.Children.Add(child); return panel; }
    private Button DButton(string title, Func<Task> action)
    {
        var button = new Button { Content = title, Margin = new Thickness(0, 0, 8, 8) };
        button.Click += async (_, _) =>
        {
            if (!button.IsEnabled) return;
            button.IsEnabled = false;
            try { await action(); }
            catch (OperationCanceledException) { }
            catch (Exception e) { DesktopError(e); }
            finally { if (!closed) button.IsEnabled = true; }
        };
        return button;
    }
    private static VariableSizedWrapGrid DActions(params UIElement[] controls)
    { var row = new VariableSizedWrapGrid { Orientation = Orientation.Horizontal, MaximumRowsOrColumns = 4 }; foreach (var control in controls) row.Children.Add(control); return row; }
    private StackPanel AddDesktopPage(string tag, string title, Symbol icon)
    {
        var content = new StackPanel { Spacing = 16, MaxWidth = 1000, Padding = new Thickness(0, 0, 0, 28) };
        var scroll = new ScrollViewer { Content = content, Visibility = Visibility.Collapsed, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        desktopPages[tag] = scroll;
        ((Grid)PageLayout.Children.Last()).Children.Add(scroll);
        Navigation.MenuItems.Add(new NavigationViewItem { Content = title, Tag = tag, Icon = new SymbolIcon(icon) });
        return content;
    }

    private void InitializeDesktop()
    {
        InitializeDesktopPaths();
        BuildConnectionPanel(); BuildComputerPanels(); BuildFilesPanel(); BuildArchivePanel(); BuildJournalPanel(); BuildUpdatesPanel(); BuildSettingsPanel();
        Notice.Title = "XASS · нативный тестовый клиент";
        Notice.Message = "Привяжите компьютер или импортируйте файл подключения. Приложение управляет агентом, музыкой и локальными данными.";
        var commands = AddDesktopPage("commands", "Команды", Symbol.More);
        commands.Children.Add(DLabel("Локальные действия", 24));
        commands.Children.Add(DActions(DButton("Проверить связь", CheckDesktopHealthAsync), DButton("Перезапустить агент", RestartDesktopAgentAsync),
            DButton("Проверить обновление", CheckNativeUpdateAsync), DButton("Снимок экранов", ScreenshotDesktopAsync),
            DButton("Показать буфер обмена", ClipboardDesktopAsync), DButton("Заблокировать ПК", LockDesktopAsync)));
        commands.Children.Add(DLabel("Снимок сохраняется только в Pictures\\XASS. Содержимое буфера обмена отображается локально."));
        commands.Children.Add(DButton("Выйти из XASS", QuitDesktopAsync));
        desktopReady = true;
        desktopTimer.Tick += async (_, _) => await PollDesktopAsync();
        desktopTimer.Start();
        automaticUpdateTimer.Tick += async (_, _) => await CheckAutomaticNativeUpdateAsync();
        automaticUpdateTimer.Start();
        RootGrid.Loaded += async (_, _) =>
        {
            try
            {
                await EnsureDesktopHostAsync();
                AcceptDesktopActivation(Environment.GetCommandLineArgs().Skip(1).ToArray());
                await RefreshDesktopAsync();
                await LoadDesktopJournalAsync();
            }
            catch (Exception e) { DesktopError(e); }
        };
        desktopTray = new WindowsTray(WinRT.Interop.WindowNative.GetWindowHandle(this), RestoreDesktop,
            () => DispatcherQueue.TryEnqueue(async () => { try { await CheckDesktopHealthAsync(); } catch (Exception e) { DesktopError(e); } }),
            () => DispatcherQueue.TryEnqueue(StopAllMicrophones),
            () => DispatcherQueue.TryEnqueue(async () => await QuitDesktopAsync()));
        AppWindow.Closing += (_, args) =>
        {
            if (desktopQuitting) return;
            args.Cancel = true;
            StopAllMicrophones();
            if (desktopTray?.Available == true) AppWindow.Hide();
            else if (AppWindow.Presenter is Microsoft.UI.Windowing.OverlappedPresenter presenter) presenter.Minimize();
        };
    }

    private void InitializeDesktopPaths()
    {
        string helper = Path.Combine(AppContext.BaseDirectory, "runtime", "XASS.NativeHelper.exe");
        string source = Path.Combine(AppContext.BaseDirectory, "pc_client");
        if (File.Exists(helper)) PythonPath.Text = helper;
        if (Directory.Exists(source)) SourcePath.Text = source;
        try
        {
            if (File.Exists(runtimePreferences))
            {
                if (new FileInfo(runtimePreferences).Length > 256 * 1024) throw new InvalidDataException();
                using var doc = JsonDocument.Parse(File.ReadAllText(runtimePreferences));
                if (doc.RootElement.ValueKind != JsonValueKind.Object
                    || !doc.RootElement.TryGetProperty("version", out var version)
                    || !version.TryGetInt32(out int schema) || schema != 1) throw new InvalidDataException();
                string savedPython = DValue(doc.RootElement, "python"), savedSource = DValue(doc.RootElement, "source"), savedData = DValue(doc.RootElement, "data");
                if (!File.Exists(helper) && Path.IsPathFullyQualified(savedPython) && File.Exists(savedPython)) PythonPath.Text = savedPython;
                if (!Directory.Exists(source) && Path.IsPathFullyQualified(savedSource) && Directory.Exists(savedSource)) SourcePath.Text = savedSource;
                if (Path.IsPathFullyQualified(savedData)) DataPath.Text = savedData;
            }
        }
        catch (Exception e) when (e is IOException or InvalidDataException or InvalidOperationException or JsonException or UnauthorizedAccessException) { }
        try { Directory.CreateDirectory(DataPath.Text); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        { DesktopError(new InvalidOperationException("Папка данных недоступна. Исправьте путь на странице подключения.")); }
        client.PythonPath = PythonPath.Text; client.SourcePath = SourcePath.Text; client.DataPath = DataPath.Text;
    }
    private async Task SaveDesktopPathsAsync()
    {
        if (desktopPairing || desktopRuntimeChanging) throw new InvalidOperationException("Дождитесь окончания подключения или сохранения путей.");
        desktopRuntimeChanging = true;
        try
        {
            string python = PythonPath.Text.Trim(), source = SourcePath.Text.Trim(), data = DataPath.Text.Trim();
            if (!Path.IsPathFullyQualified(python) || !File.Exists(python) || !Path.IsPathFullyQualified(source) || !Directory.Exists(source)
                || !Path.IsPathFullyQualified(data) || !Directory.Exists(data)) throw new InvalidOperationException("Проверьте абсолютные пути среды и папки данных.");
            if (desktopHost.IsRunning) await desktopHost.CloseAsync();
            client.PythonPath = python; client.SourcePath = source; client.DataPath = data;
            await Task.Run(() =>
            {
                Directory.CreateDirectory(Path.GetDirectoryName(runtimePreferences)!);
                string temp = runtimePreferences + ".tmp";
                File.WriteAllText(temp, JsonSerializer.Serialize(new { version = 1, python, source, data }));
                File.Move(temp, runtimePreferences, true);
            });
            await EnsureDesktopHostAsync(); await RefreshDesktopAsync();
        }
        finally { desktopRuntimeChanging = false; }
    }
    private async Task EnsureDesktopHostAsync()
    {
        await desktopStartGate.WaitAsync(lifetime.Token);
        try { await desktopHost.StartAsync(client.PythonPath, client.SourcePath, client.DataPath, lifetime.Token); }
        finally { desktopStartGate.Release(); }
    }
    private Task<JsonElement> DesktopRequestAsync(string action) => client.RequestAsync(new { action, version = 1 }, lifetime.Token);
    private async Task<JsonElement> DesktopHostRequestAsync(object request)
    { await EnsureDesktopHostAsync(); return await desktopHost.RequestAsync(request, lifetime.Token); }
    private void DesktopError(Exception error)
    {
        if (closed) return;
        Notice.IsOpen = true; Notice.Severity = InfoBarSeverity.Error; Notice.Title = "Действие не завершено";
        Notice.Message = error is InvalidOperationException or TimeoutException ? error.Message : "Проверьте настройки среды, доступ к папкам и состояние сети. Можно повторить действие.";
    }
    private void DesktopNotice(string message)
    { Notice.IsOpen = true; Notice.Title = "XASS"; Notice.Message = message; Notice.Severity = InfoBarSeverity.Informational; }
    private async Task<bool> DesktopConfirmAsync(string title, string message, string accept = "Продолжить")
    {
        var dialog = new ContentDialog { XamlRoot = RootGrid.XamlRoot, Title = title, Content = DLabel(message),
            PrimaryButtonText = accept, CloseButtonText = "Отмена", DefaultButton = ContentDialogButton.Close };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }
    private async Task DesktopMessageAsync(string title, string message)
    { await new ContentDialog { XamlRoot = RootGrid.XamlRoot, Title = title, Content = new ScrollViewer { Content = DLabel(message), MaxHeight = 500 }, CloseButtonText = "Закрыть" }.ShowAsync(); }
    private void SelectDesktopPage(string tag)
    { Navigation.SelectedItem = Navigation.MenuItems.OfType<NavigationViewItem>().FirstOrDefault(item => item.Tag?.ToString() == tag); }
    private async void OnDesktopNavigated(string page)
    {
        desktopPage = page;
        foreach (var pair in desktopPages) pair.Value.Visibility = pair.Key == page ? Visibility.Visible : Visibility.Collapsed;
        if (!desktopReady) return;
        try
        {
            if (page is "overview" or "device") await RefreshDesktopAsync();
            else if (page == "files") await LoadDesktopFilesAsync();
            else if (page == "archive") await LoadDesktopArchiveAsync();
            else if (page == "journal") await LoadDesktopJournalAsync();
            else if (page == "updates") await LoadDesktopUpdatesAsync();
            else if (page == "settings") await LoadDesktopSettingsAsync();
        }
        catch (Exception e) { DesktopError(e); }
    }
    private async Task PollDesktopAsync()
    {
        if (!desktopReady || desktopPolling || closed || !activeWindow) return;
        desktopPolling = true;
        try
        {
            if (desktopPage is "overview" or "device") await RefreshDesktopAsync();
            else if (desktopPage == "settings") transcriptStatus.Text = "Расшифровка песен: " + (await DesktopRequestAsync("desktop_transcription")).ToString();
            else if (desktopPage == "archive" && desktopHost.IsRunning)
            {
                var host = await desktopHost.RequestAsync(new { action = "host_status" }, lifetime.Token);
                var job = host.GetProperty("job"); archiveJob.Text = $"{DValue(job, "message")} · {DValue(job, "progress", "0")}%";
            }
        }
        catch { /* Keep the last view and retry when active; explicit actions show errors. */ }
        finally { desktopPolling = false; }
    }

    private void BuildConnectionPanel()
    {
        var existing = (StackPanel)ConnectionPage.Content;
        pairServer = new TextBox { Header = "Адрес сервера", MaxLength = 2048, PlaceholderText = "https://ваш-сервер" };
        pairName = new TextBox { Header = "Имя компьютера", MaxLength = 128, Text = Environment.MachineName };
        pairCode = new PasswordBox { Header = "Одноразовый код подключения", MaxLength = 64 };
        pairButton = DButton("Подключить компьютер", PairDesktopAsync);
        var form = DStack(DLabel("Подключить XASS", 26), pairServer, pairName, pairCode,
            DActions(pairButton, DButton("Импортировать файл", PickConnectionAsync), DButton("Вставить JSON", PasteConnectionAsync)), pairingStatus);
        form.AllowDrop = true;
        form.DragOver += (_, e) => { if (e.DataView.Contains(StandardDataFormats.StorageItems)) e.AcceptedOperation = DataPackageOperation.Copy; };
        form.Drop += async (_, e) =>
        {
            try
            {
                var items = await e.DataView.GetStorageItemsAsync();
                if (items.Count == 1 && items[0] is StorageFile file) await ImportConnectionAsync(file.Path, null);
            }
            catch (Exception ex) { DesktopError(ex); }
        };
        existing.Children.Insert(0, form);
        existing.Children.Insert(1, DLabel("Можно перетащить .xass, .xass-connect или JSON сюда. Одноразовый код не сохраняется в настройках интерфейса."));
        foreach (var label in existing.Children.OfType<TextBlock>())
            if (label.Text.Contains("Для этой предварительной") || label.Text.Contains("Ключи не копируются")) label.Visibility = Visibility.Collapsed;
        ConnectButton.Visibility = Visibility.Collapsed;
        existing.Children.Add(DButton("Сохранить пути среды", SaveDesktopPathsAsync));
    }
    private FileOpenPicker DesktopOpenPicker(params string[] extensions)
    {
        var picker = new FileOpenPicker(); WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        foreach (string extension in extensions) picker.FileTypeFilter.Add(extension); return picker;
    }
    private Task PickConnectionAsync() => ImportConnectionSourceAsync(async cancellation =>
    {
        var file = await DesktopOpenPicker(".xass", ".xass-connect", ".json").PickSingleFileAsync();
        return file is null ? null : await DesktopPairingFlow.ReadProfileFileAsync(file.Path, cancellation);
    });
    private Task PasteConnectionAsync() => ImportConnectionSourceAsync(async cancellation =>
    {
        var clipboard = Clipboard.GetContent();
        if (!clipboard.Contains(StandardDataFormats.Text)) throw new InvalidOperationException("Буфер не содержит JSON подключения.");
        string value = await clipboard.GetTextAsync();
        cancellation.ThrowIfCancellationRequested();
        return value;
    });
    private Task ImportConnectionAsync(string? path, string? content) => ImportConnectionSourceAsync(async cancellation =>
    {
        if ((path is null) == (content is null)) throw new InvalidOperationException("Выберите один источник подключения.");
        return path is null ? content : await DesktopPairingFlow.ReadProfileFileAsync(path, cancellation);
    });
    private Task ImportConnectionSourceAsync(Func<CancellationToken, Task<string?>> readSource) => RunDesktopPairingAsync(operationCancellation =>
        desktopPairingFlow.ImportAsync(readSource, client.RequestAsync, async (profile, cancellation) =>
        {
            // Preview only in the confirmation dialog. Cancelling preserves the manual form.
            string previewName = DValue(profile, "name");
            if (string.IsNullOrWhiteSpace(previewName)) previewName = Environment.MachineName;
            bool accepted = await DesktopConfirmAsync("Привязать компьютер?",
                $"Сервер: {DValue(profile, "server")}\nКомпьютер: {previewName}\nСрок действия: {DValue(profile, "expires_at")}\nКлюч агента будет сохранён в зашифрованном виде для этой учётной записи Windows.", "Подключить");
            cancellation.ThrowIfCancellationRequested();
            return accepted;
        }, operationCancellation));
    private Task PairDesktopAsync() => RunDesktopPairingAsync(cancellation => desktopPairingFlow.PairManualAsync(
        pairServer.Text, pairName.Text, pairCode.Password, client.RequestAsync, cancellation));
    private async Task RunDesktopPairingAsync(Func<CancellationToken, Task<JsonElement?>> operation)
    {
        if (desktopPairing || desktopQuitting || closed) return;
        if (desktopRuntimeChanging) throw new InvalidOperationException("Дождитесь сохранения путей среды.");
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        desktopPairingCancellation = cancellation;
        desktopPairingUi = true;
        pairButton.IsEnabled = saveSettings.IsEnabled = false;
        pairServer.IsEnabled = pairName.IsEnabled = pairCode.IsEnabled = false;
        pairingStatus.Text = "Подготовка подключения…";
        bool paired = false;
        try
        {
            JsonElement? result = await operation(cancellation.Token);
            if (result is null)
            { pairingStatus.Text = "Подключение отменено. Можно ввести данные вручную или импортировать новый файл."; return; }
            paired = true;
            pairCode.Password = "";
            pairServer.Text = DValue(result.Value, "server"); pairName.Text = DValue(result.Value, "name", Environment.MachineName);
            pairingStatus.Text = "Компьютер подключён. Персональный ключ сохранён зашифрованно.";
            // A completed claim can win a cancellation race. Keep its success, but
            // never restart a background agent after application shutdown began.
            if (closed || desktopQuitting || cancellation.IsCancellationRequested) return;
            await RestartDesktopAgentAsync(); SelectDesktopPage("overview");
        }
        catch (OperationCanceledException)
        { pairingStatus.Text = "Подключение отменено. Можно повторить с новыми данными."; throw; }
        catch
        {
            pairingStatus.Text = paired ? "Компьютер подключён, но агент не удалось перезапустить. Повторите запуск агента."
                : "Не удалось подключиться. Проверьте адрес и срок действия кода. Для импорта выберите файл или JSON заново.";
            throw;
        }
        finally
        {
            desktopPairingCancellation = null;
            desktopPairingUi = false;
            pairButton.IsEnabled = saveSettings.IsEnabled = true;
            pairServer.IsEnabled = pairName.IsEnabled = pairCode.IsEnabled = true;
        }
    }

    private void BuildComputerPanels()
    {
        var overview = (StackPanel)OverviewPage.Content;
        overview.Children.Insert(1, DStack(desktopGreeting, desktopSummary, desktopMetrics,
            DActions(DButton("Mini App", OpenDesktopMiniAppAsync), DButton("Проверить связь", CheckDesktopHealthAsync),
                     DButton("Команды", () => { SelectDesktopPage("commands"); return Task.CompletedTask; })), desktopEvents));
        foreach (var label in overview.Children.OfType<TextBlock>()) if (label.Text.Contains("пока остаются")) label.Visibility = Visibility.Collapsed;
        var device = (StackPanel)((Border)DevicePage.Content).Child;
        device.Children.Add(processStatus); device.Children.Add(DButton("Обновить показатели и процессы", LoadDesktopProcessesAsync));
        processList.MaxHeight = 450; device.Children.Add(processList);
        device.Children.Add(DActions(DButton("Перезапустить агент", RestartDesktopAgentAsync), DButton("Остановить агент", async () =>
        { await DesktopHostRequestAsync(new { action = "host_stop" }); await RefreshDesktopAsync(); }), DButton("Запустить агент", async () =>
        { await DesktopHostRequestAsync(new { action = "host_start" }); await RefreshDesktopAsync(); })));
    }
    private async Task RefreshDesktopAsync()
    {
        var result = await DesktopRequestAsync("desktop_status"); if (closed) return;
        DeviceName.Text = DValue(result, "name", Environment.MachineName); DeviceState.Text = StateLabel(DValue(result, "state"));
        OverviewStatus.Text = DeviceName.Text + " · " + DeviceState.Text;
        if (result.TryGetProperty("legacy_agent", out var legacy) && legacy.GetBoolean())
        { Notice.IsOpen = true; Notice.Severity = InfoBarSeverity.Warning; Notice.Title = "Прежний агент XASS"; Notice.Message = DValue(result, "compatibility_notice"); }
        string greeting = DateTime.Now.Hour < 6 ? "Доброй ночи" : DateTime.Now.Hour < 12 ? "Доброе утро" : DateTime.Now.Hour < 18 ? "Добрый день" : "Добрый вечер";
        desktopGreeting.Text = greeting + (string.IsNullOrWhiteSpace(DValue(result, "owner_name")) ? "!" : ", " + DValue(result, "owner_name") + "!"); desktopGreeting.FontSize = 24;
        desktopSummary.Text = $"{DValue(result, "server")}\nPID: {DValue(result, "pid")} · последний ответ: {DValue(result, "age", "нет")} с · задержка: {DValue(result, "latency_ms")} мс · агент: {DValue(result, "version")}\n{DValue(result, "detail")}";
        if (desktopPage is "overview" or "device")
        {
            var m = await DesktopRequestAsync("desktop_metrics");
            desktopMetrics.Text = $"CPU {DValue(m, "cpu_percent")}% · RAM {DValue(m, "ram_percent")}% · диск {DValue(m, "disk_percent")}%";
            processStatus.Text = $"{DValue(m, "host")} · {DValue(m, "user")}\n{DValue(m, "os")}\nВремя: {DValue(m, "local_time")} · работа: {DValue(m, "uptime")} с\n{desktopMetrics.Text}\nRAM {Bytes(m, "ram_used")} / {Bytes(m, "ram_total")} · диск {Bytes(m, "disk_used")} / {Bytes(m, "disk_total")}";
        }
    }
    private static string Bytes(JsonElement v, string key) => v.TryGetProperty(key, out var n) && n.TryGetInt64(out long value) ? $"{value / 1073741824.0:F2} ГБ" : "—";
    private async Task LoadDesktopProcessesAsync()
    {
        var result = await DesktopRequestAsync("desktop_processes");
        processList.ItemsSource = result.GetProperty("processes").EnumerateArray().Select(p => $"{DValue(p, "name")} · PID {DValue(p, "pid")} · CPU {DValue(p, "cpu")}% · RAM {DValue(p, "memory")}%").ToList();
        await RefreshDesktopAsync();
    }
    private async Task RestartDesktopAgentAsync()
    { await DesktopHostRequestAsync(new { action = "host_restart" }); await RefreshDesktopAsync(); DesktopNotice("Агент перезапущен. Ожидается первый ответ сервера."); }
    private async Task CheckDesktopHealthAsync()
    { var result = await DesktopRequestAsync("desktop_health"); DesktopNotice($"Сервер доступен · {DValue(result, "latency_ms")} мс"); }
    private async Task OpenDesktopMiniAppAsync()
    {
        var result = await DesktopRequestAsync("desktop_miniapp"); string url = DValue(result, "url");
        await Windows.System.Launcher.LaunchUriAsync(new Uri(url));
    }
    private async Task ScreenshotDesktopAsync()
    { var result = await DesktopRequestAsync("desktop_screenshot"); await DesktopMessageAsync("Снимок сохранён локально", DValue(result, "path")); }
    private async Task ClipboardDesktopAsync()
    { var result = await DesktopRequestAsync("desktop_clipboard"); await DesktopMessageAsync("Буфер обмена · первые 1200 символов", DValue(result, "text", "Буфер пуст")); }
    private async Task LockDesktopAsync()
    { if (await DesktopConfirmAsync("Заблокировать Windows?", "Чтобы продолжить работу, потребуется снова войти в Windows.", "Заблокировать")) await client.RequestAsync(new { action = "desktop_lock", confirmed = true }, lifetime.Token); }

    private sealed record NativeFile(string Name, string Type, long Size)
    { public override string ToString() => Type == "directory" ? "📁  " + Name : $"{Name} · {Size:N0} байт"; }
    private void BuildFilesPanel()
    {
        var page = AddDesktopPage("files", "Файлы", Symbol.Folder);
        fileRoot = new ComboBox { Header = "Разрешённая папка", HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var item in new[] { ("desktop", "Рабочий стол"), ("downloads", "Загрузки"), ("documents", "Документы"), ("xass_files", "XASS Files") })
            fileRoot.Items.Add(new ComboBoxItem { Tag = item.Item1, Content = item.Item2 });
        fileRoot.SelectedIndex = 0;
        fileRoot.SelectionChanged += async (_, _) => { fileRelative = ""; try { await LoadDesktopFilesAsync(); } catch (Exception e) { DesktopError(e); } };
        fileList.IsItemClickEnabled = true; fileList.MaxHeight = 600;
        fileList.ItemClick += async (_, e) =>
        {
            if (e.ClickedItem is NativeFile { Type: "directory" } entry && !entry.Name.Contains('/') && !entry.Name.Contains('\\') && entry.Name is not "." and not "..")
            { fileRelative = string.IsNullOrEmpty(fileRelative) ? entry.Name : fileRelative + "/" + entry.Name; try { await LoadDesktopFilesAsync(); } catch (Exception ex) { DesktopError(ex); } }
        };
        page.Children.Add(fileRoot); page.Children.Add(DActions(DButton("Вверх", async () =>
        { int index = fileRelative.LastIndexOf('/'); fileRelative = index < 0 ? "" : fileRelative[..index]; await LoadDesktopFilesAsync(); }), DButton("Обновить / повторить", LoadDesktopFilesAsync)));
        page.Children.Add(fileStatus); page.Children.Add(fileList);
    }
    private async Task LoadDesktopFilesAsync()
    {
        int generation = ++fileGeneration;
        string root = ((ComboBoxItem)fileRoot.SelectedItem).Tag.ToString()!, path = fileRelative;
        fileStatus.Text = "Загрузка…";
        try
        {
            var result = await client.RequestAsync(new { action = "desktop_files", root, path }, lifetime.Token);
            if (generation != fileGeneration || closed) return;
            var rows = result.GetProperty("entries").EnumerateArray().Select(e => new NativeFile(DValue(e, "name"), DValue(e, "type"), e.GetProperty("size").GetInt64())).ToList();
            fileList.ItemsSource = rows;
            fileStatus.Text = DValue(result, "root_label") + "/" + DValue(result, "path") + (rows.Count == 0 ? "\nПапка пуста." : $"\n{rows.Count} элементов")
                + (result.GetProperty("truncated").GetBoolean() ? "\nПоказаны первые 250 элементов. Остальные доступны в Проводнике." : "");
        }
        catch { if (generation == fileGeneration) { fileList.ItemsSource = Array.Empty<NativeFile>(); fileStatus.Text = "Не удалось открыть папку. Нажмите «Обновить / повторить» или «Вверх»."; } throw; }
    }

    private sealed record ArchiveRow(long Id, string Summary) { public override string ToString() => Summary; }
    private void BuildArchivePanel()
    {
        var page = AddDesktopPage("archive", "Архив", Symbol.Library);
        page.Children.Add(archiveStatus);
        page.Children.Add(DActions(DButton("Обновить", LoadDesktopArchiveAsync), DButton("Открыть папку", OpenArchiveFolderAsync),
            DButton("Изменить папку", MoveDesktopArchiveAsync), DButton("Проверить запись", ProbeDesktopArchiveAsync),
            DButton("Очистить медиа", CleanupDesktopArchiveAsync), DButton("Отменить копирование", async () =>
            { await DesktopHostRequestAsync(new { action = "host_archive_cancel" }); archiveJob.Text = "Запрошена отмена копирования…"; })));
        page.Children.Add(archiveJob); page.Children.Add(DLabel("Последние 500 сообщений. Нажмите сообщение, чтобы прочитать текст."));
        archiveList.MaxHeight = 600; archiveList.IsItemClickEnabled = true;
        archiveList.ItemClick += async (_, e) =>
        {
            if (e.ClickedItem is not ArchiveRow row) return;
            try { var value = await client.RequestAsync(new { action = "desktop_archive_detail", id = row.Id }, lifetime.Token); await DesktopMessageAsync("Сообщение архива", DValue(value, "text", "Нет текста")); }
            catch (Exception ex) { DesktopError(ex); }
        };
        page.Children.Add(archiveList);
    }
    private async Task LoadDesktopArchiveAsync()
    {
        var result = await DesktopRequestAsync("desktop_archive"); archivePath = DValue(result, "folder");
        archiveStatus.Text = $"{archivePath}\nКурсор: {DValue(result, "cursor")} · база: {Bytes(result, "database_size")} · медиа: {DValue(result, "media_files")} ({Bytes(result, "media_size")})\nСвободно: {Bytes(result, "free_bytes")} · последняя синхронизация: {DValue(result, "last_sync_at", "ещё нет")}\nПовтор: {DValue(result, "pending_retry")} · ошибки: {DValue(result, "errors")}\n{DValue(result, "last_error")}";
        var messages = await DesktopRequestAsync("desktop_archive_rows");
        archiveList.ItemsSource = messages.GetProperty("rows").EnumerateArray().Select(row => new ArchiveRow(row.GetProperty("id").GetInt64(),
            $"{DValue(row, "message_date")} · {DValue(row, "chat_title", DValue(row, "chat_id"))} · {DValue(row, "direction")}\n{DValue(row, "text")}\nУдалено: {DValue(row, "deleted", "0")} · переслано: {DValue(row, "forwarded_from", "нет")} · ответ: {DValue(row, "reply_to_message_id", "нет")} · медиа: {DValue(row, "media_count", "0")}")).ToList();
    }
    private async Task<string?> PickDesktopFolderAsync()
    {
        var picker = new FolderPicker(); picker.FileTypeFilter.Add("*"); WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        return (await picker.PickSingleFolderAsync())?.Path;
    }
    private async Task OpenArchiveFolderAsync()
    { if (string.IsNullOrWhiteSpace(archivePath)) await LoadDesktopArchiveAsync(); await Windows.System.Launcher.LaunchFolderAsync(await StorageFolder.GetFolderFromPathAsync(archivePath)); }
    private async Task ProbeDesktopArchiveAsync()
    { var result = await client.RequestAsync(new { action = "desktop_archive_probe", path = archivePath }, lifetime.Token); DesktopNotice("Запись доступна. Свободно: " + Bytes(result, "free_bytes")); }
    private async Task MoveDesktopArchiveAsync()
    {
        string? folder = await PickDesktopFolderAsync(); if (folder is null) return;
        var copy = new CheckBox { Content = "Скопировать существующие сообщения и медиа", IsChecked = true };
        var dialog = new ContentDialog { XamlRoot = RootGrid.XamlRoot, Title = "Изменить папку архива?",
            Content = DStack(DLabel($"Новая пустая папка: {folder}\nАгент временно остановится. Исходный архив сохранится на месте. Копирование можно отменить."), copy),
            PrimaryButtonText = "Изменить папку", CloseButtonText = "Отмена", DefaultButton = ContentDialogButton.Close };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        desktopArchiveMutationPending++;
        try { await DesktopHostRequestAsync(new { action = "host_archive_move", path = folder, copy = copy.IsChecked == true, confirmed = true }); }
        finally { desktopArchiveMutationPending--; }
        archiveJob.Text = "Копирование начато. Исходный архив сохранён.";
    }
    private async Task CleanupDesktopArchiveAsync()
    {
        if (!await DesktopConfirmAsync("Безвозвратно удалить локальные медиа?", "Будут удалены локально сохранённые фото, видео и файлы архива. Текст сообщений и центральный сервер останутся без изменений. Удаление нельзя отменить.", "Удалить медиа")) return;
        JsonElement result;
        desktopArchiveMutationPending++;
        try { result = await DesktopHostRequestAsync(new { action = "host_archive_cleanup", confirmed = true }); }
        finally { desktopArchiveMutationPending--; }
        DesktopNotice($"Удалено файлов: {DValue(result, "removed_files")} · освобождено: {Bytes(result, "freed_bytes")}"); await LoadDesktopArchiveAsync();
    }

    private void BuildJournalPanel()
    {
        var page = AddDesktopPage("journal", "Журнал", Symbol.Document);
        journal = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 450, MaxHeight = 700 };
        page.Children.Add(DActions(DButton("Обновить", LoadDesktopJournalAsync), DButton("Сохранить диагностику JSON", ExportDesktopDiagnosticsAsync), DButton("Перезапустить агент", RestartDesktopAgentAsync)));
        page.Children.Add(DLabel("Диагностика не содержит персонального ключа агента. Пути, системные показатели и недавние события остаются в выбранном локальном файле.")); page.Children.Add(journal);
    }
    private async Task LoadDesktopJournalAsync()
    {
        var result = await DesktopRequestAsync("desktop_diagnostics");
        journal.Text = JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true });
        var events = result.GetProperty("logs").EnumerateArray().Select(x => x.GetString() ?? "").TakeLast(5);
        desktopEvents.Text = "Недавние события\n" + string.Join("\n", events);
    }
    private async Task ExportDesktopDiagnosticsAsync()
    {
        var picker = new FileSavePicker { SuggestedFileName = "XASS-diagnostics-" + DateTime.Now.ToString("yyyyMMdd-HHmm") };
        picker.FileTypeChoices.Add("Диагностика JSON", new List<string> { ".json" });
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        var file = await picker.PickSaveFileAsync(); if (file is null) return;
        var result = await DesktopRequestAsync("desktop_diagnostics"); await FileIO.WriteTextAsync(file, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
        DesktopNotice("Диагностика сохранена локально: " + file.Name);
    }

    private void BuildSettingsPanel()
    {
        var page = AddDesktopPage("settings", "Настройки", Symbol.Setting);
        ownerName = new TextBox { Header = "Ваше имя", MaxLength = 128 }; interval = new TextBox { Header = "Интервал телеметрии, секунды (5–86400)", Text = "30" };
        autoUpdates = new CheckBox { Content = "Автоматически устанавливать совместимые обновления", IsThreeState = false };
        transcription = new CheckBox { Content = "Расшифровывать песни на этом ПК (Demucs + Whisper)", IsThreeState = false };
        archiveFolder = new TextBox { Header = "Папка архива (пусто = стандартная)" }; archiveLimit = new TextBox { Header = "Лимит медиа, ГБ (0 = без лимита)", Text = "0" }; archiveRetention = new TextBox { Header = "Хранить медиа, дней (0 = без срока)", Text = "0" };
        saveSettings = DButton("Сохранить и перезапустить агент", SaveDesktopSettingsAsync);
        foreach (var control in new UIElement[] { ownerName, interval, autoUpdates,
            DLabel("Предпочтение старого агента сохраняется отдельно. Нативное автоматическое обновление включается ниже отдельно и использует проверку запуска с резервной копией."), nativeAutomatic, transcription,
            DLabel("При первом включении расшифровки агент скачает отдельную среду и модели. Нужны сеть, свободное место и время."), transcriptStatus,
            DButton("Повторить подготовку расшифровки", RestartDesktopAgentAsync), archiveFolder, DButton("Выбрать папку", async () => { string? folder = await PickDesktopFolderAsync(); if (folder is not null) archiveFolder.Text = folder; }), archiveLimit, archiveRetention,
            DActions(saveSettings, DButton("Перечитать настройки", LoadDesktopSettingsAsync)), settingsStatus }) page.Children.Add(control);
    }
    private async Task LoadDesktopSettingsAsync()
    {
        if (desktopPairing) return;
        var s = await DesktopRequestAsync("desktop_settings");
        nativeAutomatic.IsChecked = NativeUpdatePreferences.Load();
        ownerName.Text = DValue(s, "owner_name"); interval.Text = DValue(s, "interval_sec", "30"); autoUpdates.IsChecked = s.GetProperty("auto_update").GetBoolean();
        transcription.IsChecked = s.GetProperty("transcription_enabled").GetBoolean(); archiveFolder.Text = DValue(s, "archive_folder"); archiveLimit.Text = DValue(s, "archive_max_gb", "0"); archiveRetention.Text = DValue(s, "archive_retention_days", "0");
        transcriptStatus.Text = "Расшифровка песен: " + (await DesktopRequestAsync("desktop_transcription")).ToString();
    }
    private async Task SaveDesktopSettingsAsync()
    {
        if (desktopPairing) throw new InvalidOperationException("Дождитесь окончания подключения.");
        if (!int.TryParse(interval.Text, out int seconds) || seconds < 5 || seconds > 86400 || !int.TryParse(archiveRetention.Text, out int days) || days < 0
            || !double.TryParse(archiveLimit.Text.Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out double gb) || !double.IsFinite(gb) || gb < 0)
            throw new InvalidOperationException("Проверьте интервал, лимит архива и срок хранения.");
        var current = await DesktopRequestAsync("desktop_settings");
        if (transcription.IsChecked == true && !current.GetProperty("transcription_enabled").GetBoolean()
            && !await DesktopConfirmAsync("Включить расшифровку песен?", "Агент сможет скачать среду Demucs + Whisper и модели с официальных источников. Это использует интернет, диск и ресурсы ПК.", "Включить")) return;
        bool automatic = nativeAutomatic.IsChecked == true;
        if (automatic && !NativeUpdatePreferences.Load() && !await DesktopConfirmAsync("Включить нативное автообновление?",
            "После закрытия окна в трей XASS сможет проверять официальный тестовый канал раз в 30 минут. Если музыка не играет и архив не переносится, приложение и агент будут перезапущены для установки. Перед установкой создаётся проверенная резервная копия; при неудачном запуске будет выполнен откат. Микрофоны не включатся автоматически.", "Включить")) return;
        await client.RequestAsync(new { action = "desktop_save_settings", settings = new { owner_name = ownerName.Text.Trim(), interval_sec = seconds,
            auto_update = autoUpdates.IsChecked == true, transcription_enabled = transcription.IsChecked == true, archive_folder = archiveFolder.Text.Trim(), archive_max_gb = gb, archive_retention_days = days } }, lifetime.Token);
        NativeUpdatePreferences.Save(automatic);
        settingsStatus.Text = "Настройки сохранены. Перезапуск агента…"; await RestartDesktopAgentAsync(); settingsStatus.Text = "Настройки сохранены.";
    }

    private void BuildUpdatesPanel()
    {
        var page = AddDesktopPage("updates", "Обновления", Symbol.Download);
        installUpdate = DButton("Скачать и установить", InstallNativeUpdateAsync); installUpdate.IsEnabled = false;
        page.Children.Add(updateStatus); page.Children.Add(DActions(DButton("Проверить тестовое обновление", CheckNativeUpdateAsync), installUpdate,
            DButton("Перезапустить агент", RestartDesktopAgentAsync), DButton("Отменить подготовку", () => { nativeUpdateCancellation?.Cancel(); return Task.CompletedTask; })));
        page.Children.Add(updateProgress);
        page.Children.Add(DLabel("Принимаются только нативные тестовые выпуски официального репозитория XASS. Старый установщик с Tk не заменит этот интерфейс. Перед установкой создаётся проверенная копия программы. Ошибка проверки запуска вызывает восстановление предыдущей версии. Проверка на реальной Windows остаётся обязательной для тестового выпуска."));
    }
    private async Task LoadDesktopUpdatesAsync()
    {
        var result = await DesktopRequestAsync("desktop_updates");
        string nativeResult = "Ещё нет";
        try { nativeResult = await File.ReadAllTextAsync(Path.Combine(NativeUpdateCoordinator.UpdatesRoot, "last-result.json")); } catch (IOException) { }
        updateStatus.Text = $"Версия: {DValue(result, "version")}\nРевизия: {DValue(result, "revision")}\nКанал: native-test\nСохранённое автообновление: {DValue(result, "auto_update")}\nПоследний результат: {DValue(result, "result")}\nСостояние: {DValue(result, "state")}\nНативное обновление: {nativeResult}";
    }
    private string InstalledNativeRevision()
    {
        foreach (string file in new[] { Path.Combine(AppContext.BaseDirectory, "build-info.json"), Path.Combine(client.SourcePath, "build-info.json") })
        { try { using var doc = JsonDocument.Parse(File.ReadAllText(file)); string value = DValue(doc.RootElement, "revision"); if (!string.IsNullOrEmpty(value)) return value; } catch { } }
        return "";
    }
    private async Task CheckNativeUpdateAsync()
    {
        availableNativeUpdate = null; installUpdate.IsEnabled = false;
        updateStatus.Text = "Проверка официальных native-test выпусков…";
        try
        {
            availableNativeUpdate = await nativeUpdates.CheckAsync(InstalledNativeRevision(), lifetime.Token);
            installUpdate.IsEnabled = availableNativeUpdate is not null;
            updateStatus.Text = availableNativeUpdate is null ? "Новых совместимых нативных тестовых выпусков не найдено." : $"Доступна {availableNativeUpdate.Version}\n{availableNativeUpdate.Revision}\n{availableNativeUpdate.Size / 1048576.0:F1} МБ";
            SelectDesktopPage("updates");
        }
        catch (OperationCanceledException) { updateStatus.Text = "Проверка обновления отменена. Можно повторить."; throw; }
        catch (TimeoutException) { updateStatus.Text = "Сервер обновлений не ответил вовремя. Нажмите «Проверить» для повторной попытки."; throw; }
        catch { updateStatus.Text = "Не удалось проверить обновление. Проверьте сеть и повторите."; throw; }
    }
    private async Task InstallNativeUpdateAsync()
    {
        var update = availableNativeUpdate; if (update is null || nativeUpdateRunning) return;
        if (!await DesktopConfirmAsync("Установить тестовое обновление?", $"XASS {update.Version}, ревизия {update.Revision}\nЗагрузка: {update.Size / 1048576.0:F1} МБ. После проверки SHA-256 и резервного копирования XASS, агент и микрофоны завершатся. Если проверка новой версии не пройдёт, предыдущая версия будет восстановлена. Данные сохраняются.", "Скачать и установить")) return;
        await ApplyNativeUpdateAsync(update, automatic: false);
    }
    private bool AutomaticNativeGuardsAllow(CancellationToken token) => NativeAutomaticUpdatePolicy.AllowsShutdown(new(
        Closed: closed, Quitting: desktopQuitting, Pairing: desktopPairing, Enabled: NativeUpdatePreferences.Load(),
        WindowVisible: AppWindow.IsVisible, MusicBusy: musicBusy, MusicState: musicState,
        ArchiveBusy: desktopArchiveMutationPending != 0, SettingsBusy: !saveSettings.IsEnabled,
        AssistantBusy: assistantRequest is not null, CancellationRequested: token.IsCancellationRequested));

    private async Task<bool> FreshAutomaticNativeGuardsAllowAsync(CancellationToken token)
    {
        if (!AutomaticNativeGuardsAllow(token) || !desktopHost.IsRunning) return false;
        var context = MusicContext();
        Task<JsonElement> hostTask = desktopHost.RequestAsync(new { action = "host_status" }, lifetime.Token);
        Task<JsonElement> musicTask = nativeMusic.RequestAsync(new { action = "snapshot" }, context.Python, context.Source, context.Data, lifetime.Token);
        await Task.WhenAll(hostTask, musicTask);
        string archive = DValue((await hostTask).GetProperty("job"), "state");
        string playback = DValue(await musicTask, "state");
        return archive is not ("copying" or "committing" or "cleaning")
            && playback is not ("playing" or "paused" or "loading" or "stopping") && AutomaticNativeGuardsAllow(token);
    }
    private static async Task<bool> IsAutomaticNativeUpdateRejectedAsync(NativeUpdate update, CancellationToken token)
    {
        string? rejected = await NativeAutomaticUpdatePolicy.ReadRejectedStateAsync(
            Path.Combine(NativeUpdateCoordinator.UpdatesRoot, "last-result.json"), token);
        if (rejected is not null) NativeUpdatePreferences.RememberRejected(rejected);
        return NativeUpdatePreferences.IsRejected(update.Sha256);
    }
    private async Task ApplyNativeUpdateAsync(NativeUpdate update, bool automatic)
    {
        if (nativeUpdateRunning) return;
        if (!NativeAutomaticUpdatePolicy.MayAttemptRelease(update.Sha256, automatic, NativeUpdatePreferences.IsRejected))
        { updateStatus.Text = "Этот пакет ранее не прошёл проверку. Автоповтор отключён; доступен явный повтор установки или новый выпуск."; return; }
        nativeUpdateRunning = true; nativeUpdateCommitAuthorized = false;
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        nativeUpdateCancellation = cancel;
        PreparedNativeUpdate? prepared = null;
        try
        {
            updateProgress.Value = 0; updateProgress.Visibility = Visibility.Visible;
            updateStatus.Text = "Загрузка проверяемого установщика…";
            string installer = await nativeUpdates.DownloadAsync(update, new System.Progress<double>(p => updateProgress.Value = p), cancel.Token);
            if (automatic && (!await FreshAutomaticNativeGuardsAllowAsync(cancel.Token) || await IsAutomaticNativeUpdateRejectedAsync(update, cancel.Token)))
            { updateStatus.Text = "Автообновление отложено. XASS продолжает работать."; return; }
            prepared = await updateCoordinator.PrepareAsync(update, installer, new System.Progress<string>(message => updateStatus.Text = message), cancel.Token, automatic);
            if (automatic)
            {
                // Recheck after the long download/backup, including an authoritative
                // host/player read and a final synchronous UI/opt-in check at commit.
                bool committed = await NativeAutomaticUpdatePolicy.FinishPreparationAsync(
                    async () => await FreshAutomaticNativeGuardsAllowAsync(cancel.Token)
                        && !await IsAutomaticNativeUpdateRejectedAsync(update, cancel.Token),
                    () => AutomaticNativeGuardsAllow(cancel.Token),
                    () => NativeUpdateCoordinator.CancelPreparedAsync(prepared!),
                    () => { nativeUpdateCommitAuthorized = true; updateStatus.Text = "Резервная копия проверена. Завершение для установки…"; return QuitDesktopAsync(); });
                prepared = null;
                if (!committed) updateStatus.Text = "Автообновление отложено: состояние изменилось. XASS продолжает работать.";
                return;
            }
            updateStatus.Text = "Резервная копия проверена. Завершение для установки…";
            nativeUpdateCommitAuthorized = true;
            await QuitDesktopAsync(); prepared = null;
        }
        catch (OperationCanceledException)
        {
            if (prepared is not null) await NativeUpdateCoordinator.CancelPreparedAsync(prepared);
            if (!closed) updateStatus.Text = "Подготовка обновления отменена. XASS продолжает работать; можно повторить позже.";
        }
        catch (TimeoutException)
        {
            if (prepared is not null) await NativeUpdateCoordinator.CancelPreparedAsync(prepared);
            updateStatus.Text = "Загрузка или подготовка не завершилась вовремя. Текущая версия сохранена; можно повторить.";
            throw;
        }
        catch
        {
            if (prepared is not null) await NativeUpdateCoordinator.CancelPreparedAsync(prepared);
            updateStatus.Text = "Обновление не началось. Текущая версия сохранена; проверьте состояние и повторите.";
            throw;
        }
        finally
        {
            nativeUpdateCancellation = null; nativeUpdateRunning = false; nativeUpdateCommitAuthorized = false;
            if (!closed) { updateProgress.Value = 0; updateProgress.Visibility = Visibility.Collapsed; }
        }
    }
    private async Task CheckAutomaticNativeUpdateAsync()
    {
        if (nativeUpdateRunning || !AutomaticNativeGuardsAllow(lifetime.Token)) return;
        try
        {
            if (!await FreshAutomaticNativeGuardsAllowAsync(lifetime.Token)) return;
            NativeUpdate? update = await nativeUpdates.CheckAsync(InstalledNativeRevision(), lifetime.Token);
            if (update is null || await IsAutomaticNativeUpdateRejectedAsync(update, lifetime.Token)) return;
            await ApplyNativeUpdateAsync(update, automatic: true);
        }
        catch (OperationCanceledException) { if (!closed) updateStatus.Text = "Автоматическая проверка отменена."; }
        catch (TimeoutException) { if (!closed) updateStatus.Text = "Автоматическая проверка не дождалась сети. Можно проверить обновление вручную или дождаться следующей попытки."; }
        catch { if (!closed) updateStatus.Text = "Автообновление отложено. Проверьте состояние или повторите проверку вручную."; }
    }

    public void AcceptDesktopActivation(string[] arguments)
    {
        if (RootGrid.XamlRoot is null)
        {
            RoutedEventHandler? loaded = null;
            loaded = (_, _) => { RootGrid.Loaded -= loaded; AcceptDesktopActivation(arguments); };
            RootGrid.Loaded += loaded;
            return;
        }
        DispatcherQueue.TryEnqueue(async () =>
        {
            if (arguments.Contains("--native-update-health", StringComparer.Ordinal))
            {
                try
                {
                    await EnsureDesktopHostAsync();
                    await desktopHost.RequestAsync(new { action = "host_status" }, lifetime.Token);
                    await NativeUpdateCoordinator.WriteHealthAcknowledgmentAsync(arguments, InstalledNativeRevision());
                }
                catch { return; }
            }
            bool minimized = arguments.Contains("--minimized", StringComparer.OrdinalIgnoreCase);
            string? file = arguments.FirstOrDefault(a => Path.IsPathFullyQualified(a) && new[] { ".xass", ".xass-connect", ".json" }.Contains(Path.GetExtension(a), StringComparer.OrdinalIgnoreCase));
            if (file is not null)
            {
                RestoreDesktop(); SelectDesktopPage("connection");
                try { await ImportConnectionAsync(file, null); } catch (Exception e) { DesktopError(e); }
            }
            else if (minimized)
            {
                StopAllMicrophones();
                if (desktopTray?.Available == true) AppWindow.Hide();
                else if (AppWindow.Presenter is Microsoft.UI.Windowing.OverlappedPresenter presenter) presenter.Minimize();
            }
            else RestoreDesktop();
        });
    }
    private void RestoreDesktop()
    { AppWindow.Show(); if (AppWindow.Presenter is Microsoft.UI.Windowing.OverlappedPresenter presenter) presenter.Restore(); Activate(); }
    private async Task QuitDesktopAsync()
    {
        if (desktopQuitting) return;
        desktopQuitting = true;
        desktopPairingCancellation?.Cancel();
        if (!nativeUpdateCommitAuthorized)
        {
            nativeUpdateCancellation?.Cancel();
            try { await updateCoordinator.CancelPendingAsync(); }
            catch (Exception error) { desktopQuitting = false; DesktopError(error); return; }
        }
        desktopTimer.Stop(); automaticUpdateTimer.Stop(); StopAllMicrophones(); DisposeVoice(); DisposeDesktopMusic();
        await nativeMusic.DisposeAsync();
        await desktopHost.CloseAsync(); desktopTray?.Dispose(); desktopTray = null;
        lifetime.Cancel(); Close();
    }
    private void DisposeDesktop()
    { desktopTimer.Stop(); automaticUpdateTimer.Stop(); desktopTray?.Dispose(); desktopTray = null; _ = desktopHost.CloseAsync(); }
}
