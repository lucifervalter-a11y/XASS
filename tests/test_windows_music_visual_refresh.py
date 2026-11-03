from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = (ROOT / "windows" / "Xass.Native" / "MainWindow.MusicLayout.cs").read_text(encoding="utf-8")
MUSIC = (ROOT / "windows" / "Xass.Native" / "MainWindow.DesktopMusic.cs").read_text(encoding="utf-8")


class WindowsMusicVisualRefreshTests(unittest.TestCase):
    def test_editorial_layout_has_artwork_hierarchy_and_player_surface(self):
        for marker in ("XASS  /  MUSIC", "NOW PLAYING", "musicCover", "musicTitle.FontSize = musicWide ? 38 : 25",
                       "musicTransportCard", "musicDetails", "CoverPalette.Blend"):
            with self.subTest(marker=marker):
                self.assertIn(marker, LAYOUT)

    def test_transport_child_is_attached_after_instance_initialization(self):
        # C# CS0236 forbids a field initializer from using another instance field.
        field = LAYOUT.split("private readonly Border musicTransportCard =", 1)[1].split(";", 1)[0]
        self.assertNotIn("Child = musicTransport", field)
        builder = LAYOUT.split("private void BuildMusicTransport()", 1)[1].split("private void BuildMusicSections()", 1)[0]
        self.assertIn("musicTransportCard.Child = musicTransport;", builder)
        self.assertLess(builder.index("musicTransportCard.Child = musicTransport;"), builder.index("musicTransport.RowDefinitions"))

    def test_responsive_and_high_contrast_paths_remain(self):
        self.assertIn("musicWide = size.Width >= 760", LAYOUT)
        self.assertIn("appearanceAccessibility.HighContrast", LAYOUT)
        self.assertIn("UIElementType.WindowText", LAYOUT)
        self.assertIn("UIElementType.Highlight", LAYOUT)

    def test_player_actions_and_automation_identifiers_remain(self):
        for marker in ("MusicPrevious", "MusicNext", "MusicPause", "MusicResume", "MusicPlay", "MusicStop", "MusicSeek", "MusicVolume"):
            with self.subTest(marker=marker):
                self.assertIn(marker, LAYOUT)
        self.assertIn('"MusicServerLibrary"', LAYOUT)
        self.assertIn('"MusicHistory"', LAYOUT)
        self.assertIn('"MusicLyrics"', LAYOUT)
        self.assertIn("private async Task CommitMusicSeekAsync()", MUSIC)
        self.assertIn("private async Task CommitMusicVolumeAsync()", MUSIC)


if __name__ == "__main__":
    unittest.main()
