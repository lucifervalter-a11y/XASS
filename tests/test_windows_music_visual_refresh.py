from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = (ROOT / "windows" / "Xass.Native" / "MainWindow.MusicLayout.cs").read_text(encoding="utf-8")
MUSIC = (ROOT / "windows" / "Xass.Native" / "MainWindow.DesktopMusic.cs").read_text(encoding="utf-8")


class WindowsMusicVisualRefreshTests(unittest.TestCase):
    def test_original_signal_deck_replaces_previous_editorial_composition(self):
        for marker in ("XASS  /  AUDIO SYSTEM", "СИГНАЛ · СЕЙЧАС", "01  /  ИСТОЧНИКИ",
                       "03  /  ВЫХОД", "0xFFB9FF65", "RadialGradientBrush", "StrokeDashArray",
                       "Музыкальная карта", "musicBackdrop", "musicArtFrame"):
            with self.subTest(marker=marker):
                self.assertIn(marker, LAYOUT)
        self.assertNotIn("musicTitle.FontSize = musicWide ? 38 : 25", LAYOUT)
        self.assertIn("musicBackdrop", LAYOUT.split("var atmosphere", 1)[1])

    def test_layout_has_distinct_wide_and_stacked_arrangements(self):
        self.assertIn("size.Width>=920&&size.Height>=500", LAYOUT)
        self.assertIn("Grid.SetRow(musicLibraryPane,musicWide?0:1)", LAYOUT)
        self.assertIn("Grid.SetRow(musicDetails,1)", LAYOUT)
        self.assertIn("Grid.SetRow(musicHero,0)", LAYOUT)
        self.assertIn("Grid.SetRow(musicLabels,musicWide?1:0)", LAYOUT)
        self.assertIn("musicWorkspace.RowDefinitions[1].Height=musicWide?new GridLength(0):new GridLength(1,GridUnitType.Star)", LAYOUT)
        self.assertIn("musicArtFrame.Width=musicArtFrame.Height", LAYOUT)
        self.assertIn("musicArtworkAura.Width=musicArtworkAura.Height=cover+90", LAYOUT)
        self.assertIn("musicTitle.MaxLines=musicWide?3:2", LAYOUT)
        self.assertIn("Grid.SetRow(musicTransportControls,musicWide?0:1)", LAYOUT)

    def test_transport_is_in_dock_and_volume_remains_available_on_compact_layout(self):
        builder = LAYOUT.split("private void BuildMusicTransport()", 1)[1].split("private void BuildMusicSections()", 1)[0]
        self.assertIn("musicTransportCard.Child=musicTransport", builder)
        self.assertIn('"MusicSeek"', builder)
        self.assertIn('"MusicVolumeMenu"', builder)
        self.assertIn("volumeButton.Flyout", builder)
        for action in ("previous", "next", "pause", "resume", "reset"):
            self.assertIn(f'action="{action}"', builder)

    def test_all_existing_music_features_and_accessibility_hooks_remain(self):
        for marker in ("MusicPrevious", "MusicNext", "MusicPause", "MusicResume", "MusicPlay", "MusicStop", "MusicSeek", "MusicVolume",
                       '"MusicServerLibrary"', '"MusicHistory"', '"MusicLyrics"', '"MusicQueueSource"'):
            with self.subTest(marker=marker):
                self.assertIn(marker, LAYOUT)
        for action in ("local_play", "catalog", "seek", "volume", "cover_sha256", "lyrics"):
            self.assertIn(action, MUSIC)
        self.assertIn("private async Task CommitMusicSeekAsync()", MUSIC)
        self.assertIn("private async Task CommitMusicVolumeAsync()", MUSIC)

    def test_high_contrast_keeps_system_colors(self):
        high_contrast = LAYOUT.split("private void UpdateMusicBackdrop()", 1)[1]
        high_contrast = high_contrast.split("uint fallback", 1)[0] if "uint fallback" in high_contrast else high_contrast
        self.assertIn("appearanceAccessibility.HighContrast", high_contrast)
        self.assertIn("UIElementType.WindowText", high_contrast)
        self.assertIn("UIElementType.Highlight", high_contrast)


if __name__ == "__main__":
    unittest.main()
