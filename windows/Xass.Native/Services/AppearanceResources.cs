using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Xass.Native.Services;

// Install shared brushes before control templates are created. Replacing a
// dictionary entry later leaves already-open controls holding the old brush.
public static class AppearanceResources
{
    public static void Register(ResourceDictionary resources)
    {
        void Alias(string source, params string[] names)
        {
            var brush = (SolidColorBrush)resources[source];
            foreach (string name in names) resources[name] = brush;
        }
        Alias("XassAccentFillBrush", "AccentFillColorDefaultBrush", "AccentFillColorSelectedTextBackgroundBrush",
            "SliderTrackValueFill", "ToggleSwitchFillOn", "CheckBoxCheckBackgroundFillChecked",
            "ProgressBarForeground", "TextControlSelectionHighlightColor", "ToggleButtonBackgroundChecked");
        Alias("XassAccentHoverBrush", "AccentFillColorSecondaryBrush", "SliderTrackValueFillPointerOver",
            "ToggleSwitchFillOnPointerOver", "CheckBoxCheckBackgroundFillCheckedPointerOver", "ToggleButtonBackgroundCheckedPointerOver");
        Alias("XassAccentPressedBrush", "AccentFillColorTertiaryBrush", "SliderTrackValueFillPressed",
            "ToggleSwitchFillOnPressed", "CheckBoxCheckBackgroundFillCheckedPressed", "ToggleButtonBackgroundCheckedPressed");
        Alias("XassAccentTextBrush", "AccentTextFillColorPrimaryBrush", "AccentTextFillColorSecondaryBrush",
            "AccentTextFillColorTertiaryBrush", "NavigationViewSelectionIndicatorForeground",
            "SliderThumbBackground", "SliderThumbBackgroundPointerOver", "SliderThumbBackgroundPressed",
            "PivotHeaderItemSelectedPipeFill", "PivotHeaderItemFocusPipeFill", "TextControlBorderBrushFocused",
            "ListViewItemSelectionIndicatorBrush", "ListViewItemSelectionIndicatorPointerOverBrush", "ListViewItemSelectionIndicatorPressedBrush",
            "HyperlinkButtonForeground", "HyperlinkButtonForegroundPointerOver", "HyperlinkButtonForegroundPressed");
        Alias("XassAccentForegroundBrush", "TextOnAccentFillColorPrimaryBrush", "TextOnAccentFillColorDefaultBrush",
            "TextOnAccentFillColorSecondaryBrush", "TextOnAccentFillColorSelectedTextBrush",
            "ToggleButtonForegroundChecked", "ToggleButtonForegroundCheckedPointerOver", "ToggleButtonForegroundCheckedPressed");
        // Disabled and error/success resources retain their distinct semantics.
    }
}
