using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using System.Runtime.InteropServices;
using Windows.UI;
using Windows.UI.ViewManagement;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly AppearanceStore appearanceStore = new();
    private readonly UISettings appearanceUi = new();
    private readonly AccessibilitySettings appearanceAccessibility = new();
    private AppearancePreferences appearance = AppearancePreferences.Defaults;
    private bool appearanceReady;

    private bool populatingAppearance;
    private bool appearanceSaving;
    private bool systemColorsSubscribed, contrastSubscribed;
    private string appliedBackdrop = "";
    private AppearancePreferences savedAppearance = AppearancePreferences.Defaults;

    private void InitializeAppearance()
    {
        // A bounded, local 4 KiB preference file is loaded on the UI thread.
        // Initialization must always leave editing and saving available.
        bool recovered = false;
        try
        {
            InitializeAppearanceChoices();
            appearance = savedAppearance = appearanceStore.Load(out recovered);
            PopulateAppearanceControls();
            ApplyAppearance();
        }
        catch (Exception error)
        {
            AppearanceStatus.Text = $"Не удалось полностью применить оформление ({error.GetType().Name}). Выберите цвет и повторите сохранение.";
        }
        finally
        {
            appearanceReady = true;
            AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = true;
        }
        AppearanceTheme.SelectionChanged += (_, _) => PreviewAppearance();
        AppearanceAccentMode.SelectionChanged += (_, _) => PreviewAppearance();
        AppearanceColor.ColorChanged += (_, _) => PreviewAppearance();
        // Some desktop Windows sessions don't expose these WinRT events.
        // Their absence must not disable the user's preview/save controls.
        try { appearanceUi.ColorValuesChanged += AppearanceSystemColorsChanged; systemColorsSubscribed = true; }
        catch (System.Runtime.InteropServices.COMException) { }
        try { appearanceAccessibility.HighContrastChanged += AppearanceContrastChanged; contrastSubscribed = true; }
        catch (System.Runtime.InteropServices.COMException) { }
        RootGrid.ActualThemeChanged += AppearanceActualThemeChanged;
        Activated += (_, _) => QueueAppearanceColors();
        if (recovered)
            AppearanceStatus.Text = "Настройки оформления не удалось прочитать. Используются значения Windows; исходный файл будет заменён только после сохранения.";
    }

    private void InitializeAppearanceChoices()
    {
        AppearanceTheme.Items.Clear();
        foreach (var choice in new[]
        {
            (id: "system", label: "\u041a\u0430\u043a \u0432 Windows", accessible: "System theme"),
            (id: "light", label: "\u0421\u0432\u0435\u0442\u043b\u0430\u044f", accessible: "Light theme"),
            (id: "dark", label: "\u0422\u0451\u043c\u043d\u0430\u044f", accessible: "Dark theme"),
            (id: AppearanceThemeCatalog.Classic, label: "Classic", accessible: "Classic theme"),
            (id: AppearanceThemeCatalog.Cyber, label: "Cyber", accessible: "Cyber theme"),
            (id: AppearanceThemeCatalog.Tokyo, label: "Tokyo", accessible: "Tokyo theme"),
            (id: AppearanceThemeCatalog.Nordic, label: "Nordic", accessible: "Nordic theme"),
            (id: AppearanceThemeCatalog.Matrix, label: "Matrix", accessible: "Matrix theme"),
        })
        {
            var item = new ComboBoxItem { Content = choice.label, Tag = choice.id };
            AutomationProperties.SetName(item, choice.accessible);
            AppearanceTheme.Items.Add(item);
        }
        AutomationProperties.SetName(AppearanceTheme, "XASS interface theme");
        AutomationProperties.SetHelpText(AppearanceTheme,
            "System, light, dark, or one of five XASS themes");
        AutomationProperties.SetName(AppearanceAccentMode, "XASS accent color");
        AutomationProperties.SetName(AppearanceColor, "Custom accent color");
    }

    private static string SelectedTag(ComboBox selector, string fallback) =>
        (selector.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? fallback;

    private AppearancePreferences SelectedAppearance() => new(
        SelectedTag(AppearanceTheme, "system"),
        AppearanceAccentMode.SelectedIndex == 0 ? "system" : $"#{ToRgb(AppearanceColor.Color):X6}");

    private void PreviewAppearance()
    {
        if (!appearanceReady || populatingAppearance || appearanceSaving || closed) return;
        AppearanceColor.IsEnabled = AppearanceAccentMode.SelectedIndex == 1;
        appearance = SelectedAppearance().Normalize();
        try
        {
            ApplyAppearance();
            AppearanceStatus.Text = appearance == savedAppearance ? "Оформление сохранено." : "Предпросмотр применяется ко всем экранам. Сохраните изменения, чтобы оставить их после перезапуска.";
        }
        catch (Exception error)
        {
            AppearanceStatus.Text = $"Предпросмотр не завершён ({error.GetType().Name}). Повторите сохранение.";
        }
        AppearanceApply.IsEnabled = true;
    }

    private void PopulateAppearanceControls()
    {
        populatingAppearance = true;
        string canonical = AppearanceThemeCatalog.NormalizeThemeName(appearance.Theme);
        AppearanceTheme.SelectedItem = AppearanceTheme.Items.OfType<ComboBoxItem>()
            .FirstOrDefault(item => string.Equals(item.Tag?.ToString(), canonical, StringComparison.Ordinal))
            ?? AppearanceTheme.Items[0];
        AppearanceAccentMode.SelectedIndex = appearance.Accent == "system" ? 0 : 1;
        AppearanceColor.IsEnabled = AppearanceAccentMode.SelectedIndex == 1;
        AppearanceColor.Color = AccentPalette.TryParse(appearance.Accent, out uint custom)
            ? ToColor(custom) : appearanceUi.GetColorValue(UIColorType.Accent);
        populatingAppearance = false;
    }

    private static Color ToColor(uint rgb) => Color.FromArgb(255,
        (byte)(rgb >> 16), (byte)(rgb >> 8), (byte)rgb);
    private static uint ToRgb(Color color) => (uint)((color.R << 16) | (color.G << 8) | color.B);
    private static void SetAppearanceBrush(string key, Color color) =>
        ((SolidColorBrush)Application.Current.Resources[key]).Color = color;

    private void ApplyAppearance()
    {
        RootGrid.RequestedTheme = appearance.Theme switch
        {
            "light" => ElementTheme.Light,
            "dark" => ElementTheme.Dark,
            _ when AppearanceThemeCatalog.IsPreset(appearance.Theme) => ElementTheme.Dark,
            _ => ElementTheme.Default
        };
        ApplyAppearanceBackdrop(AppearanceThemeCatalog.TryGetPalette(appearance.Theme, out var palette)
            ? palette.Backdrop : "system");
        UpdateAppearanceColors();
    }

    private void ApplyAppearanceBackdrop(string kind)
    {
        if (appliedBackdrop == kind) return;
        appliedBackdrop = kind;
        try
        {
            if (kind == "acrylic" && Microsoft.UI.Composition.SystemBackdrops.DesktopAcrylicController.IsSupported())
                SystemBackdrop = new DesktopAcrylicBackdrop();
            else if (kind == "mica_alt" && Microsoft.UI.Composition.SystemBackdrops.MicaController.IsSupported())
                SystemBackdrop = new MicaBackdrop { Kind = MicaKind.BaseAlt };
            else if (Microsoft.UI.Composition.SystemBackdrops.MicaController.IsSupported())
                SystemBackdrop = new MicaBackdrop();
            else
                SystemBackdrop = null;
        }
        catch (Exception error) when (error is InvalidOperationException or System.Runtime.InteropServices.COMException)
        {
            // A theme must remain usable if this Windows build rejects its backdrop.
            SystemBackdrop = null;
        }
    }

    private static uint ThemeRgb(string value) => AccentPalette.TryParse(value, out uint rgb)
        ? rgb : throw new InvalidDataException("Invalid color in the built-in appearance catalog.");

    private uint ResolveAppearanceAccent(AppearanceThemePalette? palette = null)
    {
        if (AccentPalette.TryParse(appearance.Accent, out uint custom)) return custom;
        if (palette is null && AppearanceThemeCatalog.TryGetPalette(appearance.Theme, out var selected))
            palette = selected;
        return palette is null ? ToRgb(appearanceUi.GetColorValue(UIColorType.Accent)) : ThemeRgb(palette.Accent);
    }

    private static uint ColorRefToRgb(uint colorRef) =>
        ((colorRef & 0x0000FF) << 16) | (colorRef & 0x00FF00) | ((colorRef & 0xFF0000) >> 16);

    private const int DwmwaBorderColor = 34;
    private const uint DwmColorDefault = 0xFFFFFFFF;
    [DllImport("dwmapi.dll", EntryPoint = "DwmSetWindowAttribute")]
    private static extern int SetDwmWindowAttribute(nint window, int attribute, ref uint value, int valueSize);

    private void ApplyDwmBorder(uint colorRef)
    {
        try
        {
            nint window = WinRT.Interop.WindowNative.GetWindowHandle(this);
            _ = SetDwmWindowAttribute(window, DwmwaBorderColor, ref colorRef, sizeof(uint));
        }
        catch (Exception error) when (error is DllNotFoundException or EntryPointNotFoundException)
        {
            // Windows versions without DWMWA_BORDER_COLOR keep their default border.
        }
    }

    private void UpdateAppearanceColors()
    {
        if (closed) return;
        bool highContrast = appearanceAccessibility.HighContrast;
        AppearanceThemePalette? palette = AppearanceThemeCatalog.TryGetPalette(appearance.Theme, out var found)
            ? found : null;
        bool dark = palette is not null || RootGrid.ActualTheme == ElementTheme.Dark;

        uint page = palette is null ? (dark ? 0x202020u : 0xF3F3F3u) : ThemeRgb(palette.Background);
        uint card = palette is null ? (dark ? 0x2B2B2Bu : 0xFFFFFFu) : ThemeRgb(palette.Card);
        uint hover = palette is null ? (dark ? 0x333333u : 0xE9E9E9u) : ThemeRgb(palette.Hover);
        uint border = palette is null ? (dark ? 0x454545u : 0xDADADAu) : ThemeRgb(palette.Line);
        uint sidebar = palette is null ? (dark ? 0x181818u : 0xF9F9F9u) : ThemeRgb(palette.Sidebar);
        uint field = palette is null ? (dark ? 0x292929u : 0xFFFFFFu) : ThemeRgb(palette.Field);
        uint well = palette is null ? (dark ? 0x333333u : 0xF0F0F0u) : ThemeRgb(palette.Well);
        uint text = palette is null ? (dark ? 0xFFFFFFu : 0x1A1A1Au) : ThemeRgb(palette.Text);
        uint muted = palette is null ? (dark ? 0xC8C8C8u : 0x616161u) : ThemeRgb(palette.Muted);
        uint note = palette is null ? card : ThemeRgb(palette.Note);
        uint sidebarCard = palette is null ? card : ThemeRgb(palette.SidebarCard);
        uint sidebarBorder = palette is null ? border : ThemeRgb(palette.SidebarBorder);
        uint navForeground = palette is null ? muted : ThemeRgb(palette.NavigationForeground);
        uint navActive = palette is null ? hover : ThemeRgb(palette.NavigationActiveBackground);
        uint navSelected = palette is null ? (dark ? 0x383838u : 0xE2E2E2u) : ThemeRgb(palette.NavigationSelectedBackground);

        if (highContrast)
        {
            page = card = sidebar = ToRgb(appearanceUi.UIElementColor(UIElementType.Window));
            field = well = note = sidebarCard = hover = navActive = navSelected =
                ToRgb(appearanceUi.UIElementColor(UIElementType.ButtonFace));
            text = navForeground = ToRgb(appearanceUi.UIElementColor(UIElementType.WindowText));
            muted = border = sidebarBorder = ToRgb(appearanceUi.UIElementColor(UIElementType.GrayText));
        }

        SetAppearanceBrush("XassPageBackgroundBrush", ToColor(page));
        SetAppearanceBrush("XassCardBackgroundBrush", ToColor(card));
        SetAppearanceBrush("XassHoverBrush", ToColor(hover));
        SetAppearanceBrush("XassBorderBrush", ToColor(border));
        SetAppearanceBrush("XassSidebarBrush", ToColor(sidebar));
        SetAppearanceBrush("XassFieldBrush", ToColor(field));
        SetAppearanceBrush("XassWellBrush", ToColor(well));
        SetAppearanceBrush("XassTextBrush", ToColor(text));
        SetAppearanceBrush("XassMutedTextBrush", ToColor(muted));
        SetAppearanceBrush("XassNavForegroundBrush", ToColor(navForeground));
        SetAppearanceBrush("XassNavActiveBrush", ToColor(navActive));
        SetAppearanceBrush("XassNavSelectedBrush", ToColor(navSelected));
        SetAppearanceBrush("XassNoteBrush", ToColor(note));
        SetAppearanceBrush("XassSidebarCardBrush", ToColor(sidebarCard));
        SetAppearanceBrush("XassSidebarBorderBrush", ToColor(sidebarBorder));

        uint accent = ResolveAppearanceAccent(palette);
        Color fill = highContrast ? appearanceUi.UIElementColor(UIElementType.Highlight) : ToColor(accent);
        Color foreground = highContrast ? appearanceUi.UIElementColor(UIElementType.HighlightText)
            : ToColor(AccentPalette.TextOn(accent));
        SetAppearanceBrush("XassAccentFillBrush", fill);
        SetAppearanceBrush("XassAccentForegroundBrush", foreground);
        foreach (var state in new[] { (suffix: "", shift: 0.0), (suffix: "PointerOver", shift: -0.08),
                                     (suffix: "Pressed", shift: -0.16) })
        {
            uint background = AccentPalette.Shift(accent, state.shift);
            Color stateFill = highContrast ? fill : ToColor(background);
            Color stateText = highContrast ? foreground : ToColor(AccentPalette.TextOn(background));
            SetAppearanceBrush("AccentButtonBackground" + state.suffix, stateFill);
            SetAppearanceBrush("AccentButtonForeground" + state.suffix, stateText);
            SetAppearanceBrush("AccentButtonBorderBrush" + state.suffix, stateText);
        }
        uint accentSoft = palette is not null && appearance.Accent == "system"
            ? ThemeRgb(palette.AccentSoft) : AccentPalette.Shift(accent, -0.08);
        SetAppearanceBrush("XassAccentSoftBrush", highContrast ? fill : ToColor(accentSoft));
        SetAppearanceBrush("XassAccentHoverBrush", highContrast ? fill : ToColor(accentSoft));
        SetAppearanceBrush("XassAccentPressedBrush", highContrast ? fill : ToColor(AccentPalette.Shift(accent, -0.16)));
        SetAppearanceBrush("XassAccentTextBrush", highContrast ? fill : ToColor(AccentPalette.ReadableAccent(accent, page)));
        uint semanticAccent = highContrast ? ToRgb(fill) : accent;
        SetAppearanceBrush("XassLilacBrush", ToColor(palette is null || highContrast ? semanticAccent : ThemeRgb(palette.Lilac)));
        SetAppearanceBrush("XassSuccessBrush", ToColor(palette is null || highContrast ? semanticAccent : ThemeRgb(palette.Green)));
        SetAppearanceBrush("XassWarningBrush", ToColor(palette is null || highContrast ? semanticAccent : ThemeRgb(palette.Amber)));
        SetAppearanceBrush("XassErrorBrush", ToColor(highContrast ? semanticAccent : palette is null ? text : ThemeRgb(palette.Red)));
        // Keep the material visible when supported. The exact theme background
        // remains the fallback and the shared surface resource for controls.
        RootGrid.Background = highContrast || SystemBackdrop is null
            ? (Brush)Application.Current.Resources["XassPageBackgroundBrush"]
            : new SolidColorBrush(Microsoft.UI.Colors.Transparent);
        UpdateMusicBackdrop();
        if (Microsoft.UI.Windowing.AppWindowTitleBar.IsCustomizationSupported())
        {
            var bar = AppWindow.TitleBar;
            Color titleBackground = ToColor(highContrast || palette is null ? page : ColorRefToRgb(palette.DwmCaption));
            Color titleText = ToColor(highContrast || palette is null ? text : ColorRefToRgb(palette.DwmText));
            bar.BackgroundColor = bar.InactiveBackgroundColor = titleBackground;
            bar.ForegroundColor = bar.InactiveForegroundColor = titleText;
            bar.ButtonBackgroundColor = bar.ButtonInactiveBackgroundColor = titleBackground;
            bar.ButtonForegroundColor = bar.ButtonInactiveForegroundColor = titleText;
        }
        ApplyDwmBorder(!highContrast && palette is not null ? palette.DwmBorder : DwmColorDefault);
    }

    private void QueueAppearanceColors() => DispatcherQueue.TryEnqueue(() =>
    {
        if (!closed) UpdateAppearanceColors();
    });
    private void AppearanceSystemColorsChanged(UISettings sender, object args) => QueueAppearanceColors();
    private void AppearanceContrastChanged(AccessibilitySettings sender, object args) => QueueAppearanceColors();
    private void AppearanceActualThemeChanged(FrameworkElement sender, object args) => UpdateAppearanceColors();

    private async void AppearanceApplyClick(object sender, RoutedEventArgs args)
    {
        if (!appearanceReady || !AppearanceApply.IsEnabled) return;
        await SaveAppearanceAsync(SelectedAppearance());
    }

    private async void AppearanceResetClick(object sender, RoutedEventArgs args)
    {
        if (!appearanceReady || !AppearanceReset.IsEnabled) return;
        await SaveAppearanceAsync(AppearancePreferences.Defaults);
        if (!closed) PopulateAppearanceControls();
    }

    private async Task SaveAppearanceAsync(AppearancePreferences selected)
    {
        appearanceSaving = true;
        AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = false;
        appearance = selected.Normalize();
        bool applied = false;
        try
        {
            ApplyAppearance();
            applied = true;
            await Task.Run(() => appearanceStore.Save(appearance));
            savedAppearance = appearance;
            if (!closed) AppearanceStatus.Text = "Оформление сохранено. Оно останется после перезапуска и обновления.";
        }
        catch (Exception error)
        {
            if (!closed) AppearanceStatus.Text = applied
                ? "Предпросмотр работает, но сохранить настройки не удалось. Повторите сохранение."
                : $"Цвет не удалось применить ({error.GetType().Name}). Изменения не сохранены.";
        }
        finally
        {
            appearanceSaving = false;
            if (!closed) AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = true;
        }
    }

    private void DisposeAppearance()
    {
        if (!appearanceReady) return;
        if (systemColorsSubscribed) appearanceUi.ColorValuesChanged -= AppearanceSystemColorsChanged;
        if (contrastSubscribed) appearanceAccessibility.HighContrastChanged -= AppearanceContrastChanged;
        RootGrid.ActualThemeChanged -= AppearanceActualThemeChanged;
    }
}
