using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Storage.Pickers;
using Windows.Storage.Streams;
using Windows.System;
using Xass.Native.Services;

namespace Xass.Native;

public sealed class MusicCatalogRow : INotifyPropertyChanged
{
    public int Id { get; init; }
    public string Title { get; init; } = "";
    public string Artist { get; init; } = "";
    public string Album { get; init; } = "";
    public double Duration { get; init; }
    public bool Favorite { get; init; }
    private bool current;
    public bool Current { get => current; set { if (current == value) return; current = value; PropertyChanged?.Invoke(this, new(nameof(Display))); } }
    public string Display => $"{(Current ? "▶ " : "")}{(Favorite ? "★ " : "")}{Title}\n{string.Join(" · ", new[] { Artist, Album, $"{(int)Duration / 60}:{(int)Duration % 60:00}" }.Where(s => !string.IsNullOrWhiteSpace(s)))}";
    public event PropertyChangedEventHandler? PropertyChanged;
}
public sealed record MusicQueueRow(int Index, string Path, string Title, bool Exists)
{
    public string Display => $"{Title}{(Exists ? "" : " · файл не найден")}";
}
public sealed record MusicLyricRow(double? Time, string Text)
{
    public override string ToString() => Text;
}

public sealed partial class MainWindow
{
    private readonly MusicClient nativeMusic = new();
    private readonly AgentClient musicCatalogClient = new();
    private readonly DispatcherTimer musicTimer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private bool desktopMusicActive;
    private bool musicBusy, musicPolling, musicCatalogBusy, musicUpdating, musicSeekDirty, musicVolumeDirty;
    private bool musicPageVisible, musicCatalogLoaded, musicRevealInitialized;
    private string musicState = "idle", musicOwner = "local", musicTrackKey = "", musicCoverDigest = "", musicQuery = "";
    private int musicReveal, musicOffset, musicSearchGeneration, musicCoverGeneration;
    private int? musicNextOffset;
    private (int Offset, string Query, string Direction)? musicPendingCatalog, musicRetryCatalog;
    private readonly Stack<int> musicPreviousOffsets = new();
    private DateTime musicCatalogDue = DateTime.MinValue;
    private JsonElement? musicSnapshot;
    private readonly TextBox musicSearch = new() { PlaceholderText = "Название, исполнитель или альбом", MaxLength = 120 };
    private readonly ListView musicTracks = new() { DisplayMemberPath = "Display", SelectionMode = ListViewSelectionMode.Single };
    private readonly ListView musicQueue = new() { DisplayMemberPath = "Display", SelectionMode = ListViewSelectionMode.Single };
    private readonly ListView musicLyrics = new() { SelectionMode = ListViewSelectionMode.Single, IsTabStop = false, MaxHeight = 160 };
    private readonly TextBlock musicTitle = new() { Text = "Ничего не играет", FontSize = 20, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock musicStatus = new() { Text = "Локальные файлы доступны без привязки к серверу.", TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock musicError = new() { TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed };
    private readonly TextBlock musicCatalogCount = new() { Text = "Библиотека сервера", VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap };
    private readonly Image musicCover = new() { Width = 72, Height = 72, Stretch = Stretch.UniformToFill };
    private readonly Slider musicSeek = new() { Minimum = 0, Maximum = 1 };
    private readonly Slider musicVolume = new() { Minimum = 0, Maximum = 100, Value = 70, Header = "Громкость" };
    private Button musicOpen = null!, musicLocalPlay = null!, musicPrevious = null!, musicNext = null!, musicPlay = null!;
    private Button musicPause = null!, musicResume = null!, musicStop = null!, musicBackPage = null!, musicNextPage = null!;
    private List<MusicCatalogRow> musicRows = new();
    private List<MusicLyricRow> musicLyricRows = new();
    private string musicLyricsText = "", musicQueueSignature = "";

    private static Button MusicButton(string label, RoutedEventHandler click)
    {
        var button = new Button { Content = new TextBlock { Text = label, TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center } };
        AutomationProperties.SetName(button, label);
        button.Click += click;
        return button;
    }
    private void InitializeDesktopMusic()
    {
        desktopMusicActive = true;
        BuildMusicWorkspace();
        musicTimer.Tick += async (_, _) =>
        {
            if (closed) return;
            await PollDesktopMusicAsync();
            if (musicPageVisible && !musicCatalogBusy && DateTime.UtcNow >= musicCatalogDue)
            {
                if (musicRetryCatalog is { } retry) await LoadDesktopCatalogAsync(retry.Offset, retry.Query, retry.Direction);
                else await LoadDesktopCatalogAsync(musicOffset, musicQuery, "refresh");
            }
        };
        musicTimer.Start(); UpdateDesktopMusicControls();
    }

    private static bool IsMusicSliderKey(VirtualKey key) => key is VirtualKey.Left or VirtualKey.Right or VirtualKey.Up or VirtualKey.Down or VirtualKey.Home or VirtualKey.End or VirtualKey.PageUp or VirtualKey.PageDown;
    private (string Python, string Source, string Data) MusicContext()
    {
        string bundled = Path.Combine(AppContext.BaseDirectory, "pc_client");
        return (client.PythonPath.Length > 0 ? client.PythonPath : PythonPath.Text.Trim(),
            Directory.Exists(bundled) ? bundled : client.SourcePath.Length > 0 ? client.SourcePath : SourcePath.Text.Trim(),
            client.DataPath.Length > 0 ? client.DataPath : DataPath.Text.Trim());
    }
    private Task<JsonElement> MusicRequestAsync(object request)
    {
        var c = MusicContext(); return nativeMusic.RequestAsync(request, c.Python, c.Source, c.Data, lifetime.Token);
    }
    private Task<JsonElement> CatalogRequestAsync(object request)
    {
        var c = MusicContext(); musicCatalogClient.PythonPath = c.Python; musicCatalogClient.SourcePath = c.Source; musicCatalogClient.DataPath = c.Data;
        return musicCatalogClient.RequestAsync(request, lifetime.Token);
    }
    private async void OnDesktopMusicNavigated(string page)
    {
        musicPageVisible = page == "music";
        if (!desktopMusicActive || !musicPageVisible) return;
        await PollDesktopMusicAsync();
        if (!musicCatalogLoaded) await LoadDesktopCatalogAsync(0, musicSearch.Text.Trim(), "reset");
    }
    private async void DisposeDesktopMusic()
    {
        musicTimer.Stop(); await discordMusic.DisposeAsync(); await nativeMusic.DisposeAsync();
    }
    private async Task PollDesktopMusicAsync()
    {
        if (closed || musicPolling || musicBusy) return;
        var c = MusicContext();
        if (string.IsNullOrWhiteSpace(c.Source) || string.IsNullOrWhiteSpace(c.Data)) return;
        musicPolling = true;
        try { await ApplyDesktopMusicAsync(await MusicRequestAsync(new { action = "snapshot" })); }
        catch (OperationCanceledException) { }
        catch (Exception)
        {
            _ = PublishDiscordMusicAsync(null);
            if (!closed)
            {
                musicState = "unavailable";
                musicStatus.Text = "Музыкальный модуль недоступен. Проверьте установку или пути подключения.";
                UpdateDesktopMusicControls();
            }
        }
        finally { musicPolling = false; }
    }
    private async Task MusicActionAsync(object request)
    {
        if (closed || musicBusy) return;
        musicBusy = true; UpdateDesktopMusicControls();
        try { await ApplyDesktopMusicAsync(await MusicRequestAsync(request)); }
        catch (OperationCanceledException) { }
        catch (Exception ex) { if (!closed) { musicError.Text = ex is InvalidOperationException ? ex.Message : "Не удалось выполнить действие плеера. Повторите."; musicError.Visibility = Visibility.Visible; } }
        finally { musicBusy = false; if (!closed) UpdateDesktopMusicControls(); }
    }
    private async void PickDesktopMusic(object sender, RoutedEventArgs args)
    {
        if (musicBusy) return;
        try
        {
            var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary };
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            foreach (string suffix in new[] { ".mp3", ".wav", ".flac", ".ogg" }) picker.FileTypeFilter.Add(suffix);
            var files = await picker.PickMultipleFilesAsync();
            if (files.Count == 0 || closed) return;
            if (files.Count > 40) { musicError.Text = "Выберите не больше 40 файлов за один раз."; musicError.Visibility = Visibility.Visible; return; }
            await MusicActionAsync(new { action = "open", paths = files.Select(f => f.Path).ToArray() });
        }
        catch (Exception) { musicError.Text = "Не удалось открыть выбор файлов. Попробуйте ещё раз."; musicError.Visibility = Visibility.Visible; }
    }
    private async Task PlayLocalSelectionAsync()
    {
        if (musicQueue.SelectedItem is MusicQueueRow row) await MusicActionAsync(new { action = "local_play", index = row.Index });
    }
    private async Task PlayCatalogSelectionAsync()
    {
        if (musicBusy || musicTracks.SelectedItem is not MusicCatalogRow row) return;
        musicBusy = true; UpdateDesktopMusicControls();
        try
        {
            // A cast is queued only after local audio has acknowledged silence.
            await MusicRequestAsync(new { action = "stop" });
            await CatalogRequestAsync(new { action = "play", track_id = row.Id, volume = musicVolume.Value });
            musicStatus.Text = "Запуск отправлен агенту. Ожидаем подтверждение воспроизведения…";
            await ApplyDesktopMusicAsync(await MusicRequestAsync(new { action = "snapshot" }));
        }
        catch (OperationCanceledException) { }
        catch (Exception) { if (!closed) { musicError.Text = "Не удалось отправить трек. Проверьте привязку, агент и сеть."; musicError.Visibility = Visibility.Visible; } }
        finally { musicBusy = false; if (!closed) UpdateDesktopMusicControls(); }
    }
    private async Task CommitMusicSeekAsync()
    {
        if (!musicSeekDirty || musicBusy) return;
        double position = musicSeek.Value; musicSeekDirty = false; await MusicActionAsync(new { action = "seek", position });
    }
    private async Task CommitMusicVolumeAsync()
    {
        if (!musicVolumeDirty || musicBusy) return;
        double volume = musicVolume.Value; musicVolumeDirty = false; await MusicActionAsync(new { action = "volume", volume });
    }
    private void UpdateDesktopMusicControls()
    {
        if (musicOpen is null) return;
        bool localAvailable = musicSnapshot is not JsonElement snap || snap.GetProperty("local_available").GetBoolean();
        musicOpen.IsEnabled = !musicBusy && localAvailable;
        musicLocalPlay.IsEnabled = !musicBusy && localAvailable && musicQueue.SelectedItem is MusicQueueRow;
        musicPrevious.IsEnabled = musicNext.IsEnabled = !musicBusy && localAvailable && musicQueue.Items.Count > 0;
        musicPlay.IsEnabled = !musicBusy && (musicSource.SelectedIndex == 0 ? localAvailable && musicQueue.SelectedItem is MusicQueueRow : musicTracks.SelectedItem is MusicCatalogRow);
        musicPause.IsEnabled = !musicBusy && musicState == "playing";
        musicResume.IsEnabled = !musicBusy && musicState is "paused" or "ended";
        musicStop.IsEnabled = !musicBusy && musicState is "playing" or "paused" or "loading" or "error" or "stopping" or "ended";
        musicSeek.IsEnabled = !musicBusy && musicState is "playing" or "paused" or "ended";
        musicVolume.IsEnabled = !musicBusy;
        musicBackPage.IsEnabled = !musicCatalogBusy && musicPreviousOffsets.Count > 0;
        musicNextPage.IsEnabled = !musicCatalogBusy && musicNextOffset.HasValue;
        musicPause.Visibility = musicState == "playing" ? Visibility.Visible : Visibility.Collapsed;
        musicResume.Visibility = musicState is "paused" or "ended" ? Visibility.Visible : Visibility.Collapsed;
        musicPlay.Visibility = musicState is "playing" or "paused" or "ended" ? Visibility.Collapsed : Visibility.Visible;
        musicLoading.IsActive = musicBusy || musicState == "loading";
        musicLoading.Visibility = musicLoading.IsActive ? Visibility.Visible : Visibility.Collapsed;
        musicQueueEmpty.Visibility = musicQueue.Items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        musicCatalogEmpty.Visibility = musicTracks.Items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        musicCatalogEmpty.Text = musicCatalogBusy ? "Загружаем библиотеку…" : musicCatalogLoaded ? "Треки не найдены. Измените запрос." : "Библиотека пока недоступна. Нажмите «Найти» для повторной попытки.";
        musicFollow.IsEnabled = musicLyricRows.Any(row => row.Time is not null);
    }

    private async Task ApplyDesktopMusicAsync(JsonElement snap)
    {
        if (closed) return;
        _ = PublishDiscordMusicAsync(DiscordMusicTrack.FromSnapshot(snap));
        musicSnapshot = snap;
        musicOwner = snap.GetProperty("owner").GetString() ?? "local";
        musicState = snap.GetProperty("state").GetString() ?? "idle";
        string title = snap.GetProperty("title").GetString() ?? "", artist = snap.GetProperty("artist").GetString() ?? "";
        string key = musicOwner + snap.GetProperty("track_id") + title + artist + snap.GetProperty("current_index");
        if (key != musicTrackKey) musicSeekDirty = false;
        musicTrackKey = key;
        musicTitle.Text = title; musicArtist.Text = artist;
        musicArtist.Visibility = string.IsNullOrWhiteSpace(artist) ? Visibility.Collapsed : Visibility.Visible;
        OverviewTrack.Text = string.IsNullOrWhiteSpace(artist) ? title : $"{title} · {artist}";
        if (musicHistory.Observe(musicState,key,title,artist,DateTimeOffset.Now))
        {
            musicHistoryList.ItemsSource = musicHistory.Entries.ToList();
            musicHistoryEmpty.Visibility = Visibility.Collapsed;
        }
        double position = snap.GetProperty("position").GetDouble(), duration = snap.GetProperty("duration").GetDouble();
        musicStatus.Text = $"{(musicOwner == "agent" ? "Трансляция" : "На компьютере")} · {StateLabel(musicState)}";
        musicElapsed.Text = Clock(position); musicDuration.Text = Clock(duration);
        musicError.Text = snap.TryGetProperty("compatibility_notice", out var compatibility) && !string.IsNullOrWhiteSpace(compatibility.GetString())
            ? compatibility.GetString() : snap.GetProperty("error").GetString(); musicError.Visibility = string.IsNullOrWhiteSpace(musicError.Text) ? Visibility.Collapsed : Visibility.Visible;
        musicUpdating = true;
        musicSeek.Maximum = Math.Max(1, duration); if (!musicSeekDirty) musicSeek.Value = Math.Clamp(position, 0, musicSeek.Maximum);
        if (!musicVolumeDirty) musicVolume.Value = snap.GetProperty("volume").GetDouble();
        musicUpdating = false;
        string queueSignature = snap.GetProperty("queue").GetRawText();
        if (queueSignature != musicQueueSignature)
        {
            string? selected = (musicQueue.SelectedItem as MusicQueueRow)?.Path;
            var queue = snap.GetProperty("queue").EnumerateArray().Select(r => new MusicQueueRow(r.GetProperty("index").GetInt32(), r.GetProperty("path").GetString() ?? "", r.GetProperty("title").GetString() ?? "", r.GetProperty("exists").GetBoolean())).ToList();
            musicQueue.ItemsSource = queue; musicQueue.SelectedItem = queue.Find(r => r.Path == selected); musicQueueSignature = queueSignature;
        }
        int? current = snap.GetProperty("track_id").ValueKind == JsonValueKind.Number ? snap.GetProperty("track_id").GetInt32() : null;
        foreach (var row in musicRows) row.Current = musicOwner == "agent" && row.Id == current;
        string lyrics = snap.GetProperty("lyrics").GetString() ?? "";
        if (musicLyricsText != lyrics || musicLyrics.ItemsSource is null)
        {
            musicLyricsText = lyrics;
            musicLyricRows = snap.GetProperty("lyric_rows").EnumerateArray().Select(r => new MusicLyricRow(r.GetProperty("time").ValueKind == JsonValueKind.Number ? r.GetProperty("time").GetDouble() : null, r.GetProperty("text").GetString() ?? "")).ToList();
            if (musicLyricRows.Count == 0) musicLyricRows.Add(new(null, "Текст песни отсутствует"));
            musicLyrics.ItemsSource = musicLyricRows;
            musicLyricStatus.Text = musicLyricRows.Any(row => row.Time is not null) ? "По времени трека" : string.IsNullOrWhiteSpace(lyrics) ? "В треке нет текста песни" : "Текст без временных меток";
        }
        int lyricIndex = musicLyricRows.FindLastIndex(r => r.Time is double at && at <= position);
        if (musicLyrics.SelectedIndex != lyricIndex)
        {
            musicLyrics.SelectedIndex = lyricIndex;
            if (lyricIndex >= 0 && musicFollow.IsChecked == true) musicLyrics.ScrollIntoView(musicLyricRows[lyricIndex], ScrollIntoViewAlignment.Leading);
        }
        int reveal = snap.GetProperty("reveal").GetInt32();
        if (musicRevealInitialized && reveal > 0 && reveal != musicReveal)
        {
            Navigation.SelectedItem = Navigation.MenuItems.OfType<NavigationViewItem>().FirstOrDefault(i => i.Tag?.ToString() == "music");
            AppWindow.Show(); Activate(); FlashDesktopMusic();
        }
        musicReveal = reveal; musicRevealInitialized = true;
        UpdateDesktopMusicControls();
        string digest = snap.GetProperty("cover_sha256").GetString() ?? "";
        if (digest == musicCoverDigest) return;
        musicCoverDigest = digest; int coverGeneration = ++musicCoverGeneration; musicCover.Source = null; coverPalette = null; UpdateMusicBackdrop();
        if (digest.Length != 64) return;
        try
        {
            byte[] bytes = Convert.FromBase64String(snap.GetProperty("cover").GetString() ?? "");
            if (bytes.Length > 512 * 1024 || !Convert.ToHexString(SHA256.HashData(bytes)).Equals(digest, StringComparison.OrdinalIgnoreCase)) return;
            using var stream = new InMemoryRandomAccessStream();
            using (var writer = new DataWriter(stream.GetOutputStreamAt(0))) { writer.WriteBytes(bytes); await writer.StoreAsync(); await writer.FlushAsync(); }
            stream.Seek(0); var bitmap = new BitmapImage(); await bitmap.SetSourceAsync(stream);
            stream.Seek(0);
            var decoder = await Windows.Graphics.Imaging.BitmapDecoder.CreateAsync(stream);
            var pixels = await decoder.GetPixelDataAsync(Windows.Graphics.Imaging.BitmapPixelFormat.Bgra8,
                Windows.Graphics.Imaging.BitmapAlphaMode.Straight, new Windows.Graphics.Imaging.BitmapTransform { ScaledWidth = 32, ScaledHeight = 32 },
                Windows.Graphics.Imaging.ExifOrientationMode.IgnoreExifOrientation, Windows.Graphics.Imaging.ColorManagementMode.ColorManageToSRgb);
            var palette = CoverPalette.FromBgra(pixels.DetachPixelData());
            if (!closed && coverGeneration == musicCoverGeneration) { musicCover.Source = bitmap; coverPalette = palette; UpdateMusicBackdrop(); }
        }
        catch (Exception) { if (coverGeneration == musicCoverGeneration) musicCover.Source = null; }
    }

    private async Task SearchDesktopMusicAsync() => await LoadDesktopCatalogAsync(0, musicSearch.Text.Trim(), "reset");
    private async Task LoadDesktopCatalogAsync(int requestedOffset, string requestedQuery, string direction)
    {
        if (closed) return;
        if (musicCatalogBusy)
        {
            ++musicSearchGeneration;
            musicPendingCatalog = (requestedOffset, requestedQuery, direction);
            return;
        }
        int generation = ++musicSearchGeneration;
        musicCatalogBusy = true; UpdateDesktopMusicControls();
        try
        {
            JsonElement result = await CatalogRequestAsync(new { action = "catalog", offset = requestedOffset, query = requestedQuery });
            if (closed || generation != musicSearchGeneration) return;
            var rows = result.GetProperty("tracks").EnumerateArray().Select(r => new MusicCatalogRow {
                Id = r.GetProperty("id").GetInt32(), Title = r.GetProperty("title").GetString() ?? "", Artist = r.GetProperty("artist").GetString() ?? "", Album = r.GetProperty("album").GetString() ?? "",
                Duration = r.TryGetProperty("duration", out var d) ? d.GetDouble() : 0, Favorite = r.TryGetProperty("favorite", out var f) && f.GetBoolean() }).ToList();
            int? selected = (musicTracks.SelectedItem as MusicCatalogRow)?.Id;
            ScrollViewer? scroller = FindMusicScroller(musicTracks); double scroll = scroller?.VerticalOffset ?? 0;
            if (direction == "reset") musicPreviousOffsets.Clear();
            else if (direction == "next") musicPreviousOffsets.Push(musicOffset);
            else if (direction == "previous" && musicPreviousOffsets.Count > 0) musicPreviousOffsets.Pop();
            musicRows = rows; musicTracks.ItemsSource = rows;
            if (direction == "refresh") { musicTracks.SelectedItem = rows.Find(r => r.Id == selected); DispatcherQueue.TryEnqueue(() => scroller?.ChangeView(null, scroll, null, true)); }
            musicOffset = requestedOffset; musicQuery = requestedQuery;
            musicNextOffset = result.GetProperty("next_offset").ValueKind == JsonValueKind.Null ? null : result.GetProperty("next_offset").GetInt32();
            int total = result.GetProperty("total").GetInt32();
            musicCatalogCount.Text = rows.Count == 0 ? "Ничего не найдено" : $"{requestedOffset + 1}–{requestedOffset + rows.Count} из {total}";
            musicCatalogLoaded = true; musicRetryCatalog = null; musicCatalogDue = DateTime.UtcNow.AddSeconds(120);
            if (musicSnapshot is JsonElement snapshot) await ApplyDesktopMusicAsync(snapshot);
        }
        catch (OperationCanceledException) { }
        catch (Exception)
        {
            if (!closed && generation == musicSearchGeneration) { musicCatalogCount.Text = "Библиотека недоступна. Проверьте привязку и сеть; повтор через 30 секунд."; musicRetryCatalog = (requestedOffset, requestedQuery, direction); musicCatalogDue = DateTime.UtcNow.AddSeconds(30); }
        }
        finally
        {
            musicCatalogBusy = false;
            if (!closed)
            {
                UpdateDesktopMusicControls();
                if (musicPendingCatalog is { } pending)
                {
                    musicPendingCatalog = null;
                    await LoadDesktopCatalogAsync(pending.Offset, pending.Query, pending.Direction);
                }
            }
        }
    }
    private static ScrollViewer? FindMusicScroller(DependencyObject parent)
    {
        if (parent is ScrollViewer viewer) return viewer;
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++) if (FindMusicScroller(VisualTreeHelper.GetChild(parent, i)) is ScrollViewer found) return found;
        return null;
    }
    [StructLayout(LayoutKind.Sequential)] private struct MusicFlashInfo { public uint Size; public IntPtr Window; public uint Flags; public uint Count; public uint Timeout; }
    [DllImport("user32.dll", EntryPoint = "FlashWindowEx")] private static extern bool MusicFlashWindow(ref MusicFlashInfo info);
    private void FlashDesktopMusic()
    {
        var info = new MusicFlashInfo { Size = (uint)Marshal.SizeOf<MusicFlashInfo>(), Window = WinRT.Interop.WindowNative.GetWindowHandle(this), Flags = 3, Count = 3 };
        MusicFlashWindow(ref info);
    }
}
