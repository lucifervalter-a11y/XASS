using Windows.Foundation;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Media.SpeechSynthesis;

namespace Xass.Native.Services;

public sealed record LocalVoice(string Id, string Label, bool RussianFemale, bool Preferred);

// Installed Windows voices only. Text and generated audio stay in this process.
public sealed class LocalSpeechService : IDisposable
{
    private readonly SemaphoreSlim serial = new(1, 1);
    private readonly object sync = new();
    private CancellationTokenSource? current;
    private MediaPlayer? player;
    private bool disposed;

    public static IReadOnlyList<LocalVoice> InstalledVoices() => SpeechSynthesizer.AllVoices
        .Select(voice => new LocalVoice(voice.Id, $"{voice.DisplayName} · {voice.Language} · {voice.Gender}",
            voice.Language.Equals("ru-RU", StringComparison.OrdinalIgnoreCase) && voice.Gender == VoiceGender.Female,
            voice.Language.Equals("ru-RU", StringComparison.OrdinalIgnoreCase) && voice.Gender == VoiceGender.Female
                && (voice.DisplayName.Contains("Irina", StringComparison.OrdinalIgnoreCase)
                    || voice.DisplayName.Contains("Ирина", StringComparison.OrdinalIgnoreCase))))
        .OrderByDescending(voice => voice.Preferred).ThenByDescending(voice => voice.RussianFemale)
        .ThenBy(voice => voice.Label).ToArray();

    public async Task SpeakAsync(string text, string voiceId, double rate, double volume, CancellationToken lifetime)
    {
        if (string.IsNullOrWhiteSpace(text) || text.Length > 512)
            throw new InvalidOperationException("Голосовой ответ должен содержать от 1 до 512 символов.");
        var voice = SpeechSynthesizer.AllVoices.FirstOrDefault(value => value.Id == voiceId)
            ?? throw new InvalidOperationException("Выбранный голос Windows больше не доступен. Выберите установленный голос.");
        var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime);
        cancel.CancelAfter(TimeSpan.FromSeconds(60));
        lock (sync)
        {
            ObjectDisposedException.ThrowIf(disposed, this);
            current?.Cancel();
            current = cancel;
        }
        bool entered = false;
        try
        {
            await serial.WaitAsync(cancel.Token);
            entered = true;
            cancel.Token.ThrowIfCancellationRequested();
            using var synthesizer = new SpeechSynthesizer { Voice = voice };
            synthesizer.Options.SpeakingRate = Math.Clamp(rate, 0.75, 1.5);
            using var stream = await synthesizer.SynthesizeTextToStreamAsync(text).AsTask(cancel.Token);
            cancel.Token.ThrowIfCancellationRequested();
            using var source = MediaSource.CreateFromStream(stream, stream.ContentType);
            using var media = new MediaPlayer { AutoPlay = false, Volume = Math.Clamp(volume, 0, 1) };
            // Do not take over the music player's system transport controls.
            media.CommandManager.IsEnabled = false;
            var ended = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            TypedEventHandler<MediaPlayer, object> onEnded = (_, _) => ended.TrySetResult();
            TypedEventHandler<MediaPlayer, MediaPlayerFailedEventArgs> onFailed = (_, _) =>
                ended.TrySetException(new InvalidOperationException("Windows не смог воспроизвести голосовой ответ."));
            media.MediaEnded += onEnded;
            media.MediaFailed += onFailed;
            using var registration = cancel.Token.Register(() =>
            {
                try { media.Pause(); } catch { /* Device/close races must still cancel the wait. */ }
                ended.TrySetCanceled(cancel.Token);
            });
            try
            {
                lock (sync)
                {
                    cancel.Token.ThrowIfCancellationRequested();
                    player = media;
                    media.Source = source;
                    media.Play();
                }
                await ended.Task;
            }
            finally
            {
                lock (sync) { if (ReferenceEquals(player, media)) player = null; }
                media.MediaEnded -= onEnded;
                media.MediaFailed -= onFailed;
                media.Pause();
                media.Source = null;
            }
        }
        finally
        {
            lock (sync) { if (ReferenceEquals(current, cancel)) current = null; }
            cancel.Dispose();
            if (entered) serial.Release();
        }
    }

    public void Stop()
    {
        lock (sync)
        {
            current?.Cancel();
            try { player?.Pause(); } catch { /* Already closed or audio device removed. */ }
        }
    }

    public void Dispose()
    {
        lock (sync) { disposed = true; }
        Stop();
        // In-flight SpeakAsync owns/disposes its stream, source, player and synthesizer.
    }
}
