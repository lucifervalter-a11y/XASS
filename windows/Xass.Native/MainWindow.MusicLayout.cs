using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.Media;
using Windows.Foundation;
using Windows.System;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly Grid musicWorkspace = new() { ColumnSpacing = 28, RowSpacing = 16 };
    private readonly Grid musicHero = new() { ColumnSpacing = 16, RowSpacing = 16 };
    private readonly Grid musicTransport = new() { RowSpacing = 2 };
    private readonly Border musicArtFrame = new() { CornerRadius = new CornerRadius(20), HorizontalAlignment = HorizontalAlignment.Center };
    private readonly TextBlock musicArtist = new() { FontSize = 17, TextWrapping = TextWrapping.Wrap, Opacity = 0.75 };
    private readonly TextBlock musicElapsed = new() { Text = "0:00", FontSize = 12, Opacity = 0.7 };
    private readonly TextBlock musicDuration = new() { Text = "0:00", FontSize = 12, Opacity = 0.7, HorizontalAlignment = HorizontalAlignment.Right };
    private readonly TextBlock musicQueueEmpty = new() { Text = "Очередь пуста. Добавьте аудиофайлы или выберите библиотеку сервера.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly TextBlock musicCatalogEmpty = new() { Text = "Загружаем библиотеку…", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly ProgressRing musicLoading = new() { Width = 22, Height = 22, IsActive = false, Visibility = Visibility.Collapsed };
    private readonly ListView musicHistoryList = new() { DisplayMemberPath = "Display", SelectionMode = ListViewSelectionMode.None };
    private readonly TextBlock musicHistoryEmpty = new() { Text = "Здесь появятся треки, которые играли при открытом XASS.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly MusicSessionHistory musicHistory = new();
    private readonly Pivot musicSections = new() { SelectedIndex = 2 };
    private readonly ComboBox musicSource = new() { HorizontalAlignment = HorizontalAlignment.Stretch, SelectedIndex = 0 };
    private readonly Grid musicLocalView = new() { RowSpacing = 8 }, musicServerView = new() { RowSpacing = 8 };
    private readonly TextBlock musicLyricStatus = new() { Text = "Текст из текущего трека", FontSize = 12, Opacity = 0.65, TextWrapping = TextWrapping.Wrap };
    private readonly ToggleButton musicFollow = new() { Content = "Следить за текстом", IsChecked = true, IsEnabled = false };
    private readonly LinearGradientBrush musicBackdrop = new() { StartPoint = new Point(0,0), EndPoint = new Point(1,1) };
    private CoverPalette? coverPalette;
    private bool musicWide;
    private bool musicLyricRevealPending;
    private StackPanel musicLabels = null!;
    private Border musicDetails = null!;

    private Button MusicIcon(string glyph, string label, string id, RoutedEventHandler click, bool primary = false)
    {
        var button = new Button { Content = new FontIcon { Glyph = glyph, FontSize = primary ? 25 : 18 },
            Width = primary ? 58 : 42, Height = primary ? 58 : 42, CornerRadius = new CornerRadius(29),
            Style = (Style)Application.Current.Resources[primary ? "AccentButtonStyle" : "SubtleButtonStyle"] };
        AutomationProperties.SetName(button, label); AutomationProperties.SetAutomationId(button, id);
        ToolTipService.SetToolTip(button, label); button.Click += click; return button;
    }
    private static Grid MusicRows(params GridLength[] rows)
    {
        var grid = new Grid { RowSpacing = 10 };
        foreach (var row in rows) grid.RowDefinitions.Add(new RowDefinition { Height = row });
        return grid;
    }
    private void BuildMusicWorkspace()
    {
        MusicPage.Children.Clear(); MusicPage.RowDefinitions.Clear();
        MusicPage.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        MusicPage.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        var background = new Border { Background = musicBackdrop, CornerRadius = new CornerRadius(24) };
        Grid.SetRowSpan(background, 2); MusicPage.Children.Add(background);
        var header = new Controls.FlowPanel { Margin = new Thickness(20,16,20,0), VerticalSpacing = 8 };
        var title = DLabel("МУЗЫКА", 13); title.CharacterSpacing = 160; title.VerticalAlignment = VerticalAlignment.Center; title.Margin = new Thickness(0,0,16,0);
        header.Children.Add(title);
        musicOpen = MusicButton("Открыть файлы", PickDesktopMusic); AutomationProperties.SetAutomationId(musicOpen, "MusicOpenFiles");
        header.Children.Add(musicOpen);
        header.Children.Add(MusicButton("Библиотека", (_, _) => musicSections.SelectedIndex = 0));
        var discord = MusicButton("Discord", (_, _) => { }); AutomationProperties.SetAutomationId(discord, "MusicDiscordSettings");
        discord.Flyout = new Flyout { Content = BuildDiscordPresenceControl(), Placement = FlyoutPlacementMode.BottomEdgeAlignedLeft };
        header.Children.Add(discord); MusicPage.Children.Add(header);

        musicWorkspace.Margin = new Thickness(20,12,20,20); Grid.SetRow(musicWorkspace,1); MusicPage.Children.Add(musicWorkspace);
        for (int i=0;i<3;i++) musicWorkspace.RowDefinitions.Add(new RowDefinition());
        for (int i=0;i<2;i++) musicWorkspace.ColumnDefinitions.Add(new ColumnDefinition());
        for (int i=0;i<2;i++) { musicHero.RowDefinitions.Add(new RowDefinition()); musicHero.ColumnDefinitions.Add(new ColumnDefinition()); }
        var art = new Grid();
        art.Children.Add(new Border { Background = new SolidColorBrush(Microsoft.UI.Colors.Gray), Opacity = 0.13, CornerRadius = new CornerRadius(20) });
        var emptyArt = new StackPanel { HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Spacing = 12 };
        emptyArt.Children.Add(new FontIcon { Glyph = "\uE8D6", FontSize = 54 });
        emptyArt.Children.Add(new TextBlock { Text = "XASS", CharacterSpacing = 180, FontSize = 12, HorizontalAlignment = HorizontalAlignment.Center, Opacity = 0.55 });
        art.Children.Add(emptyArt); art.Children.Add(musicCover); musicArtFrame.Child = art; musicHero.Children.Add(musicArtFrame);
        AutomationProperties.SetName(musicArtFrame, "Обложка текущего трека");
        AutomationProperties.SetAutomationId(musicTitle,"MusicTitle");
        AutomationProperties.SetAutomationId(musicStatus,"MusicStatus");
        AutomationProperties.SetAutomationId(musicElapsed,"MusicElapsed");
        AutomationProperties.SetAutomationId(musicDuration,"MusicDuration");
        musicTitle.FontWeight = Microsoft.UI.Text.FontWeights.SemiBold; musicTitle.MaxLines = 3;
        musicLabels = new StackPanel { Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
        musicLabels.Children.Add(musicTitle); musicLabels.Children.Add(musicArtist);
        var status = new Controls.FlowPanel(); status.Children.Add(musicLoading); status.Children.Add(musicStatus);
        musicStatus.FontSize = 12; musicStatus.Opacity = 0.7; musicLabels.Children.Add(status); musicLabels.Children.Add(musicError);
        musicHero.Children.Add(musicLabels); musicWorkspace.Children.Add(musicHero);
        AutomationProperties.SetLiveSetting(musicError, Microsoft.UI.Xaml.Automation.Peers.AutomationLiveSetting.Polite);
        BuildMusicTransport(); musicWorkspace.Children.Add(musicTransport);
        BuildMusicSections();
        musicDetails = new Border { Style = (Style)Application.Current.Resources["XassCard"], Padding = new Thickness(16,4,16,12), Child = musicSections };
        musicWorkspace.Children.Add(musicDetails);
        musicWorkspace.SizeChanged += (_, e) => UpdateMusicLayout(e.NewSize);
        musicTransport.SizeChanged += (_, _) => UpdateMusicLayout(new Size(musicWorkspace.ActualWidth, musicWorkspace.ActualHeight));
        RootGrid.ActualThemeChanged += (_, _) => UpdateMusicBackdrop();
        UpdateMusicBackdrop();
    }

    private void BuildMusicTransport()
    {
        foreach(var height in new[] { GridLength.Auto, GridLength.Auto, GridLength.Auto }) musicTransport.RowDefinitions.Add(new RowDefinition { Height = height });
        musicSeek.Margin = new Thickness(0); AutomationProperties.SetAutomationId(musicSeek, "MusicSeek");
        AutomationProperties.SetName(musicSeek, "Позиция трека в секундах"); musicTransport.Children.Add(musicSeek);
        var clocks = new Grid(); clocks.Children.Add(musicElapsed); clocks.Children.Add(musicDuration); Grid.SetRow(clocks,1); musicTransport.Children.Add(clocks);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 5, HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0,4,0,0) };
        musicPrevious = MusicIcon("\uE892", "Предыдущий трек", "MusicPrevious", async (_, _) => await MusicActionAsync(new { action = "previous" }));
        musicNext = MusicIcon("\uE893", "Следующий трек", "MusicNext", async (_, _) => await MusicActionAsync(new { action = "next" }));
        musicPause = MusicIcon("\uE769", "Пауза", "MusicPause", async (_, _) => await MusicActionAsync(new { action = "pause" }), true);
        musicResume = MusicIcon("\uE768", "Продолжить или повторить", "MusicResume", async (_, _) => await MusicActionAsync(new { action = "resume" }), true);
        musicPlay = MusicIcon("\uE768", "Включить выбранный трек", "MusicPlay", async (_, _) => await PlayMusicSelectionAsync(), true);
        var primary = new Grid(); primary.Children.Add(musicPlay); primary.Children.Add(musicPause); primary.Children.Add(musicResume);
        musicStop = MusicIcon("\uE71A", "Стоп и сброс", "MusicStop", async (_, _) => await MusicActionAsync(new { action = "reset" }));
        var volume = MusicIcon("\uE767", "Громкость", "MusicVolumeMenu", (_, _) => { });
        musicVolume.Width = 230; AutomationProperties.SetAutomationId(musicVolume,"MusicVolume");
        volume.Flyout = new Flyout { Content = DStack(musicVolume, MusicButton("Применить громкость", async (_, _) => { await CommitMusicVolumeAsync(); volume.Flyout?.Hide(); })) };
        foreach(var control in new UIElement[] { musicPrevious, primary, musicNext, musicStop, volume }) buttons.Children.Add(control);
        Grid.SetRow(buttons,2); musicTransport.Children.Add(buttons);
        musicSeek.ValueChanged += (_, _) => { if (!musicUpdating) musicSeekDirty = true; };
        musicVolume.ValueChanged += (_, _) => { if (!musicUpdating) musicVolumeDirty = true; };
        musicSeek.KeyUp += async (_, e) => { if (IsMusicSliderKey(e.Key)) await CommitMusicSeekAsync(); };
        musicVolume.KeyUp += async (_, e) => { if (IsMusicSliderKey(e.Key)) await CommitMusicVolumeAsync(); };
        musicSeek.PointerCaptureLost += async (_, _) => { if (musicSeekDirty) await CommitMusicSeekAsync(); };
        musicVolume.PointerCaptureLost += async (_, _) => { if (musicVolumeDirty) await CommitMusicVolumeAsync(); };
        musicTransport.KeyDown += async (_, e) =>
        {
            if (e.Key != VirtualKey.Space || e.OriginalSource is Button or Slider or TextBox) return;
            e.Handled = true;
            if (musicState == "playing") await MusicActionAsync(new { action = "pause" });
            else if (musicState is "paused" or "ended") await MusicActionAsync(new { action = "resume" });
            else await PlayMusicSelectionAsync();
        };
    }

    private void BuildMusicSections()
    {
        var star = new GridLength(1, GridUnitType.Star);
        var queue = MusicRows(GridLength.Auto, star);
        musicSource.Items.Add(new ComboBoxItem { Content = "На компьютере" }); musicSource.Items.Add(new ComboBoxItem { Content = "Библиотека сервера" }); musicSource.SelectedIndex = 0;
        AutomationProperties.SetName(musicSource, "Источник очереди"); AutomationProperties.SetAutomationId(musicSource, "MusicQueueSource"); queue.Children.Add(musicSource);
        var queueBody = new Grid(); Grid.SetRow(queueBody,1); queue.Children.Add(queueBody);
        foreach(var view in new[] { musicLocalView, musicServerView }) { view.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); view.RowDefinitions.Add(new RowDefinition { Height = star }); view.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); queueBody.Children.Add(view); }
        var localCaption = DLabel("До 40 сохранённых файлов · двойной щелчок для запуска",12); localCaption.Opacity = .65; musicLocalView.Children.Add(localCaption);
        var localList = new Grid(); localList.Children.Add(musicQueue); localList.Children.Add(musicQueueEmpty); Grid.SetRow(localList,1); musicLocalView.Children.Add(localList);
        musicLocalPlay = MusicButton("Включить выбранный", async (_, _) => await PlayLocalSelectionAsync()); Grid.SetRow(musicLocalPlay,2); musicLocalView.Children.Add(musicLocalPlay);
        var search = new Grid { ColumnSpacing = 8 }; search.ColumnDefinitions.Add(new ColumnDefinition { Width = star }); search.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        search.Children.Add(musicSearch); var find = MusicButton("Найти", async (_, _) => await SearchDesktopMusicAsync()); Grid.SetColumn(find,1); search.Children.Add(find); musicServerView.Children.Add(search);
        AutomationProperties.SetName(musicSearch,"Поиск музыки сервера"); musicSearch.KeyDown += async (_, e) => { if(e.Key == VirtualKey.Enter) { e.Handled=true; await SearchDesktopMusicAsync(); } };
        var catalog = new Grid(); catalog.Children.Add(musicTracks); catalog.Children.Add(musicCatalogEmpty); Grid.SetRow(catalog,1); musicServerView.Children.Add(catalog);
        var paging = new Controls.FlowPanel();
        musicBackPage = MusicButton("Назад", async (_, _) => { if(musicPreviousOffsets.Count>0) await LoadDesktopCatalogAsync(musicPreviousOffsets.Peek(),musicQuery,"previous"); });
        musicNextPage = MusicButton("Далее", async (_, _) => { if(musicNextOffset is int next) await LoadDesktopCatalogAsync(next,musicQuery,"next"); });
        paging.Children.Add(musicBackPage); paging.Children.Add(musicNextPage); paging.Children.Add(musicCatalogCount);
        paging.Children.Add(MusicButton("Включить выбранный", async (_, _) => await PlayCatalogSelectionAsync())); Grid.SetRow(paging,2); musicServerView.Children.Add(paging);
        void SelectSource() { musicLocalView.Visibility = musicSource.SelectedIndex == 0 ? Visibility.Visible : Visibility.Collapsed; musicServerView.Visibility = musicSource.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed; UpdateDesktopMusicControls(); }
        musicSource.SelectionChanged += (_, _) => SelectSource(); SelectSource();
        AutomationProperties.SetName(musicQueue,"Очередь локальных файлов"); AutomationProperties.SetAutomationId(musicQueue,"MusicLocalQueue");
        AutomationProperties.SetName(musicTracks,"Библиотека сервера"); AutomationProperties.SetAutomationId(musicTracks,"MusicServerLibrary");
        foreach(var list in new[] { musicQueue, musicTracks }) list.ItemContainerStyle = MusicRowStyle();
        musicTracks.SelectionChanged += (_, _) => UpdateDesktopMusicControls(); musicQueue.SelectionChanged += (_, _) => UpdateDesktopMusicControls();
        musicTracks.DoubleTapped += async (_, _) => await PlayCatalogSelectionAsync(); musicQueue.DoubleTapped += async (_, _) => await PlayLocalSelectionAsync();
        musicTracks.KeyDown += async (_, e) => { if(e.Key == VirtualKey.Enter) { e.Handled=true; await PlayCatalogSelectionAsync(); } };
        musicQueue.KeyDown += async (_, e) => { if(e.Key == VirtualKey.Enter) { e.Handled=true; await PlayLocalSelectionAsync(); } };
        musicSections.Items.Add(new PivotItem { Header = "Очередь", Content = queue });
        var history = MusicRows(GridLength.Auto,star); history.Children.Add(DLabel("В этом запуске XASS · последние 50 треков",12));
        var historyBody = new Grid(); historyBody.Children.Add(musicHistoryList); historyBody.Children.Add(musicHistoryEmpty); Grid.SetRow(historyBody,1); history.Children.Add(historyBody);
        musicHistoryList.ItemContainerStyle = MusicRowStyle(); AutomationProperties.SetName(musicHistoryList,"История прослушивания этого запуска");
        AutomationProperties.SetAutomationId(musicHistoryList,"MusicHistory");
        musicSections.Items.Add(new PivotItem { Header = "История", Content = history });
        var lyrics = MusicRows(GridLength.Auto,star); var lyricTop = new Controls.FlowPanel(); lyricTop.Children.Add(musicFollow); lyricTop.Children.Add(musicLyricStatus); lyrics.Children.Add(lyricTop);
        musicLyrics.MaxHeight = double.PositiveInfinity; musicLyrics.IsTabStop = true;
        musicLyrics.ItemTemplate = (DataTemplate)XamlReader.Load("<DataTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'><TextBlock Text='{Binding Text}' TextWrapping='Wrap' FontSize='25' FontWeight='SemiBold' LineHeight='35' Margin='4,10'/></DataTemplate>");
        musicLyrics.ItemContainerStyle = MusicRowStyle(); AutomationProperties.SetName(musicLyrics,"Текст песни; текущая строка выделена"); AutomationProperties.SetAutomationId(musicLyrics,"MusicLyrics");
        musicLyrics.SizeChanged += (_, _) => QueueMusicLyricReveal();
        musicFollow.Checked += (_, _) => QueueMusicLyricReveal();
        musicSections.SelectionChanged += (_, _) => QueueMusicLyricReveal();
        Grid.SetRow(musicLyrics,1); lyrics.Children.Add(musicLyrics); musicSections.Items.Add(new PivotItem { Header = "Текст", Content = lyrics });
        AutomationProperties.SetAutomationId(musicSections,"MusicSections");
    }

    private static Style MusicRowStyle()
    {
        var style = new Style(typeof(ListViewItem)); style.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty,HorizontalAlignment.Stretch));
        style.Setters.Add(new Setter(Control.PaddingProperty,new Thickness(8,8,8,8))); return style;
    }
    private Task PlayMusicSelectionAsync() => musicSource.SelectedIndex == 0 ? PlayLocalSelectionAsync() : PlayCatalogSelectionAsync();

    private void QueueMusicLyricReveal()
    {
        if (closed || musicLyricRevealPending || musicFollow.IsChecked != true || musicLyrics.SelectedItem is null) return;
        musicLyricRevealPending = true;
        DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
        {
            musicLyricRevealPending = false;
            if (!closed && musicSections.SelectedIndex == 2 && musicFollow.IsChecked == true && musicLyrics.SelectedItem is object current)
                musicLyrics.ScrollIntoView(current, ScrollIntoViewAlignment.Leading);
        });
    }

    private void UpdateMusicLayout(Size size)
    {
        if (musicDetails is null || size.Width <= 0 || size.Height <= 0) return;
        musicWide = size.Width >= 760 && size.Height >= 430;
        musicWorkspace.ColumnSpacing = musicWide ? 28 : 0;
        musicWorkspace.ColumnDefinitions[0].Width = new GridLength(musicWide ? .95 : 1,GridUnitType.Star);
        musicWorkspace.ColumnDefinitions[1].Width = musicWide ? new GridLength(1.1,GridUnitType.Star) : new GridLength(0);
        musicWorkspace.RowDefinitions[0].Height = musicWide ? new GridLength(1,GridUnitType.Star) : GridLength.Auto;
        musicWorkspace.RowDefinitions[1].Height = musicWide ? GridLength.Auto : new GridLength(1,GridUnitType.Star);
        musicWorkspace.RowDefinitions[2].Height = musicWide ? new GridLength(0) : GridLength.Auto;
        Grid.SetRow(musicTransport,musicWide ? 1 : 2); Grid.SetColumn(musicDetails,musicWide ? 1 : 0); Grid.SetRow(musicDetails,musicWide ? 0 : 1); Grid.SetRowSpan(musicDetails,musicWide ? 2 : 1);
        musicHero.ColumnDefinitions[0].Width = musicWide ? new GridLength(1,GridUnitType.Star) : GridLength.Auto;
        musicHero.ColumnDefinitions[1].Width = musicWide ? new GridLength(0) : new GridLength(1,GridUnitType.Star);
        musicHero.RowDefinitions[0].Height = musicWide ? new GridLength(1,GridUnitType.Star) : GridLength.Auto;
        musicHero.RowDefinitions[1].Height = musicWide ? GridLength.Auto : new GridLength(0);
        Grid.SetRow(musicLabels,musicWide ? 1 : 0); Grid.SetColumn(musicLabels,musicWide ? 0 : 1);
        double art = musicWide ? Math.Clamp(Math.Min(size.Width*.43, size.Height - Math.Max(110,musicTransport.ActualHeight) - 130),150,420) : size.Height < 430 ? 72 : 100;
        musicArtFrame.Width = musicArtFrame.Height = art; musicCover.Width = musicCover.Height = art;
        musicArtFrame.VerticalAlignment = musicWide ? VerticalAlignment.Center : VerticalAlignment.Top;
        musicTitle.FontSize = musicWide ? 28 : 21; musicTitle.MaxLines = musicWide ? 3 : 2;
        musicArtist.FontSize = musicWide ? 17 : 14; musicArtist.MaxLines = 2;
    }
    private void UpdateMusicBackdrop()
    {
        bool dark = RootGrid.ActualTheme == ElementTheme.Dark;
        uint surface = dark ? 0x16191Fu : 0xFAFAFCu;
        uint fallback = AccentPalette.TryParse(appearance.Accent,out uint custom) ? custom : 0x75658Fu;
        var palette = coverPalette ?? new CoverPalette(fallback, fallback);
        musicBackdrop.GradientStops.Clear();
        if (appearanceAccessibility.HighContrast)
        {
            var background = appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.Window);
            musicBackdrop.GradientStops.Add(new GradientStop { Offset = 0, Color = background });
            musicBackdrop.GradientStops.Add(new GradientStop { Offset = 1, Color = background });
            return;
        }
        // Keep small secondary text readable even with an all-white cover.
        musicBackdrop.GradientStops.Add(new GradientStop { Offset = 0, Color = ToColor(CoverPalette.Blend(palette.Primary,surface,dark ? .20 : .13)) });
        musicBackdrop.GradientStops.Add(new GradientStop { Offset = 1, Color = ToColor(CoverPalette.Blend(palette.Secondary,surface,dark ? .12 : .06)) });
    }
}
