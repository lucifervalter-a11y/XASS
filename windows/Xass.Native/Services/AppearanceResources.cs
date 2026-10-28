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
        Alias("XassPageBackgroundBrush", "ApplicationPageBackgroundThemeBrush", "SolidBackgroundFillColorBaseBrush");
        Alias("XassCardBackgroundBrush", "CardBackgroundFillColorDefaultBrush", "LayerFillColorDefaultBrush");
        Alias("XassHoverBrush", "SubtleFillColorSecondaryBrush", "ControlFillColorSecondaryBrush");
        Alias("XassBorderBrush", "CardStrokeColorDefaultBrush", "ControlStrokeColorDefaultBrush", "DividerStrokeColorDefaultBrush");
        Alias("XassSidebarBrush", "NavigationViewDefaultPaneBackground");
        Alias("XassFieldBrush", "ControlFillColorDefaultBrush", "TextControlBackground");
        Alias("XassWellBrush", "ControlFillColorTertiaryBrush");
        Alias("XassTextBrush", "TextFillColorPrimaryBrush");
        Alias("XassMutedTextBrush", "TextFillColorSecondaryBrush", "TextFillColorTertiaryBrush");
        Alias("XassNavForegroundBrush", "NavigationViewItemForeground", "NavigationViewItemForegroundPointerOver",
            "NavigationViewItemForegroundPressed", "NavigationViewItemForegroundSelected");
        Alias("XassNavActiveBrush", "NavigationViewItemBackgroundPointerOver", "NavigationViewItemBackgroundPressed");
        Alias("XassNavSelectedBrush", "NavigationViewItemBackgroundSelected", "NavigationViewItemBackgroundSelectedPointerOver",
            "NavigationViewItemBackgroundSelectedPressed");
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
