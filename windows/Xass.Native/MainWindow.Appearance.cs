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

    private async void InitializeAppearance()
    {
        AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = false;
        AppearanceAccentMode.SelectionChanged += (_, _) =>
            AppearanceColor.IsEnabled = AppearanceAccentMode.SelectedIndex == 1;
        var loaded = await Task.Run(() =>
        {
            var preferences = appearanceStore.Load(out bool recovered);
            return (preferences, recovered);
        });
        if (closed) return;
        appearance = loaded.preferences;
        PopulateAppearanceControls();
        ApplyAppearance();
        appearanceUi.ColorValuesChanged += AppearanceSystemColorsChanged;
        appearanceAccessibility.HighContrastChanged += AppearanceContrastChanged;
        RootGrid.ActualThemeChanged += AppearanceActualThemeChanged;
        appearanceReady = true;
        AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = true;
        if (loaded.recovered)
            AppearanceStatus.Text = "Не удалось прочитать часть настроек. Применено стандартное оформление; файл изменится только после сохранения.";
    }

    private void PopulateAppearanceControls()
    {
        AppearanceTheme.SelectedIndex = appearance.Theme switch { "light" => 1, "dark" => 2, _ => 0 };
        AppearanceAccentMode.SelectedIndex = appearance.Accent == "system" ? 0 : 1;
        AppearanceColor.IsEnabled = AppearanceAccentMode.SelectedIndex == 1;
        AppearanceColor.Color = AccentPalette.TryParse(appearance.Accent, out uint custom)
            ? ToColor(custom) : appearanceUi.GetColorValue(UIColorType.Accent);
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
        // Disabled controls, ordinary text, focus rings and native selection colors keep
        // their WinUI resources. Do not overwrite system error/success/contrast semantics.
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
        string theme = AppearanceTheme.SelectedIndex switch { 1 => "light", 2 => "dark", _ => "system" };
        string accent = AppearanceAccentMode.SelectedIndex == 0 ? "system" : $"#{ToRgb(AppearanceColor.Color):X6}";
        await SaveAppearanceAsync(new AppearancePreferences(theme, accent));
    }

    private async void AppearanceResetClick(object sender, RoutedEventArgs args)
    {
        if (!appearanceReady || !AppearanceReset.IsEnabled) return;
        await SaveAppearanceAsync(AppearancePreferences.Defaults);
        if (!closed) PopulateAppearanceControls();
    }

    private async Task SaveAppearanceAsync(AppearancePreferences selected)
    {
        AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = false;
        appearance = selected.Normalize();
        ApplyAppearance();
        try
        {
            await Task.Run(() => appearanceStore.Save(selected));
            if (!closed) AppearanceStatus.Text = "Оформление сохранено. Оно останется после перезапуска и обновления.";
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            if (!closed) AppearanceStatus.Text = "Оформление применено к этому окну, но сохранить файл не удалось. Проверьте доступ к папке данных и попробуйте снова.";
        }
        finally
        {
            if (!closed) AppearanceApply.IsEnabled = AppearanceReset.IsEnabled = true;
        }
    }

    private void DisposeAppearance()
    {
        if (!appearanceReady) return;
        appearanceUi.ColorValuesChanged -= AppearanceSystemColorsChanged;
        appearanceAccessibility.HighContrastChanged -= AppearanceContrastChanged;
        RootGrid.ActualThemeChanged -= AppearanceActualThemeChanged;
    }
}
