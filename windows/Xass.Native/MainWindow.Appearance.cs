using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
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
    private AppearancePreferences savedAppearance = AppearancePreferences.Defaults;

    private void InitializeAppearance()
    {
        // A bounded, local 4 KiB preference file is loaded on the UI thread.
        // Initialization must always leave editing and saving available.
        bool recovered = false;
        try
        {
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

    private AppearancePreferences SelectedAppearance() => new(
        AppearanceTheme.SelectedIndex switch { 1 => "light", 2 => "dark", _ => "system" },
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
        AppearanceTheme.SelectedIndex = appearance.Theme switch { "light" => 1, "dark" => 2, _ => 0 };
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
            "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default
        };
        UpdateAppearanceColors();
    }

    private void UpdateAppearanceColors()
    {
        if (closed) return;
        bool highContrast = appearanceAccessibility.HighContrast;
        uint accent = AccentPalette.TryParse(appearance.Accent, out uint custom)
            ? custom : ToRgb(appearanceUi.GetColorValue(UIColorType.Accent));
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
        SetAppearanceBrush("XassAccentHoverBrush", highContrast ? fill : ToColor(AccentPalette.Shift(accent, -0.08)));
        SetAppearanceBrush("XassAccentPressedBrush", highContrast ? fill : ToColor(AccentPalette.Shift(accent, -0.16)));
        uint surface = RootGrid.ActualTheme == ElementTheme.Dark ? 0x202020u : 0xFFFFFFu;
        SetAppearanceBrush("XassAccentTextBrush", highContrast ? fill : ToColor(AccentPalette.ReadableAccent(accent, surface)));
        UpdateMusicBackdrop();
        if (Microsoft.UI.Windowing.AppWindowTitleBar.IsCustomizationSupported())
        {
            var bar = AppWindow.TitleBar;
            Color background = ToColor(surface), text = ToColor(AccentPalette.TextOn(surface));
            bar.BackgroundColor = bar.InactiveBackgroundColor = background;
            bar.ForegroundColor = bar.InactiveForegroundColor = text;
            bar.ButtonBackgroundColor = bar.ButtonInactiveBackgroundColor = background;
            bar.ButtonForegroundColor = bar.ButtonInactiveForegroundColor = text;
        }
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
