using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using Windows.Foundation;
using Windows.System;
using Xass.Native.Services;

namespace Xass.Native;

/// <summary>Original XASS signal-deck design: restrained orbital geometry, acid mint signal,
/// and an ink/violet atmosphere. Artwork and transport remain real, accessible controls.</summary>
public sealed partial class MainWindow
{
        private readonly Grid musicWorkspace = new() { ColumnSpacing = 20, RowSpacing = 16 };
    private readonly Grid musicHero = new() { ColumnSpacing = 22, RowSpacing = 12 };
    private readonly Grid musicTransport = new() { ColumnSpacing = 16, RowSpacing = 4 };
    private readonly Border musicTransportCard = new() { Background = new SolidColorBrush(ToColor(0xE6191A22)), BorderBrush = new SolidColorBrush(ToColor(0x713B3E49)), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(20), Padding = new Thickness(18,10,18,10) };
    private readonly Border musicArtFrame = new() { CornerRadius = new CornerRadius(18), HorizontalAlignment = HorizontalAlignment.Center, BorderThickness = new Thickness(1) };
    private readonly TextBlock musicArtist = new() { FontSize = 16, TextWrapping = TextWrapping.Wrap, Opacity = 0.82 };
    private readonly TextBlock musicKicker = new() { Text = "СИГНАЛ · СЕЙЧАС", FontSize = 10, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold, CharacterSpacing = 190 };
    private readonly TextBlock musicElapsed = new() { Text = "0:00", FontSize = 11, Opacity = 0.76 };
    private readonly TextBlock musicDuration = new() { Text = "0:00", FontSize = 11, Opacity = 0.76, HorizontalAlignment = HorizontalAlignment.Right };
    private readonly TextBlock musicQueueEmpty = new() { Text = "В локальном контуре пока нет файлов. Добавьте музыку или переключитесь на серверную библиотеку.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly TextBlock musicCatalogEmpty = new() { Text = "Ожидание библиотеки…", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly ProgressRing musicLoading = new() { Width = 16, Height = 16, IsActive = false, Visibility = Visibility.Collapsed };
    private readonly ListView musicHistoryList = new() { DisplayMemberPath = "Display", SelectionMode = ListViewSelectionMode.None };
    private readonly TextBlock musicHistoryEmpty = new() { Text = "После первого прослушивания здесь появится история этого сеанса.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12,24,12,0) };
    private readonly MusicSessionHistory musicHistory = new();
    private readonly Pivot musicSections = new() { SelectedIndex = 0 };
    private readonly ComboBox musicSource = new() { HorizontalAlignment = HorizontalAlignment.Stretch, SelectedIndex = 0 };
    private readonly Grid musicLocalView = new() { RowSpacing = 8 }, musicServerView = new() { RowSpacing = 8 };
    private readonly TextBlock musicLyricStatus = new() { Text = "Синхронизация текста с треком", FontSize = 11, Opacity = 0.72, TextWrapping = TextWrapping.Wrap };
    private readonly ToggleButton musicFollow = new() { Content = "Автоследование", IsChecked = true, IsEnabled = false };
    private readonly LinearGradientBrush musicBackdrop = new() { StartPoint = new Point(0,0), EndPoint = new Point(1,1) };
    private CoverPalette? coverPalette;
    private bool musicWide;
    private bool musicLyricRevealPending;
    private StackPanel musicLabels = null!;
    private Border musicDetails = null!;
    private Grid? musicHeaderGrid;
    private Grid musicLibraryPane = null!;
    private Grid musicTransportTimeline = null!;
    private StackPanel musicTransportButtons = null!;
    private StackPanel musicTransportControls = null!;
    private Grid musicArtworkStage = null!;
    private Ellipse musicArtworkAura = null!, musicOuterOrbit = null!, musicInnerOrbit = null!;
    private TextBlock? musicBrandTitle, musicBrandSubtitle;

    private Button MusicIcon(string glyph, string label, string id, RoutedEventHandler click, bool primary = false)
    {
        var button = new Button { Content = new FontIcon { Glyph = glyph, FontSize = primary ? 23 : 17 }, Width = primary ? 54 : 40, Height = primary ? 54 : 40,
            CornerRadius = new CornerRadius(18), Style = (Style)Application.Current.Resources[primary ? "AccentButtonStyle" : "SubtleButtonStyle"] };
        AutomationProperties.SetName(button,label); AutomationProperties.SetAutomationId(button,id); ToolTipService.SetToolTip(button,label); button.Click += click; return button;
    }
    private static Grid MusicRows(params GridLength[] rows)
    {
        var grid = new Grid { RowSpacing = 9 }; foreach (var row in rows) grid.RowDefinitions.Add(new RowDefinition { Height = row }); return grid;
    }
    private static TextBlock MusicEyebrow(string text) => new() { Text = text, FontSize = 10, CharacterSpacing = 150,
        FontWeight = Microsoft.UI.Text.FontWeights.SemiBold, Foreground = new SolidColorBrush(ToColor(0xFFB9FF65)) };
    private static Border MusicPanel(UIElement child, double radius = 18) => new() { Child = child, CornerRadius = new CornerRadius(radius),
        Background = new SolidColorBrush(ToColor(0xD9191A22)), BorderBrush = new SolidColorBrush(ToColor(0x713B3E49)),
        BorderThickness = new Thickness(1), Padding = new Thickness(16) };
    private void BuildMusicWorkspace()
    {
        MusicPage.Children.Clear(); MusicPage.RowDefinitions.Clear();
        MusicPage.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        MusicPage.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        MusicPage.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var atmosphere = new Border { Background=musicBackdrop }; Grid.SetRowSpan(atmosphere,3); MusicPage.Children.Add(atmosphere);
        var headerGrid = new Grid { Margin = new Thickness(20,12,20,0), ColumnSpacing = 18, RequestedTheme = ElementTheme.Dark };
        musicHeaderGrid = headerGrid; headerGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headerGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1,GridUnitType.Star) });
        headerGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var mark = new Grid { Width = 38, Height = 38, VerticalAlignment = VerticalAlignment.Center };
        mark.Children.Add(new Ellipse { Stroke = new SolidColorBrush(ToColor(0xFFB9FF65)), StrokeThickness = 1.3, Margin = new Thickness(2) });
        mark.Children.Add(new Ellipse { Stroke = new SolidColorBrush(ToColor(0xFFB9FF65)), StrokeThickness = 1, Margin = new Thickness(8,0,8,0), Opacity = .55 });
        mark.Children.Add(new Ellipse { Fill = new SolidColorBrush(ToColor(0xFFB9FF65)), Width = 5, Height = 5 });
        AutomationProperties.SetName(mark,"XASS · аудиоконтур"); headerGrid.Children.Add(mark);
        var masthead = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(12,0,0,0) };
        Grid.SetColumn(masthead,1); var title = MusicEyebrow("XASS  /  AUDIO SYSTEM"); masthead.Children.Add(title); musicBrandTitle = title;
        var subtitle = new TextBlock { Text = "Локальная и серверная библиотека · единый плеер", FontSize = 11, Foreground = new SolidColorBrush(ToColor(0xFFAAAAB7)) };
        masthead.Children.Add(subtitle); musicBrandSubtitle = subtitle; headerGrid.Children.Add(masthead);
        var header = new Controls.FlowPanel { VerticalSpacing = 7, HorizontalSpacing = 7, VerticalAlignment = VerticalAlignment.Center };
        musicOpen = MusicButton("＋  Добавить", PickDesktopMusic); AutomationProperties.SetAutomationId(musicOpen,"MusicOpenFiles"); header.Children.Add(musicOpen);
        var discord = MusicButton("Связь", (_, _) => { }); AutomationProperties.SetAutomationId(discord,"MusicDiscordSettings");
        discord.Flyout = new Flyout { Content = BuildDiscordPresenceControl(), Placement = FlyoutPlacementMode.BottomEdgeAlignedLeft }; header.Children.Add(discord);
        Grid.SetColumn(header,2); headerGrid.Children.Add(header); Grid.SetRow(headerGrid,0); MusicPage.Children.Add(headerGrid);

        musicWorkspace.Margin = new Thickness(20,14,20,10); musicWorkspace.RequestedTheme = ElementTheme.Dark;
        for (int i=0;i<3;i++) musicWorkspace.RowDefinitions.Add(new RowDefinition());
        musicWorkspace.ColumnDefinitions.Add(new ColumnDefinition()); musicWorkspace.ColumnDefinitions.Add(new ColumnDefinition());
        Grid.SetRow(musicWorkspace,1); MusicPage.Children.Add(musicWorkspace);

        var libraryPanel = musicLibraryPane = new Grid { RowSpacing = 10 };
        libraryPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); libraryPanel.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1,GridUnitType.Star) });
        var libraryTop = new Grid { ColumnSpacing = 10 }; libraryTop.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1,GridUnitType.Star) }); libraryTop.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var libraryHeading = new StackPanel { Spacing = 3 }; libraryHeading.Children.Add(MusicEyebrow("01  /  ИСТОЧНИКИ"));
        libraryHeading.Children.Add(new TextBlock { Text = "Музыкальная карта", FontSize = 21, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold }); libraryTop.Children.Add(libraryHeading);
        musicSource.Items.Add(new ComboBoxItem { Content = "МОИ ФАЙЛЫ" }); musicSource.Items.Add(new ComboBoxItem { Content = "СЕРВЕР" }); musicSource.SelectedIndex=0;
        musicSource.MinWidth=128; musicSource.FontSize=10; AutomationProperties.SetName(musicSource,"Источник очереди"); AutomationProperties.SetAutomationId(musicSource,"MusicQueueSource");
        Grid.SetColumn(musicSource,1); libraryTop.Children.Add(musicSource); libraryPanel.Children.Add(libraryTop);
        BuildMusicSections();
        musicDetails = MusicPanel(musicSections); musicDetails.Padding = new Thickness(10,6,10,10);
        Grid.SetRow(musicDetails,1); libraryPanel.Children.Add(musicDetails); Grid.SetColumn(libraryPanel,0); musicWorkspace.Children.Add(libraryPanel);

        musicHero.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1,GridUnitType.Star) });
        musicHero.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        musicHero.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        musicHero.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1,GridUnitType.Star) });
        musicHero.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1,GridUnitType.Star) });
        var artStage = musicArtworkStage = new Grid { HorizontalAlignment=HorizontalAlignment.Stretch, VerticalAlignment=VerticalAlignment.Stretch, MinHeight=130 };
        var aura = musicArtworkAura = new Ellipse { Width=260, Height=260, HorizontalAlignment=HorizontalAlignment.Center, VerticalAlignment=VerticalAlignment.Center,
            Fill=new RadialGradientBrush { GradientStops = { new GradientStop { Color=ToColor(0x4C974BFA), Offset=0 }, new GradientStop { Color=ToColor(0x007C4DDB), Offset=1 } } } };
        artStage.Children.Add(aura);
        var orbit1 = musicOuterOrbit = new Ellipse { Width=224, Height=224, HorizontalAlignment=HorizontalAlignment.Center, VerticalAlignment=VerticalAlignment.Center, Stroke=new SolidColorBrush(ToColor(0x47B9FF65)), StrokeThickness=1 };
        var orbit2 = musicInnerOrbit = new Ellipse { Width=198, Height=198, HorizontalAlignment=HorizontalAlignment.Center, VerticalAlignment=VerticalAlignment.Center, Stroke=new SolidColorBrush(ToColor(0x387D83A1)), StrokeThickness=1, StrokeDashArray=new DoubleCollection { 2,4 } };
        artStage.Children.Add(orbit1); artStage.Children.Add(orbit2);
        var art = new Grid { HorizontalAlignment=HorizontalAlignment.Stretch, VerticalAlignment=VerticalAlignment.Stretch };
        art.Children.Add(new Border { Background=new SolidColorBrush(ToColor(0xFF252632)), CornerRadius=new CornerRadius(14) });
        art.Children.Add(new FontIcon { Glyph="\uE8D6", FontSize=38, HorizontalAlignment=HorizontalAlignment.Center, VerticalAlignment=VerticalAlignment.Center, Foreground=new SolidColorBrush(ToColor(0xFFA4A5B5)) });
        art.Children.Add(musicCover); musicCover.HorizontalAlignment=HorizontalAlignment.Stretch; musicCover.VerticalAlignment=VerticalAlignment.Stretch;
        musicArtFrame.Width=182; musicArtFrame.Height=182; musicArtFrame.Padding=new Thickness(3); musicArtFrame.Background=new SolidColorBrush(ToColor(0xFF15161D));
        musicArtFrame.BorderBrush=new SolidColorBrush(ToColor(0x75B9FF65)); musicArtFrame.Child=art; artStage.Children.Add(musicArtFrame);
        Grid.SetColumnSpan(artStage,2); musicHero.Children.Add(artStage); AutomationProperties.SetName(musicArtFrame,"Обложка текущего трека");

        musicLabels=new StackPanel { Spacing=11, VerticalAlignment=VerticalAlignment.Center, Margin=new Thickness(8,4,6,4) };
        var index = new Controls.FlowPanel { VerticalSpacing=6, HorizontalSpacing=6 }; index.Children.Add(musicLoading); index.Children.Add(musicKicker); musicLabels.Children.Add(index);
        musicTitle.FontSize=27; musicTitle.FontWeight=Microsoft.UI.Text.FontWeights.SemiBold; musicTitle.MaxLines=3; musicTitle.TextWrapping=TextWrapping.Wrap; musicTitle.TextTrimming=TextTrimming.CharacterEllipsis;
        musicLabels.Children.Add(musicTitle); musicArtist.FontSize=15; musicLabels.Children.Add(musicArtist);
        musicLabels.Children.Add(new Border { Height=1, Background=new SolidColorBrush(ToColor(0x536A6B77)), Margin=new Thickness(0,2,18,2) });
        var station = new Controls.FlowPanel { VerticalSpacing=5, HorizontalSpacing=7 }; station.Children.Add(new Ellipse { Width=6, Height=6, Fill=new SolidColorBrush(ToColor(0xFFB9FF65)), VerticalAlignment=VerticalAlignment.Center });
        musicStatus.FontSize=11; musicStatus.Foreground=new SolidColorBrush(ToColor(0xFFC4C3CF)); station.Children.Add(musicStatus); musicLabels.Children.Add(station); musicLabels.Children.Add(musicError);
        Grid.SetColumn(musicLabels,1); musicHero.Children.Add(musicLabels);
        var signature = new Grid { ColumnSpacing=8, Margin=new Thickness(2,4,2,0) }; signature.ColumnDefinitions.Add(new ColumnDefinition()); signature.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        signature.Children.Add(MusicEyebrow("XASS · ПЛЕЕР")); var decor = new TextBlock { Text="LOCAL / SERVER", FontSize=9, CharacterSpacing=90, Foreground=new SolidColorBrush(ToColor(0xFFB9FF65)), Opacity=.85 };
        Grid.SetColumn(decor,1); signature.Children.Add(decor); Grid.SetRow(signature,2); Grid.SetColumnSpan(signature,2); musicHero.Children.Add(signature);
        AutomationProperties.SetAutomationId(musicTitle,"MusicTitle"); AutomationProperties.SetAutomationId(musicStatus,"MusicStatus");
        AutomationProperties.SetAutomationId(musicElapsed,"MusicElapsed"); AutomationProperties.SetAutomationId(musicDuration,"MusicDuration");
        AutomationProperties.SetLiveSetting(musicError,Microsoft.UI.Xaml.Automation.Peers.AutomationLiveSetting.Polite);
        Grid.SetColumn(musicHero,1); musicWorkspace.Children.Add(musicHero);

        BuildMusicTransport(); Grid.SetRow(musicTransportCard,2); Grid.SetColumnSpan(musicTransportCard,2); musicWorkspace.Children.Add(musicTransportCard);
        musicWorkspace.SizeChanged += (_, e) => UpdateMusicLayout(e.NewSize);
        musicTransport.SizeChanged += (_, _) => UpdateMusicLayout(new Size(musicWorkspace.ActualWidth,musicWorkspace.ActualHeight));
        RootGrid.ActualThemeChanged += (_, _) => UpdateMusicBackdrop(); UpdateMusicBackdrop();
    }

    private void BuildMusicTransport()
    {
        musicTransportCard.Child=musicTransport;
        musicTransport.ColumnDefinitions.Add(new ColumnDefinition { Width=new GridLength(1,GridUnitType.Star) });
        musicTransport.ColumnDefinitions.Add(new ColumnDefinition { Width=GridLength.Auto });
        var timeline = musicTransportTimeline = new Grid { RowSpacing=1 }; timeline.RowDefinitions.Add(new RowDefinition { Height=GridLength.Auto }); timeline.RowDefinitions.Add(new RowDefinition { Height=GridLength.Auto });
        var label=MusicEyebrow("02  /  ТРАНСПОРТ · ЭФИР"); timeline.Children.Add(label);
        musicSeek.Margin=new Thickness(0,-3,0,-3); Grid.SetRow(musicSeek,1); AutomationProperties.SetAutomationId(musicSeek,"MusicSeek"); AutomationProperties.SetName(musicSeek,"Позиция трека в секундах"); timeline.Children.Add(musicSeek);
        Grid.SetColumn(timeline,0); musicTransport.Children.Add(timeline);
        var controls = musicTransportControls = new StackPanel { Spacing=5, VerticalAlignment=VerticalAlignment.Center, HorizontalAlignment=HorizontalAlignment.Center };
        var clocks = new Grid { ColumnSpacing=4 }; clocks.ColumnDefinitions.Add(new ColumnDefinition()); clocks.ColumnDefinitions.Add(new ColumnDefinition()); clocks.Children.Add(musicElapsed); Grid.SetColumn(musicDuration,1); clocks.Children.Add(musicDuration); controls.Children.Add(clocks);
        var buttons = musicTransportButtons = new StackPanel { Orientation=Orientation.Horizontal, Spacing=3, HorizontalAlignment=HorizontalAlignment.Center };
        musicPrevious=MusicIcon("\uE892","Предыдущий трек","MusicPrevious",async(_,_)=>await MusicActionAsync(new { action="previous" }));
        musicNext=MusicIcon("\uE893","Следующий трек","MusicNext",async(_,_)=>await MusicActionAsync(new { action="next" }));
        musicPause=MusicIcon("\uE769","Пауза","MusicPause",async(_,_)=>await MusicActionAsync(new { action="pause" }),true);
        musicResume=MusicIcon("\uE768","Продолжить или повторить","MusicResume",async(_,_)=>await MusicActionAsync(new { action="resume" }),true);
        musicPlay=MusicIcon("\uE768","Включить выбранный трек","MusicPlay",async(_,_)=>await PlayMusicSelectionAsync(),true);
        var primary=new Grid(); primary.Children.Add(musicPlay); primary.Children.Add(musicPause); primary.Children.Add(musicResume);
        musicStop=MusicIcon("\uE71A","Стоп и сброс","MusicStop",async(_,_)=>await MusicActionAsync(new { action="reset" }));
        buttons.Children.Add(musicPrevious); buttons.Children.Add(primary); buttons.Children.Add(musicNext); buttons.Children.Add(musicStop); controls.Children.Add(buttons);
        Grid.SetColumn(controls,1); musicTransport.Children.Add(controls);

        musicVolume.Width=160; AutomationProperties.SetAutomationId(musicVolume,"MusicVolume");
        musicVolume.Minimum=0; musicVolume.Maximum=100; musicVolume.Header="Выход · громкость";
        var volumeContent=new StackPanel { Width=178, Spacing=4 }; volumeContent.Children.Add(MusicEyebrow("03  /  ВЫХОД")); volumeContent.Children.Add(musicVolume);
        var volumeButton=MusicIcon("\uE767","Громкость","MusicVolumeMenu",(_,_)=>{ }); volumeButton.Width=36; volumeButton.Height=36;
        var applyVolume=MusicButton("Применить громкость",async(_,_)=>await CommitMusicVolumeAsync());
        volumeButton.Flyout=new Flyout { Content=new StackPanel { Width=220, Spacing=8, Children={ volumeContent, applyVolume } }, Placement=FlyoutPlacementMode.Top };
        buttons.Children.Add(volumeButton);
        musicSeek.ValueChanged+=(_,_)=>{if(!musicUpdating)musicSeekDirty=true;}; musicVolume.ValueChanged+=(_,_)=>{if(!musicUpdating)musicVolumeDirty=true;};
        musicSeek.KeyUp+=async(_,e)=>{if(IsMusicSliderKey(e.Key))await CommitMusicSeekAsync();}; musicVolume.KeyUp+=async(_,e)=>{if(IsMusicSliderKey(e.Key))await CommitMusicVolumeAsync();};
        musicSeek.PointerCaptureLost+=async(_,_)=>{if(musicSeekDirty)await CommitMusicSeekAsync();}; musicVolume.PointerCaptureLost+=async(_,_)=>{if(musicVolumeDirty)await CommitMusicVolumeAsync();};
        musicTransport.KeyDown+=async(_,e)=>{if(e.Key!=VirtualKey.Space||e.OriginalSource is Button or Slider or TextBox)return;e.Handled=true;if(musicState=="playing")await MusicActionAsync(new{action="pause"});else if(musicState is "paused" or "ended")await MusicActionAsync(new{action="resume"});else await PlayMusicSelectionAsync();};
    }

    private void BuildMusicSections()
    {
        var star=new GridLength(1,GridUnitType.Star); var queue=MusicRows(GridLength.Auto,star);
        Grid.SetRow(queue,1);
        var queueBody=new Grid(); Grid.SetRow(queueBody,1); queue.Children.Add(queueBody);
        foreach(var view in new[]{musicLocalView,musicServerView}){view.RowDefinitions.Add(new RowDefinition{Height=GridLength.Auto});view.RowDefinitions.Add(new RowDefinition{Height=star});view.RowDefinitions.Add(new RowDefinition{Height=GridLength.Auto});queueBody.Children.Add(view);}
        var localCaption=MusicEyebrow("ЛОКАЛЬНЫЙ КОНТУР · ДО 40 ФАЙЛОВ");localCaption.Margin=new Thickness(2,0,0,3);musicLocalView.Children.Add(localCaption);
        var localList=new Grid();localList.Children.Add(musicQueue);localList.Children.Add(musicQueueEmpty);Grid.SetRow(localList,1);musicLocalView.Children.Add(localList);
        musicLocalPlay=MusicButton("▶    Воспроизвести выбранное",async(_,_)=>await PlayLocalSelectionAsync());Grid.SetRow(musicLocalPlay,2);musicLocalView.Children.Add(musicLocalPlay);
        var search=new Grid{ColumnSpacing=8};search.ColumnDefinitions.Add(new ColumnDefinition{Width=star});search.ColumnDefinitions.Add(new ColumnDefinition{Width=GridLength.Auto});search.Children.Add(musicSearch);
        var find=MusicButton("Искать",async(_,_)=>await SearchDesktopMusicAsync());Grid.SetColumn(find,1);search.Children.Add(find);musicServerView.Children.Add(search);
        var catalog=new Grid();catalog.Children.Add(musicTracks);catalog.Children.Add(musicCatalogEmpty);Grid.SetRow(catalog,1);musicServerView.Children.Add(catalog);
        var paging=new Controls.FlowPanel();musicBackPage=MusicButton("‹",async(_,_)=>{if(musicPreviousOffsets.Count>0)await LoadDesktopCatalogAsync(musicPreviousOffsets.Peek(),musicQuery,"previous");});
        musicNextPage=MusicButton("›",async(_,_)=>{if(musicNextOffset is int next)await LoadDesktopCatalogAsync(next,musicQuery,"next");});
        paging.Children.Add(musicBackPage);paging.Children.Add(musicNextPage);paging.Children.Add(musicCatalogCount);paging.Children.Add(MusicButton("▶  Включить",async(_,_)=>await PlayCatalogSelectionAsync()));Grid.SetRow(paging,2);musicServerView.Children.Add(paging);
        musicSearch.KeyDown+=async(_,e)=>{if(e.Key==VirtualKey.Enter){e.Handled=true;await SearchDesktopMusicAsync();}};
        void SelectSource(){musicLocalView.Visibility=musicSource.SelectedIndex==0?Visibility.Visible:Visibility.Collapsed;musicServerView.Visibility=musicSource.SelectedIndex==1?Visibility.Visible:Visibility.Collapsed;UpdateDesktopMusicControls();}
        musicSource.SelectionChanged+=(_,_)=>SelectSource();SelectSource();
        AutomationProperties.SetName(musicQueue,"Очередь локальных файлов");AutomationProperties.SetAutomationId(musicQueue,"MusicLocalQueue");
        AutomationProperties.SetName(musicTracks,"Библиотека сервера");AutomationProperties.SetAutomationId(musicTracks,"MusicServerLibrary");
        foreach(var list in new[]{musicQueue,musicTracks})list.ItemContainerStyle=MusicRowStyle();
        musicTracks.SelectionChanged+=(_,_)=>UpdateDesktopMusicControls();musicQueue.SelectionChanged+=(_,_)=>UpdateDesktopMusicControls();
        musicTracks.DoubleTapped+=async(_,_)=>await PlayCatalogSelectionAsync();musicQueue.DoubleTapped+=async(_,_)=>await PlayLocalSelectionAsync();
        musicTracks.KeyDown+=async(_,e)=>{if(e.Key==VirtualKey.Enter){e.Handled=true;await PlayCatalogSelectionAsync();}};musicQueue.KeyDown+=async(_,e)=>{if(e.Key==VirtualKey.Enter){e.Handled=true;await PlayLocalSelectionAsync();}};
        musicSections.Items.Add(new PivotItem{Header="01   ОЧЕРЕДЬ",Content=queue});
        var history=MusicRows(GridLength.Auto,star);history.Children.Add(MusicEyebrow("СЕАНС · ПОСЛЕДНИЕ 50 ТРЕКОВ"));
        var historyBody=new Grid();historyBody.Children.Add(musicHistoryList);historyBody.Children.Add(musicHistoryEmpty);Grid.SetRow(historyBody,1);history.Children.Add(historyBody);
        musicHistoryList.ItemContainerStyle=MusicRowStyle();AutomationProperties.SetName(musicHistoryList,"История прослушивания этого запуска");AutomationProperties.SetAutomationId(musicHistoryList,"MusicHistory");
        musicSections.Items.Add(new PivotItem{Header="02   ИСТОРИЯ",Content=history});
        var lyrics=MusicRows(GridLength.Auto,star);var lyricTop=new Controls.FlowPanel();lyricTop.Children.Add(musicFollow);lyricTop.Children.Add(musicLyricStatus);lyrics.Children.Add(lyricTop);
        musicLyrics.MaxHeight=double.PositiveInfinity;musicLyrics.IsTabStop=true;
        musicLyrics.ItemTemplate=(DataTemplate)XamlReader.Load("<DataTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'><TextBlock Text='{Binding Text}' TextWrapping='Wrap' FontSize='23' FontWeight='SemiBold' LineHeight='33' Margin='4,10'/></DataTemplate>");
        musicLyrics.ItemContainerStyle=MusicRowStyle();AutomationProperties.SetName(musicLyrics,"Текст песни; текущая строка выделена");AutomationProperties.SetAutomationId(musicLyrics,"MusicLyrics");musicLyrics.SizeChanged+=(_,_)=>QueueMusicLyricReveal();musicFollow.Checked+=(_,_)=>QueueMusicLyricReveal();musicSections.SelectionChanged+=(_,_)=>QueueMusicLyricReveal();
        Grid.SetRow(musicLyrics,1);lyrics.Children.Add(musicLyrics);musicSections.Items.Add(new PivotItem{Header="03   ТЕКСТ",Content=lyrics});AutomationProperties.SetAutomationId(musicSections,"MusicSections");
    }
    private static Style MusicRowStyle(){var style=new Style(typeof(ListViewItem));style.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty,HorizontalAlignment.Stretch));style.Setters.Add(new Setter(Control.PaddingProperty,new Thickness(8,8,8,8)));return style;}
    private Task PlayMusicSelectionAsync()=>musicSource.SelectedIndex==0?PlayLocalSelectionAsync():PlayCatalogSelectionAsync();
    private void QueueMusicLyricReveal(){if(closed||musicLyricRevealPending||musicFollow.IsChecked!=true||musicLyrics.SelectedItem is null)return;musicLyricRevealPending=true;DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low,()=>{musicLyricRevealPending=false;if(!closed&&musicSections.SelectedIndex==2&&musicFollow.IsChecked==true&&musicLyrics.SelectedItem is object current)musicLyrics.ScrollIntoView(current,ScrollIntoViewAlignment.Leading);});}
    private void UpdateMusicLayout(Size size)
    {
        if(musicDetails is null||size.Width<=0||size.Height<=0)return;
        musicWide=size.Width>=920&&size.Height>=500;
        musicWorkspace.ColumnSpacing=musicWide?20:0;
        musicWorkspace.ColumnDefinitions[0].Width=new GridLength(musicWide?1.08:1,GridUnitType.Star);
        musicWorkspace.ColumnDefinitions[1].Width=musicWide?new GridLength(.92,GridUnitType.Star):new GridLength(0);
        musicWorkspace.RowDefinitions[0].Height=musicWide?new GridLength(1,GridUnitType.Star):GridLength.Auto;
        musicWorkspace.RowDefinitions[1].Height=musicWide?new GridLength(0):new GridLength(1,GridUnitType.Star);
        musicWorkspace.RowDefinitions[2].Height=GridLength.Auto;
        Grid.SetRow(musicHero,0);Grid.SetColumn(musicHero,musicWide?1:0);Grid.SetRowSpan(musicHero,1);
        Grid.SetRow(musicLibraryPane,musicWide?0:1);Grid.SetColumn(musicLibraryPane,0);Grid.SetRowSpan(musicLibraryPane,musicWide?2:1); Grid.SetRow(musicDetails,1);
        Grid.SetRow(musicTransportCard,2);Grid.SetColumnSpan(musicTransportCard,2);
        musicHero.ColumnDefinitions[0].Width=musicWide?new GridLength(1,GridUnitType.Star):GridLength.Auto;
        musicHero.ColumnDefinitions[1].Width=musicWide?new GridLength(1,GridUnitType.Star):new GridLength(1,GridUnitType.Star);
        musicHero.RowDefinitions[0].Height=musicWide?new GridLength(1,GridUnitType.Star):GridLength.Auto;
        musicHero.RowDefinitions[1].Height=musicWide?GridLength.Auto:new GridLength(0); musicHero.RowDefinitions[2].Height=musicWide?GridLength.Auto:new GridLength(0);
        musicHero.ColumnDefinitions[0].Width=musicWide?new GridLength(1,GridUnitType.Star):GridLength.Auto;
        musicHero.ColumnDefinitions[1].Width=new GridLength(1,GridUnitType.Star);
        Grid.SetColumnSpan(musicArtworkStage,musicWide?2:1); Grid.SetRow(musicLabels,musicWide?1:0); Grid.SetColumn(musicLabels,musicWide?0:1); Grid.SetColumnSpan(musicLabels,musicWide?2:1);
        foreach(var decoration in musicHero.Children.OfType<Grid>().Where(child=>child.Children.OfType<TextBlock>().Any(text=>text.Text=="LOCAL / SERVER"))) decoration.Visibility=musicWide?Visibility.Visible:Visibility.Collapsed;
        musicArtworkStage.MinHeight=musicWide?230:104;
        double cover=musicWide?Math.Clamp(Math.Min(size.Width*.21,size.Height*.30),154,224):Math.Clamp(size.Width*.20,82,130);
        musicArtFrame.Width=musicArtFrame.Height=cover+8; musicCover.Width=musicCover.Height=cover;
        musicArtworkAura.Width=musicArtworkAura.Height=cover+90; musicOuterOrbit.Width=musicOuterOrbit.Height=cover+50; musicInnerOrbit.Width=musicInnerOrbit.Height=cover+24;
        musicLabels.Spacing=musicWide?12:5;musicTitle.FontSize=musicWide?27:19;musicTitle.MaxLines=musicWide?3:2;musicArtist.FontSize=musicWide?15:12;
        musicTransport.RowDefinitions.Clear(); musicTransport.RowDefinitions.Add(new RowDefinition{Height=GridLength.Auto}); musicTransport.RowDefinitions.Add(new RowDefinition{Height=musicWide?new GridLength(0):GridLength.Auto});
        Grid.SetColumn(musicTransportTimeline,0); Grid.SetColumnSpan(musicTransportTimeline,musicWide?1:2);
        Grid.SetRow(musicTransportControls,musicWide?0:1); Grid.SetColumn(musicTransportControls,musicWide?1:0); Grid.SetColumnSpan(musicTransportControls,musicWide?1:2);
    }
    private static void SetMusicAccentLabels(DependencyObject root, Brush accent)
    {
        if (root is TextBlock label && label.FontSize <= 11 && label.CharacterSpacing >= 80) label.Foreground = accent;
        for (int i=0; i<VisualTreeHelper.GetChildrenCount(root); i++) SetMusicAccentLabels(VisualTreeHelper.GetChild(root,i),accent);
    }
    private void UpdateMusicBackdrop()
    {
        bool highContrast=appearanceAccessibility.HighContrast;musicWorkspace.RequestedTheme=highContrast?ElementTheme.Default:ElementTheme.Dark;if(musicHeaderGrid is not null)musicHeaderGrid.RequestedTheme=highContrast?ElementTheme.Default:ElementTheme.Dark;
        if(highContrast){var text=appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.WindowText);if(musicBrandTitle is not null)musicBrandTitle.Foreground=new SolidColorBrush(text);if(musicBrandSubtitle is not null)musicBrandSubtitle.Foreground=new SolidColorBrush(text);musicTitle.Foreground=musicArtist.Foreground=musicStatus.Foreground=musicError.Foreground=new SolidColorBrush(text);musicKicker.Foreground=new SolidColorBrush(appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.Highlight));var bg=appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.Window);var line=appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.Highlight);musicDetails.Background=new SolidColorBrush(bg);musicDetails.BorderBrush=new SolidColorBrush(line);musicTransportCard.Background=new SolidColorBrush(bg);musicTransportCard.BorderBrush=new SolidColorBrush(line);musicArtFrame.BorderBrush=new SolidColorBrush(line);SetMusicAccentLabels(musicWorkspace,new SolidColorBrush(appearanceUi.UIElementColor(Windows.UI.ViewManagement.UIElementType.Highlight)));musicBackdrop.GradientStops.Clear();musicBackdrop.GradientStops.Add(new GradientStop{Offset=0,Color=bg});musicBackdrop.GradientStops.Add(new GradientStop{Offset=1,Color=bg});return;}
        if(musicBrandTitle is not null)musicBrandTitle.Foreground=new SolidColorBrush(ToColor(0xFFB9FF65));if(musicBrandSubtitle is not null)musicBrandSubtitle.Foreground=new SolidColorBrush(ToColor(0xFFAAAAB7));
        musicTitle.Foreground=new SolidColorBrush(ToColor(0xFFF2F1F7));musicArtist.Foreground=new SolidColorBrush(ToColor(0xFFD1D0DB));musicKicker.Foreground=new SolidColorBrush(ToColor(0xFFB9FF65));musicStatus.Foreground=new SolidColorBrush(ToColor(0xFFC4C3CF));musicError.Foreground=new SolidColorBrush(ToColor(0xFFFFA69E));
        uint fallback=ResolveAppearanceAccent();var palette=coverPalette??new CoverPalette(fallback,fallback);uint surface=0x101117;
        musicDetails.Background=new SolidColorBrush(ToColor(0xD9191A22));musicDetails.BorderBrush=new SolidColorBrush(ToColor(0x713B3E49));musicTransportCard.Background=new SolidColorBrush(ToColor(0xE6191A22));musicTransportCard.BorderBrush=new SolidColorBrush(ToColor(0x713B3E49));musicArtFrame.BorderBrush=new SolidColorBrush(ToColor(CoverPalette.Blend(palette.Primary,0xB9FF65,.42)));SetMusicAccentLabels(musicWorkspace,new SolidColorBrush(ToColor(0xFFB9FF65)));
        musicBackdrop.GradientStops.Clear();musicBackdrop.GradientStops.Add(new GradientStop{Offset=0,Color=ToColor(CoverPalette.Blend(palette.Primary,surface,.10))});musicBackdrop.GradientStops.Add(new GradientStop{Offset=1,Color=ToColor(CoverPalette.Blend(palette.Secondary,surface,.13))});
    }
}
