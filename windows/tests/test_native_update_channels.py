"""UI wiring contracts complement portable executable channel/race tests."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "Xass.Native"


class NativeUpdateChannelSourceTests(unittest.TestCase):
    def test_visible_channels_and_installed_identity_are_separate(self):
        source = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        for text in ('Content = "Релиз"', 'Content = "Бета"', 'Установлено: XASS', 'Выбран канал:',
                     'NativeUpdatePreferences.LoadChannel(NativeReleaseChannel.Installed())'):
            self.assertIn(text, source)

    def test_manual_and_automatic_use_leased_selection_not_installed_default(self):
        source = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        for start, end in (('private async Task CheckNativeUpdateAsync()', 'private async Task InstallNativeUpdateAsync()'),
                           ('private async Task CheckAutomaticNativeUpdateAsync()', 'public void AcceptDesktopActivation(')):
            section = source[source.index(start):source.index(end)]
            self.assertIn('nativeUpdateSelection.TryBegin()', section)
            self.assertIn('new NativeUpdateClient(operation.Channel)', section)
            self.assertIn('CheckForInstallationAsync', section)
            self.assertIn('operation.Dispose(); RefreshNativeUpdateControls();', section)
        self.assertNotIn('readonly NativeUpdateClient nativeUpdates = new()', source)

    def test_channel_choice_requires_confirmation_before_persistence(self):
        source = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        section = source[source.index('private async Task ChangeNativeUpdateChannelAsync()'):source.index('private async Task LoadDesktopUpdatesAsync()')]
        self.assertLess(section.index('DesktopConfirmAsync'), section.index('TrySelect(selected, NativeUpdatePreferences.SaveChannel)'))
        self.assertIn('nativeUpdateChannel.SelectedIndex = nativeUpdateSelection.Selected.Prerelease ? 1 : 0;', section)
        self.assertIn('if (changed)', section)
        self.assertIn('await CheckNativeUpdateAsync()', section)

    def test_schema_two_carries_captured_package_channel(self):
        source = (ROOT / "Services/NativeUpdateCoordinator.cs").read_text(encoding="utf-8")
        self.assertIn('schema = 2', source)
        self.assertIn('release_distribution = update.Distribution, allow_channel_change = changeChannel', source)
        self.assertLess(source.index('ValidateInstallation(update, document.RootElement)'), source.index('Directory.CreateDirectory(job)'))
        self.assertNotIn('NativeUpdatePreferences.LoadChannel', source)

    def test_history_loading_cannot_replace_channel_check_result(self):
        source = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        section = source[source.index('private async Task LoadDesktopUpdatesAsync()'):source.index('private string InstalledNativeRevision()')]
        self.assertIn('nativeAgentUpdateStatus.Text', section)
        self.assertNotIn('updateStatus.Text', section)


if __name__ == "__main__":
    unittest.main()
