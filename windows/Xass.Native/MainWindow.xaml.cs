using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Xass.Native.Services;

namespace Xass.Native;

public sealed record Track(int Id, string Title, string Artist, string Album)
{
    public string Detail => string.Join(" · ", new[] { Artist, Album }.Where(value => !string.IsNullOrWhiteSpace(value)));
}

public sealed partial class MainWindow : Window
{
    private readonly AgentClient client = new();
    private readonly CancellationTokenSource lifetime = new();
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(5) };
    private readonly Stack<int> previousOffsets = new();
    private int offset;
    private int? nextOffset;
    private string query = "";
    private string playerState = "offline";
    private bool busy;
    private bool connected;
    private bool closed;
    private bool seekDirty;
    private bool updatingSeek;
    private bool volumeDirty;
    private bool updatingVolume;
    private string lastTrack = "";
    private string? pendingSearch;
    private bool activeWindow = true;
    private readonly AssistantClient assistantClient = new();
    private CancellationTokenSource? assistantRequest;
    private JsonElement? assistantPlan;
    private string assistantPlannedText = "";

    public MainWindow()
    {
        InitializeComponent();
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1180, 860));
        if (Microsoft.UI.Composition.SystemBackdrops.MicaController.IsSupported())
        {
            SystemBackdrop = new MicaBackdrop();
            RootGrid.Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent);
        }
        // The root retains its opaque themed background without Mica support.
        RootGrid.SizeChanged += (_, _) => UpdatePagePadding();
        Navigation.DisplayModeChanged += (_, _) => UpdatePagePadding();
        TrackList.ItemsSource = Array.Empty<Track>();
        MusicPage.SizeChanged += (_, args) => PlayerScroller.MaxHeight = Math.Clamp(args.NewSize.Height * 0.45, 100, 260);
        DataPath.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XASS");
        Navigation.SelectedItem = Navigation.MenuItems[0];
        SeekSlider.ValueChanged += (_, _) => { if (!updatingSeek) seekDirty = true; };
        VolumeSlider.ValueChanged += (_, _) => { if (!updatingVolume) volumeDirty = true; };
        timer.Tick += async (_, _) =>
        {
            if (connected && !busy && activeWindow) await RunAsync(RefreshAsync, quiet: true);
        };
        Activated += async (_, args) =>
        {
            activeWindow = args.WindowActivationState != WindowActivationState.Deactivated;
            if (activeWindow && connected && !busy && !closed) await RunAsync(RefreshAsync, quiet: true);
        };
        timer.Start();
        Closed += (_, _) =>
        {
            DisposeDesktop();
            DisposeDesktopMusic();
            closed = true;
            timer.Stop();
            DisposeVoice();
            DisposeAppearance();
            lifetime.Cancel();
            pendingSearch = null;
            Application.Current.Exit();
        };
        UpdateControls();
        InitializeAssistant();
        InitializeAssistantPresentation();
        InitializeVoice();
        InitializeDesktop();
        _ = DiscoverAssistantModelAsync();
        InitializeDesktopMusic();
        InitializeAppearance();
    }

    private void UpdatePagePadding() => PageLayout.Padding = assistantCompact ? new Thickness(16) : Navigation.DisplayMode == NavigationViewDisplayMode.Minimal
        ? new Thickness(16, 52, 16, 16) : RootGrid.ActualWidth < 760 ? new Thickness(16) : new Thickness(28);

    private void Navigate(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is not NavigationViewItem item || OverviewPage is null) return;
        string page = item.Tag?.ToString() ?? "overview";
        if (assistantCompact && page != "assistant") AssistantCompactClick(this, new RoutedEventArgs());
        OverviewPage.Visibility = page == "overview" ? Visibility.Visible : Visibility.Collapsed;
        DevicePage.Visibility = page == "device" ? Visibility.Visible : Visibility.Collapsed;
        MusicPage.Visibility = page == "music" ? Visibility.Visible : Visibility.Collapsed;
        ConnectionPage.Visibility = page == "connection" ? Visibility.Visible : Visibility.Collapsed;
        AssistantPage.Visibility = page == "assistant" ? Visibility.Visible : Visibility.Collapsed;
        AppearancePage.Visibility = page == "appearance" ? Visibility.Visible : Visibility.Collapsed;
        PageTitle.Text = item.Content?.ToString() ?? "XASS";
        PageTitle.Visibility = page is "music" or "assistant" ? Visibility.Collapsed : Visibility.Visible;
        OnDesktopNavigated(page);
        OnDesktopMusicNavigated(page);
    }

    private void InitializeAssistant()
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string roaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        AssistantPython.Text = Environment.GetEnvironmentVariable("XASS_ASSISTANT_PYTHON")
            ?? Path.Combine(local, "XASS", "transcription", "python", "python.exe");
        AssistantModelPath.Text = Environment.GetEnvironmentVariable("XASS_ASSISTANT_MODEL") ?? "";
        string launcher = Path.Combine(roaming, ".minecraft", "TLauncher.exe");
        AssistantLauncher.Text = File.Exists(launcher) ? launcher : Path.Combine(roaming, ".tlauncher", "TLauncher.exe");
        AssistantLauncherProperties.Text = Path.Combine(roaming, ".tlauncher", "tlauncher-2.0.properties");
        AssistantMinecraft.Text = Path.Combine(roaming, ".minecraft");
        foreach (var field in new[] { AssistantText, AssistantPython, AssistantModelPath, AssistantLauncher,
                                      AssistantMinecraft, AssistantLauncherProperties, AssistantChannel, AssistantOllamaModel })
            field.TextChanged += (_, _) => { InvalidateAssistantPlan(); AssistantDraftChanged(); };
        AssistantPlanner.SelectionChanged += (_, _) => { InvalidateAssistantPlan(); AssistantDraftChanged(); };
    }

    private void InvalidateAssistantPlan()
    {
        assistantPlan = null;
        AssistantPlanCard.Visibility = Visibility.Collapsed;
        if (assistantRequest is null) SetAssistantStage("idle");
        AssistantExecute.IsEnabled = false;
        AssistantPreview.Text = "Разберите команду, чтобы проверить действие.";
    }

    private object AssistantSettings() => new
    {
        launcher_path = AssistantLauncher.Text.Trim(), minecraft_dir = AssistantMinecraft.Text.Trim(),
        launcher_properties_path = AssistantLauncherProperties.Text.Trim(),
        channel_url = AssistantChannel.Text.Trim(), stt_model_path = AssistantModelPath.Text.Trim(),
        planner = AssistantPlanner.SelectedIndex == 1 ? "ollama" : "rules",
        ollama_model = AssistantOllamaModel.Text.Trim()
    };

    private static string AssistantPhase(string state) => state switch
    {
        "loading_model" => "Подготовка локальной модели. Микрофон ещё выключен…",
        "recording" => "Говорите. Запись микрофона: 6 секунд…",
        "transcribing" => "Микрофон выключен. Распознаю запись…",
        "planning" => "Разбираю команду…",
        "executing" => "Выполняю проверенное действие…",
        "ready" => "Команда готова к проверке.",
        "dispatched" => "Запрос передан Windows.",
        "succeeded" => "Действие подтверждено.",
        "blocked" => "Действие недоступно.",
        "unverified" => "Результат не подтверждён.",
        "rejected" => "Команда отклонена.",
        _ => "Действие не выполнено."
    };

    private async Task RunAssistantAsync(string operation)
    {
        if (assistantRequest is not null || closed) return;
        var plan = assistantPlan;
        string text = operation == "execute" ? assistantPlannedText : AssistantText.Text;
        if (operation == "execute" && plan is null) return;
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        assistantRequest = cancel;
        assistantRecording = operation == "record";
        AssistantRecord.IsEnabled = AssistantPlanButton.IsEnabled = AssistantExecute.IsEnabled = false;
        AssistantSettingsPanel.IsEnabled = AssistantText.IsEnabled = false;
        AssistantCancel.IsEnabled = true;
        AssistantProgress.Visibility = Visibility.Visible;
        AssistantState.Text = "Подготовка…";
        SetAssistantStage(operation == "execute" ? "executing" : operation == "record" ? "loading_model" : "planning");
        if (assistantRecording) SetMicrophoneStatus("Подготовка записи. Микрофон выключен", MicrophonePhase.Preparing);
        try
        {
            await PrepareAssistantAudioAsync();
            cancel.Token.ThrowIfCancellationRequested();
            bool confirmed = false;
            if (operation == "execute" && plan!.Value.GetProperty("action").GetString() == "join_discord_voice")
            {
                var dialog = new ContentDialog
                {
                    XamlRoot = RootGrid.XamlRoot, Title = "Подключение к голосовому каналу",
                    Content = "Подключиться к «Шаурма классическая»? Это действие может включить микрофон Discord. В этой сборке OAuth/RPC ещё не настроен.",
                    PrimaryButtonText = "Подключиться", CloseButtonText = "Отмена", DefaultButton = ContentDialogButton.Close
                };
                confirmed = await dialog.ShowAsync() == ContentDialogResult.Primary;
                if (!confirmed) { AssistantState.Text = "Подключение отменено."; return; }
            }
            InvalidateAssistantPlan(); // Consume once even if process fails after dispatch.
            var progress = new System.Progress<string>(phase =>
            {
                if (!closed && ReferenceEquals(assistantRequest, cancel) && !cancel.IsCancellationRequested)
                {
                    AssistantState.Text = AssistantPhase(phase);
                    SetAssistantStage(phase);
                    if (phase == "recording") SetMicrophoneStatus("● Микрофон включён: запись 6 секунд", MicrophonePhase.Listening);
                    else if (operation == "record") SetMicrophoneStatus("Микрофон выключен. " + AssistantPhase(phase),
                        phase == "loading_model" ? MicrophonePhase.Preparing : MicrophonePhase.Processing);
                }
            });
            var result = await assistantClient.RequestAsync(AssistantPython.Text.Trim(),
                new { operation, settings = AssistantSettings(), text, plan, confirmed }, progress, cancel.Token);
            if (closed || cancel.IsCancellationRequested) return;
            if (result.TryGetProperty("text", out var recognized))
            {
                applyingRecognizedText = true;
                try { AssistantText.Text = recognized.GetString() ?? ""; }
                finally { applyingRecognizedText = false; }
                assistantDraftPending = !string.IsNullOrWhiteSpace(AssistantText.Text);
            }
            string state = result.GetProperty("state").GetString() ?? "failed";
            SetAssistantStage(state);
            AssistantState.Text = AssistantPhase(state) + " " + result.GetProperty("message").GetString();
            if (state == "ready")
            {
                assistantPlan = result.GetProperty("plan").Clone();
                assistantPlannedText = AssistantText.Text;
                AssistantPreview.Text = result.GetProperty("label").GetString();
                AssistantPlanCard.Visibility = Visibility.Visible;
            }
            if (operation == "execute") assistantDraftPending = false;
            await SpeakAssistantAsync(state == "ready"
                ? "Команда готова: " + AssistantPreview.Text + ". Проверьте её и нажмите Выполнить."
                : AssistantState.Text);
        }
        catch (OperationCanceledException)
        {
            if (!closed) SetAssistantStage("cancelled");
            if (!closed) AssistantState.Text = operation == "execute"
                ? "Ожидание отменено. Уже переданное Windows действие могло выполниться; проверьте приложение."
                : "Отменено. Микрофон выключен.";
        }
        catch (Exception error)
        {
            if (!closed) SetAssistantStage("failed");
            if (error is VoiceShutdownException)
            {
                microphoneShutdownFailed = true;
                BackgroundListening.IsOn = false;
                backgroundVoice?.Stop();
            }
            if (!closed) AssistantState.Text = error is InvalidOperationException or TimeoutException
                ? error.Message : "Помощник не выполнил действие. Проверьте Python, модель и настройки.";
        }
        finally
        {
            assistantRequest = null;
            assistantRecording = false;
            if (!closed)
            {
                AssistantRecord.IsEnabled = AssistantPlanButton.IsEnabled = true;
                AssistantSettingsPanel.IsEnabled = AssistantText.IsEnabled = true;
                AssistantExecute.IsEnabled = assistantPlan is not null;
                AssistantCancel.IsEnabled = assistantPlan is not null || assistantDraftPending;
                AssistantProgress.Visibility = Visibility.Collapsed;
                await RefreshBackgroundSafelyAsync();
                if (!BackgroundListening.IsOn) SetMicrophoneStatus("Микрофон выключен");
            }
        }
    }

    private async void AssistantRecordClick(object sender, RoutedEventArgs args) => await RunAssistantAsync("record");
    private async void AssistantPlanClick(object sender, RoutedEventArgs args) => await RunAssistantAsync("plan");
    private async void AssistantExecuteClick(object sender, RoutedEventArgs args) => await RunAssistantAsync("execute");
    private void AssistantCancelClick(object sender, RoutedEventArgs args)
    {
        InvalidateAssistantPlan();
        assistantDraftPending = false;
        StopAssistantSpeech();
        if (assistantRequest is not null) assistantRequest.Cancel();
        else
        {
            AssistantCancel.IsEnabled = false;
            AssistantState.Text = "Команда отменена.";
            SetAssistantStage("cancelled");
            _ = RefreshBackgroundSafelyAsync();
        }
    }

    private async Task RunAsync(Func<Task> action, bool quiet = false)
    {
        if (busy || closed) return;
        do
        {
            busy = true;
            Progress.Visibility = quiet ? Visibility.Collapsed : Visibility.Visible;
            UpdateControls();
            try
            {
                await action();
                if (!closed && !quiet) Notice.IsOpen = false;
            }
            catch (OperationCanceledException) when (closed) { }
            catch (Exception error)
            {
                if (!closed)
                {
                    Notice.Severity = InfoBarSeverity.Error;
                    Notice.Title = "Не удалось выполнить действие";
                    // Only locally authored errors are shown. Parsing/IO errors may
                    // include paths or response bodies, so keep those generic.
                    Notice.Message = error is InvalidOperationException or TimeoutException
                        ? error.Message : "Проверьте пути, зависимости Python, привязку и работающий агент.";
                    Notice.IsOpen = true;
                    if (quiet)
                    {
                        playerState = "offline";
                        OverviewStatus.Text = "Нет свежих данных от агента";
                        DeviceState.Text = "Нет свежих данных";
                        PlaybackState.Text = "Состояние плеера недоступно";
                    }
                }
            }
            finally
            {
                busy = false;
                if (!closed)
                {
                    Progress.Visibility = Visibility.Collapsed;
                    UpdateControls();
                }
            }
            // A rapid sequence of searches keeps only the latest query. The
            // dispatcher remains editable, with at most one adapter in flight.
            if (closed || !connected || pendingSearch is not string latest) break;
            pendingSearch = null;
            action = () => LoadCatalogAsync(0, latest, "reset");
            quiet = false;
        } while (!closed);
    }

    private void UpdateControls()
    {
        SearchButton.IsEnabled = connected;
        ConnectButton.IsEnabled = !busy;
        PythonPath.IsEnabled = SourcePath.IsEnabled = DataPath.IsEnabled = !busy;
        PreviousPage.IsEnabled = connected && !busy && previousOffsets.Count > 0;
        NextPage.IsEnabled = connected && !busy && nextOffset.HasValue;
        PlayButton.IsEnabled = connected && !busy && TrackList.SelectedItem is Track;
        PauseButton.IsEnabled = !busy && playerState == "playing";
        ResumeButton.IsEnabled = !busy && playerState == "paused";
        StopButton.IsEnabled = !busy && playerState is "playing" or "paused" or "loading" or "error" or "stopping";
        SeekButton.IsEnabled = SeekSlider.IsEnabled = !busy && playerState is "playing" or "paused";
        VolumeButton.IsEnabled = connected && !busy && playerState is "playing" or "paused";
    }

    private async void ConnectClick(object sender, RoutedEventArgs args) => await RunAsync(async () =>
    {
        connected = false;
        client.PythonPath = PythonPath.Text.Trim();
        client.SourcePath = SourcePath.Text.Trim();
        client.DataPath = DataPath.Text.Trim();
        TrackList.ItemsSource = Array.Empty<Track>();
        previousOffsets.Clear(); nextOffset = null; offset = 0; query = ""; pendingSearch = null;
        EmptyLibrary.Visibility = Visibility.Visible;
        CatalogCount.Text = "Нажмите «Найти», чтобы загрузить библиотеку";
        EmptyLibraryTitle.Text = "Ваша музыка, здесь";
        EmptyLibraryDetail.Text = "Нажмите «Найти», чтобы открыть библиотеку сервера.";
        playerState = "offline";
        DeviceName.Text = "Не выбран";
        DeviceState.Text = OverviewStatus.Text = "Подключение к локальному агенту…";
        NowPlaying.Text = OverviewTrack.Text = "Нет свежих данных";
        seekDirty = volumeDirty = false;
        await RefreshAsync();
        connected = true;
    });

    private async void RefreshClick(object sender, RoutedEventArgs args) => await RunAsync(RefreshAsync);

    private async Task RefreshAsync()
    {
        if (desktopReady) { await RefreshDesktopAsync(); return; }
        JsonElement result;
        try
        {
            result = await client.RequestAsync(new { action = "snapshot" }, lifetime.Token);
        }
        catch
        {
            if (!closed)
            {
                playerState = "offline";
                DeviceState.Text = OverviewStatus.Text = "Нет свежих данных от агента";
                PlaybackState.Text = "Состояние плеера недоступно";
            }
            throw;
        }
        if (closed) return;
        JsonElement device = result.GetProperty("device"), player = result.GetProperty("playback");
        DeviceName.Text = device.GetProperty("name").GetString() ?? "Этот компьютер";
        DeviceState.Text = StateLabel(device.GetProperty("state").GetString());
        OverviewStatus.Text = $"{DeviceName.Text} · {DeviceState.Text}";
        string title = player.GetProperty("title").GetString() ?? "Ничего не играет";
        string artist = player.GetProperty("artist").GetString() ?? "";
        string trackKey = player.GetProperty("track_id").ToString() + "\n" + title + "\n" + artist;
        if (trackKey != lastTrack) seekDirty = false;
        lastTrack = trackKey;
        NowPlaying.Text = OverviewTrack.Text = string.IsNullOrWhiteSpace(artist) ? title : $"{title} · {artist}";
        playerState = player.GetProperty("state").GetString() ?? "offline";
        double position = player.GetProperty("position").GetDouble(), duration = player.GetProperty("duration").GetDouble();
        PlaybackState.Text = $"{StateLabel(playerState)} · {Clock(position)} / {Clock(duration)}";
        updatingSeek = true;
        SeekSlider.Maximum = Math.Max(1, duration);
        if (!seekDirty) SeekSlider.Value = Math.Min(position, SeekSlider.Maximum);
        updatingSeek = false;
        if (!volumeDirty && playerState != "offline")
        {
            updatingVolume = true;
            VolumeSlider.Value = player.GetProperty("volume").GetDouble();
            updatingVolume = false;
        }
    }

    private async Task LoadCatalogAsync(int requestedOffset, string requestedQuery, string navigation)
    {
        JsonElement result = await client.RequestAsync(new { action = "catalog", offset = requestedOffset, query = requestedQuery }, lifetime.Token);
        if (closed || pendingSearch is not null) return;
        // Parse the entire bounded response before replacing the current page.
        var loaded = result.GetProperty("tracks").EnumerateArray().Select(row => new Track(
            row.GetProperty("id").GetInt32(), row.GetProperty("title").GetString() ?? "",
            row.GetProperty("artist").GetString() ?? "", row.GetProperty("album").GetString() ?? "")).ToList();
        JsonElement next = result.GetProperty("next_offset");
        int? parsedNext = next.ValueKind == JsonValueKind.Null ? null : next.GetInt32();
        int total = result.GetProperty("total").GetInt32();
        if (navigation == "reset") previousOffsets.Clear();
        else if (navigation == "next") previousOffsets.Push(offset);
        else if (navigation == "previous") previousOffsets.Pop();
        offset = requestedOffset; query = requestedQuery; nextOffset = parsedNext;
        // One replacement instead of up to 100 CollectionChanged/layout passes.
        // Polling never replaces this source, so scrolling/selection stays put.
        TrackList.ItemsSource = loaded;
        EmptyLibrary.Visibility = loaded.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        EmptyLibraryTitle.Text = "Ничего не найдено";
        EmptyLibraryDetail.Text = "Попробуйте другое название, исполнителя или альбом.";
        CatalogCount.Text = total == 0 ? "Ничего не найдено" : $"{offset + 1}–{offset + loaded.Count} из {total}";
    }

    private async void SearchClick(object sender, RoutedEventArgs args) =>
        await SearchAsync();
    private async Task SearchAsync()
    {
        if (!connected || closed) return;
        string requested = SearchBox.Text.Trim();
        if (busy)
        {
            pendingSearch = requested;
            CatalogCount.Text = "Следующий поиск готовится…";
            return;
        }
        await RunAsync(() => LoadCatalogAsync(0, requested, "reset"));
    }
    private async void SearchKeyDown(object sender, KeyRoutedEventArgs args)
    {
        if (args.Key == Windows.System.VirtualKey.Enter && connected)
        {
            args.Handled = true;
            await SearchAsync();
        }
    }
    private async void NextClick(object sender, RoutedEventArgs args)
    {
        if (nextOffset is int next) await RunAsync(() => LoadCatalogAsync(next, query, "next"));
    }
    private async void PreviousClick(object sender, RoutedEventArgs args)
    {
        if (previousOffsets.Count > 0) await RunAsync(() => LoadCatalogAsync(previousOffsets.Peek(), query, "previous"));
    }
    private void TrackSelected(object sender, SelectionChangedEventArgs args) => UpdateControls();
    private void OpenMusicClick(object sender, RoutedEventArgs args) => SelectDesktopPage("music");
    private void OpenConnectionClick(object sender, RoutedEventArgs args) => SelectDesktopPage("connection");
    private async void PlayClick(object sender, RoutedEventArgs args)
    {
        if (TrackList.SelectedItem is Track track)
            await RunAsync(async () =>
            {
                await client.RequestAsync(new { action = "play", track_id = track.Id, volume = VolumeSlider.Value }, lifetime.Token);
                seekDirty = false;
                await RefreshAsync(); // Never optimistically claim the queued track is playing.
            });
    }
    private async Task ControlAsync(object request)
    {
        await client.RequestAsync(request, lifetime.Token);
        await RefreshAsync();
    }
    private async void PauseClick(object sender, RoutedEventArgs args) => await RunAsync(() => ControlAsync(new { action = "pause" }));
    private async void ResumeClick(object sender, RoutedEventArgs args) => await RunAsync(() => ControlAsync(new { action = "resume" }));
    private async void StopClick(object sender, RoutedEventArgs args) => await RunAsync(() => ControlAsync(new { action = "stop" }));
    private async void SeekClick(object sender, RoutedEventArgs args) => await RunAsync(async () =>
    {
        await client.RequestAsync(new { action = "seek", position = SeekSlider.Value }, lifetime.Token);
        seekDirty = false;
        await RefreshAsync();
    });
    private async void VolumeClick(object sender, RoutedEventArgs args) => await RunAsync(async () =>
    {
        await client.RequestAsync(new { action = "volume", volume = VolumeSlider.Value }, lifetime.Token);
        volumeDirty = false;
        await RefreshAsync();
    });
    private static string Clock(double seconds) => $"{(int)seconds / 60}:{(int)seconds % 60:00}";
    private static string StateLabel(string? state) => state switch
    {
        "online" => "На связи", "playing" => "Играет", "paused" => "Пауза", "loading" => "Загрузка",
        "connecting" => "Подключение", "stopped" => "Остановлено", "idle" => "Готов",
        "ended" => "Завершено", "error" => "Ошибка", "stale" => "Нет свежего ответа", _ => "Не в сети"
    };
}
