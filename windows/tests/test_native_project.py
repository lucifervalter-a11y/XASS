"""Source contracts only; these do not replace the Windows XAML compiler."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1] / "Xass.Native"
XAML_NS = "{http://schemas.microsoft.com/winfx/2006/xaml}"


class NativeProjectContracts(unittest.TestCase):
    def test_project_xaml_and_manifest_are_well_formed(self):
        for filename in ("Xass.Native.csproj", "App.xaml", "MainWindow.xaml", "app.manifest"):
            with self.subTest(filename=filename):
                ET.parse(PROJECT / filename)

    def test_all_declared_xaml_events_have_code_handlers(self):
        root = ET.parse(PROJECT / "MainWindow.xaml").getroot()
        code = (PROJECT / "MainWindow.xaml.cs").read_text()
        names = []
        for element in root.iter():
            name = element.get(XAML_NS + "Name")
            if name:
                names.append(name)
            for event in ("Click", "KeyDown", "SelectionChanged"):
                handler = element.get(event)
                if handler:
                    self.assertRegex(code, rf"\b{re.escape(handler)}\s*\(")
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"Navigation", "TrackList", "ConnectionPage", "DevicePage", "MusicPage"} <= set(names))

    def test_project_is_separate_unpacked_self_contained_shell(self):
        project = ET.parse(PROJECT / "Xass.Native.csproj").getroot()
        for key, value in {"UseWinUI": "true", "WindowsPackageType": "None",
                           "SelfContained": "true", "WindowsAppSDKSelfContained": "true"}.items():
            self.assertEqual(project.findtext("PropertyGroup/" + key), value)
        packages = {node.get("Include"): node.get("Version") for node in project.findall("ItemGroup/PackageReference")}
        self.assertEqual(packages["Microsoft.WindowsAppSDK"], "1.8.260921001")
        adapter = project.find("ItemGroup/Content")
        self.assertEqual(adapter.get("Link"), "bridge.py")
        self.assertEqual(adapter.get("CopyToPublishDirectory"), "PreserveNewest")

    def test_process_launch_is_shell_free_isolated_and_bounded(self):
        code = (PROJECT / "Services/AgentClient.cs").read_text()
        for contract in ("UseShellExecute = false", 'ArgumentList.Add("-I")', 'ArgumentList.Add("-B")',
                         "Path.IsPathFullyQualified", "CancelAfter(TimeSpan.FromSeconds(20))",
                         "Kill(entireProcessTree: true)", "MaxOutput", "ThrowIfCancellationRequested"):
            self.assertIn(contract, code)
        self.assertNotIn("WriteAllText", code)
        self.assertNotIn("X-Api-Key", code)
        self.assertNotIn("HttpClient", code)

    def test_closing_shell_cancels_requests_without_stopping_agent(self):
        code = (PROJECT / "MainWindow.xaml.cs").read_text()
        self.assertIn("timer.Stop()", code)
        self.assertIn("lifetime.Cancel()", code)
        self.assertNotIn("stop_agent", code)
        self.assertNotIn("TerminateProcess", code)
        self.assertIn("if (busy || closed) return", code)
        self.assertIn('if (!volumeDirty && playerState != "offline")', code)
        self.assertIn("seekDirty = volumeDirty = false", code)

    def test_library_virtualization_is_not_wrapped_in_unbounded_scroll(self):
        root = ET.parse(PROJECT / "MainWindow.xaml").getroot()
        parents = {child: parent for parent in root.iter() for child in parent}
        library = next(node for node in root.iter() if node.get(XAML_NS + "Name") == "TrackList")
        self.assertTrue(any(node.tag.endswith("}ItemsStackPanel") for node in library.iter()))
        parent = parents[library]
        while parent is not root:
            self.assertFalse(parent.tag.endswith("}ScrollViewer"))
            self.assertFalse(parent.tag.endswith("}StackPanel"))
            parent = parents[parent]
        viewport = parents[library]
        self.assertEqual(viewport.get("Grid.Row"), "2")
        music = parents[viewport]
        rows = next(node for node in music if node.tag.endswith("}Grid.RowDefinitions"))
        self.assertEqual(rows[2].get("Height"), "*")

    def test_catalog_updates_are_batched_and_latest_search_is_serialized(self):
        code = (PROJECT / "MainWindow.xaml.cs").read_text()
        self.assertIn("TrackList.ItemsSource = loaded", code)
        self.assertNotIn("ObservableCollection", code)
        self.assertNotIn("tracks.Add", code)
        self.assertIn("pendingSearch = requested", code)
        self.assertIn("closed || pendingSearch is not null", code)
        self.assertIn("connected && !busy && activeWindow", code)
        self.assertNotIn("SearchBox.IsEnabled = !busy", code)
        client = (PROJECT / "Services/AgentClient.cs").read_text()
        self.assertIn("Task.Run(() => RequestCoreAsync", client)

    def test_material_has_themed_fallback_and_compact_viewport(self):
        code = (PROJECT / "MainWindow.xaml.cs").read_text()
        self.assertIn("MicaController.IsSupported()", code)
        self.assertIn("new MicaBackdrop()", code)
        self.assertIn("PlayerScroller.MaxHeight = Math.Clamp", code)
        root = ET.parse(PROJECT / "MainWindow.xaml").getroot()
        grid = next(node for node in root.iter() if node.get(XAML_NS + "Name") == "RootGrid")
        self.assertEqual(grid.get("Background"), "{ThemeResource ApplicationPageBackgroundThemeBrush}")


if __name__ == "__main__":
    unittest.main()
