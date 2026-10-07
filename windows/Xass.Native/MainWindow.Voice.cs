using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly LocalSpeechService localSpeech = new();
    private readonly SemaphoreSlim voiceTransitions = new(1, 1);
    private BackgroundVoiceClient? backgroundVoice;
    private CancellationTokenSource? backgroundLifetime;
    private Task speechTask = Task.CompletedTask;
    private CancellationTokenSource? speechCancellation;
    private bool voiceInitialized;
    private bool assistantDraftPending;
    private bool applyingRecognizedText;
    private bool voiceSpeaking;
    private bool assistantRecording;
    private bool backgroundPaused = true;
    private bool microphoneShutdownFailed;
    private bool acceptBackgroundCommands;
    private int backgroundEpoch;
    private int backgroundSession;

    private void InitializeVoice()
    {
        voiceInitialized = true;
        try
        {
            var voices = LocalSpeechService.InstalledVoices();
            AssistantVoice.ItemsSource = voices;
            AssistantVoice.DisplayMemberPath = nameof(LocalVoice.Label);
            AssistantVoice.SelectedItem = voices.FirstOrDefault(voice => voice.RussianFemale);
            AssistantSpeech.IsOn = AssistantVoice.SelectedItem is not null;
            AssistantSpeechStatus.Text = AssistantVoice.SelectedItem is null
                ? "Русский женский голос Windows не найден. Можно выбрать другой установленный голос. Ничего не скачивается."
                : "Локальный женский голос Windows. Настройки действуют до закрытия окна.";
        }
        catch
        {
            AssistantSpeech.IsOn = false;
            AssistantSpeechStatus.Text = "Windows не предоставил список голосов. Текстовый помощник остаётся доступен.";
        }
        AssistantSpeech.Toggled += (_, _) => { if (!AssistantSpeech.IsOn) StopAssistantSpeech(); };
        AssistantVoice.SelectionChanged += (_, _) => StopAssistantSpeech();
        AssistantSpeechStop.Click += (_, _) => StopAssistantSpeech();
        AssistantSpeechPreview.Click += async (_, _) =>
        {
            if (assistantRequest is null)
                await SpeakAssistantAsync("Здравствуйте. Я готова помочь. Команды выполняются после вашей проверки.", preview: true);
        };
        BackgroundListening.Toggled += async (_, _) => await ChangeBackgroundListeningAsync();
        BackgroundStop.Click += (_, _) => StopAllMicrophones();
        AssistantPython.TextChanged += (_, _) => StopListenerForSettingsChange();
        AssistantModelPath.TextChanged += (_, _) => StopListenerForSettingsChange();
        SetMicrophoneStatus("Микрофон выключен");
    }

    private bool CanListen => !microphoneShutdownFailed && !closed && BackgroundListening.IsOn && !assistantDraftPending
        && assistantPlan is null && assistantRequest is null && !voiceSpeaking;

    private void SetMicrophoneStatus(string text, MicrophonePhase phase = MicrophonePhase.Off)
    {
        if (closed) return;
        if (microphoneShutdownFailed)
        { text = "Остановка микрофона не подтверждена. Закройте XASS; не считайте микрофон выключенным."; phase = MicrophonePhase.Failed; }
        else if (backgroundVoice is { IsStopped: true } || (assistantRecording && assistantRequest?.IsCancellationRequested == true))
        { text = "Останавливаю микрофон…"; phase = MicrophonePhase.Stopping; }
        MicrophoneStatus.Text = text;
        BackgroundStatus.Text = text;
        if (assistantRequest is null && assistantPlan is null && phase is not MicrophonePhase.Off)
            SetAssistantStage(phase switch { MicrophonePhase.Preparing => "loading_model", MicrophonePhase.Listening => "listening",
                MicrophonePhase.Processing => "transcribing", MicrophonePhase.Failed => "failed", _ => "paused" });
        var presentation = MicrophonePresentation.For(phase, BackgroundListening.IsOn || assistantRecording);
        BackgroundStop.Content = presentation.Action;
        BackgroundStop.IsEnabled = presentation.CanStop;
        BackgroundStop.Visibility = phase == MicrophonePhase.Off && !presentation.CanStop ? Visibility.Collapsed : Visibility.Visible;
    }

    private void AssistantDraftChanged()
    {
        if (!voiceInitialized || applyingRecognizedText || closed) return;
        assistantDraftPending = !string.IsNullOrWhiteSpace(AssistantText.Text);
        acceptBackgroundCommands = false;
        AssistantCancel.IsEnabled = assistantDraftPending || assistantRequest is not null;
        _ = RefreshBackgroundSafelyAsync();
    }

    private void StopListenerForSettingsChange()
    {
        if (BackgroundListening.IsOn) BackgroundListening.IsOn = false;
    }

    private async Task ChangeBackgroundListeningAsync()
    {
        if (!voiceInitialized || closed) return;
        int session = ++backgroundSession;
        acceptBackgroundCommands = false;
        backgroundPaused = true;
        backgroundLifetime?.Cancel();
        backgroundVoice?.Stop();
        if (!BackgroundListening.IsOn)
        {
            SetMicrophoneStatus("Микрофон выключен");
            return;
        }
        // At most one background process: wait for the old reader/kill to finish.
        await voiceTransitions.WaitAsync();
        BackgroundVoiceClient? launched = null;
        var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        backgroundLifetime = cancel;
        try
        {
            if (microphoneShutdownFailed) throw new VoiceShutdownException();
            if (backgroundVoice is { } previous)
            {
                await previous.EnsureStoppedAsync();
                previous.Dispose();
                backgroundVoice = null;
            }
            if (closed || session != backgroundSession || !BackgroundListening.IsOn) return;
            SetMicrophoneStatus("Подготовка модели. Микрофон выключен", MicrophonePhase.Preparing);
            var progress = new Progress<BackgroundVoiceEvent>(update =>
            {
                if (!closed && session == backgroundSession && BackgroundListening.IsOn)
                    HandleBackgroundEvent(update);
            });
            launched = await BackgroundVoiceClient.StartAsync(AssistantPython.Text.Trim(),
                AssistantModelPath.Text.Trim(), ++backgroundEpoch, progress, cancel.Token);
            if (closed || session != backgroundSession || !BackgroundListening.IsOn)
            {
                launched.Stop();
                try { await launched.EnsureStoppedAsync(); }
                catch { backgroundVoice = launched; throw; }
                launched.Dispose();
                launched = null;
                return;
            }
            backgroundVoice = launched;
            _ = ObserveBackgroundAsync(launched, session, cancel);
        }
        catch (Exception error)
        {
            if (error is VoiceShutdownException) microphoneShutdownFailed = true;
            if (!closed && session == backgroundSession)
            {
                BackgroundListening.IsOn = false;
                SetMicrophoneStatus("Микрофон выключен. " + SafeVoiceError(error));
            }
        }
        finally
        {
            if (launched is null)
            {
                if (ReferenceEquals(backgroundLifetime, cancel)) backgroundLifetime = null;
                cancel.Dispose();
            }
            voiceTransitions.Release();
        }
        await RefreshBackgroundSafelyAsync();
    }

    private void HandleBackgroundEvent(BackgroundVoiceEvent update)
    {
        if (update.Kind == "command")
        {
            if (!acceptBackgroundCommands || update.Epoch != backgroundEpoch || !CanListen) return;
            acceptBackgroundCommands = false;
            backgroundPaused = true; // Worker closes the device before publishing a command.
            assistantDraftPending = true;
            applyingRecognizedText = true;
            try { AssistantText.Text = update.Value; }
            finally { applyingRecognizedText = false; }
            SetMicrophoneStatus("Микрофон на паузе: проверьте команду", MicrophonePhase.Paused);
            _ = RunAssistantAsync("plan"); // A preview only. Execute always remains an explicit click.
            return;
        }
        if (update.Kind != "state") return;
        string status = update.Value switch
        {
            "loading_model" => "Подготовка модели. Микрофон выключен",
            "ready" => "Модель готова. Микрофон на паузе",
            "listening" => "● Микрофон включён: «Джарвис, …»",
            "transcribing" => "Микрофон закрыт. Локальное распознавание",
            "paused" => "Микрофон на паузе: ответ или проверка команды",
            _ => "Микрофон выключен"
        };
        SetMicrophoneStatus(status, update.Value switch {
            "loading_model" => MicrophonePhase.Preparing, "listening" => MicrophonePhase.Listening,
            "transcribing" => MicrophonePhase.Processing, "ready" or "paused" => MicrophonePhase.Paused,
            _ => MicrophonePhase.Off });
    }

    private async Task ObserveBackgroundAsync(BackgroundVoiceClient worker, int session, CancellationTokenSource cancel)
    {
        Exception? failure = null;
        try { await worker.Completion; }
        catch (Exception error) { if (!cancel.IsCancellationRequested) failure = error; }
        finally
        {
            await voiceTransitions.WaitAsync();
            try
            {
                if (ReferenceEquals(backgroundVoice, worker))
                {
                    try
                    {
                        await worker.EnsureStoppedAsync();
                        backgroundVoice = null;
                        worker.Dispose();
                    }
                    catch (VoiceShutdownException error)
                    {
                        microphoneShutdownFailed = true;
                        failure = error;
                    }
                }
                if (ReferenceEquals(backgroundLifetime, cancel)) backgroundLifetime = null;
                cancel.Dispose();
            }
            finally { voiceTransitions.Release(); }
        }
        if (!closed && !BackgroundListening.IsOn && backgroundVoice is null && assistantRequest is null)
            SetMicrophoneStatus("Микрофон выключен");
        if (!closed && session == backgroundSession)
        {
            BackgroundListening.IsOn = false;
            SetMicrophoneStatus(failure is null ? "Микрофон выключен" : "Микрофон выключен. " + SafeVoiceError(failure));
        }
    }

    private async Task RefreshBackgroundAsync()
    {
        await voiceTransitions.WaitAsync(lifetime.Token);
        try
        {
            if (microphoneShutdownFailed) throw new VoiceShutdownException();
            var worker = backgroundVoice;
            if (worker is null) return;
            if (worker.IsStopped) { await worker.EnsureStoppedAsync(); return; }
            if (CanListen)
            {
                if (!acceptBackgroundCommands)
                {
                    int epoch = ++backgroundEpoch;
                    acceptBackgroundCommands = true;
                    backgroundPaused = false;
                    try { await worker.ResumeAsync(epoch, lifetime.Token); }
                    catch { acceptBackgroundCommands = false; throw; }
                }
            }
            else
            {
                acceptBackgroundCommands = false;
                if (!backgroundPaused)
                {
                    await worker.PauseAsync(lifetime.Token);
                    backgroundPaused = true;
                }
            }
        }
        finally { voiceTransitions.Release(); }
    }

    private async Task RefreshBackgroundSafelyAsync()
    {
        try { await RefreshBackgroundAsync(); }
        catch (Exception error)
        {
            if (error is VoiceShutdownException) microphoneShutdownFailed = true;
            if (!closed)
            {
                BackgroundListening.IsOn = false;
                SetMicrophoneStatus("Микрофон выключен. " + SafeVoiceError(error));
            }
        }
    }

    private async Task PrepareAssistantAudioAsync()
    {
        acceptBackgroundCommands = false;
        StopAssistantSpeech();
        await speechTask;
        await RefreshBackgroundAsync();
    }

    private async Task SpeakAssistantAsync(string text, bool preview = false)
    {
        if (closed || voiceSpeaking || (!preview && !AssistantSpeech.IsOn)) return;
        if (AssistantVoice.SelectedItem is not LocalVoice voice)
        {
            AssistantSpeechStatus.Text = "Выберите установленный голос Windows. Русский женский голос может отсутствовать.";
            return;
        }
        voiceSpeaking = true;
        acceptBackgroundCommands = false;
        AssistantSpeechPreview.IsEnabled = false;
        AssistantSpeechStop.IsEnabled = true;
        // Publish the task before callers can stop speech and acquire the microphone.
        var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        speechCancellation = cancel;
        speechTask = SpeakCoreAsync(text, voice, cancel);
        await speechTask;
    }

    private async Task SpeakCoreAsync(string text, LocalVoice voice, CancellationTokenSource cancel)
    {
        try
        {
            await RefreshBackgroundAsync(); // Require microphone-closed ACK before synthesis or playback.
            cancel.Token.ThrowIfCancellationRequested();
            AssistantSpeechStatus.Text = "Говорю локальным голосом Windows…";
            await localSpeech.SpeakAsync(text.Length <= 512 ? text : text[..512], voice.Id,
                AssistantSpeechRate.Value, AssistantSpeechVolume.Value / 100, cancel.Token);
            if (!closed) AssistantSpeechStatus.Text = "Голосовой ответ завершён.";
        }
        catch (OperationCanceledException) { if (!closed) AssistantSpeechStatus.Text = "Голосовой ответ остановлен."; }
        catch (Exception error) { if (!closed) AssistantSpeechStatus.Text = SafeVoiceError(error); }
        finally
        {
            if (ReferenceEquals(speechCancellation, cancel)) speechCancellation = null;
            cancel.Dispose();
            voiceSpeaking = false;
            if (!closed)
            {
                AssistantSpeechPreview.IsEnabled = true;
                AssistantSpeechStop.IsEnabled = false;
                await RefreshBackgroundSafelyAsync();
            }
        }
    }

    private void StopAssistantSpeech()
    {
        speechCancellation?.Cancel();
        localSpeech.Stop();
    }

    private static string SafeVoiceError(Exception error) => error is InvalidOperationException
        ? error.Message : "Проверьте локальный голос, Python, модель и разрешения микрофона Windows.";

    private void StopAllMicrophones()
    {
        acceptBackgroundCommands = false;
        BackgroundListening.IsOn = false;
        backgroundLifetime?.Cancel();
        backgroundVoice?.Stop();
        assistantRequest?.Cancel();
        StopAssistantSpeech();
        SetMicrophoneStatus("Микрофон выключен");
    }

    private void DisposeVoice()
    {
        acceptBackgroundCommands = false;
        backgroundLifetime?.Cancel();
        backgroundVoice?.Stop();
        StopAssistantSpeech();
        localSpeech.Dispose();
    }
}
