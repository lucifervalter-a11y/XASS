"""Source contracts for side-by-side acceptance mode; never launch the UI."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "Xass.Native"


class NativeTestModeContracts(unittest.TestCase):
    def test_runtime_paths_and_instance_are_isolated(self):
        mode = (ROOT / "Services" / "NativeRuntimeMode.cs").read_text(encoding="utf-8")
        self.assertIn('TestRootVariable = "XASS_NATIVE_TEST_ROOT"', mode)
        self.assertIn('Path.Combine(testRoot!, "data")', mode)
        self.assertIn('Path.Combine(testRoot!, "preferences")', mode)
        self.assertIn("IsSameOrChild(root, installedData)", mode)
        self.assertIn("IsSameOrChild(root, installedPreferences)", mode)
        self.assertIn('"XASS.Native.Test." + RootFingerprint(testRoot!)', mode)

        window = (ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")
        desktop = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        app = (ROOT / "App.xaml.cs").read_text(encoding="utf-8")
        instance = (ROOT / "Services" / "NativeInstance.cs").read_text(encoding="utf-8")
        self.assertIn("DataPath.Text = NativeRuntimeMode.DataRoot", window)
        self.assertIn("NativeRuntimeMode.RuntimePreferencesFile", desktop)
        self.assertIn("new NativeInstance(NativeRuntimeMode.InstanceName)", app)
        self.assertIn("NamedPipeServerStream(name", instance)

    def test_test_mode_cannot_autostart_or_update(self):
        desktop = (ROOT / "MainWindow.Desktop.cs").read_text(encoding="utf-8")
        self.assertIn("if (NativeRuntimeMode.AllowUpdates) automaticUpdateTimer.Start()", desktop)
        self.assertIn("if (!NativeRuntimeMode.AllowBackgroundServices)", desktop)
        self.assertIn("Background services are disabled in isolated test mode.", desktop)
        self.assertIn("Updates are disabled in isolated test mode.", desktop)

        preferences = (ROOT / "Services" / "NativeUpdatePreferences.cs").read_text(encoding="utf-8")
        coordinator = (ROOT / "Services" / "NativeUpdateCoordinator.cs").read_text(encoding="utf-8")
        client = (ROOT / "Services" / "NativeUpdateClient.cs").read_text(encoding="utf-8")
        self.assertIn("NativeRuntimeMode.UpdatePreferencesFile", preferences)
        self.assertIn("NativeRuntimeMode.UpdatesRoot", coordinator)
        self.assertIn("NativeRuntimeMode.UpdatesRoot", client)


if __name__ == "__main__":
    unittest.main()
