"""Source invariants only: real Windows build/device acceptance remains required."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "Xass.Native"
NS = "{http://schemas.microsoft.com/winfx/2006/xaml}"

class VoiceUiContractTests(unittest.TestCase):
    def test_background_is_opt_in_and_control_is_visible_on_every_page(self):
        root = ET.parse(ROOT / "MainWindow.xaml").getroot()
        names = {element.get(NS + "Name"): element for element in root.iter()}
        self.assertEqual(names["BackgroundListening"].get("IsOn"), "False")
        self.assertIn("MicrophoneStatus", names)
        self.assertIn("BackgroundStop", names)
        code = (ROOT / "MainWindow.Voice.cs").read_text()
        self.assertIn("backgroundVoice?.Stop()", code)
        self.assertIn("backgroundLifetime?.Cancel()", code)
        self.assertNotIn("activeWindow", code)  # Minimize must not silently change the opted-in policy.

    def test_preview_and_tts_block_capture_and_never_execute_automatically(self):
        code = (ROOT / "MainWindow.Voice.cs").read_text()
        self.assertIn("!assistantDraftPending", code)
        self.assertIn("assistantPlan is null && assistantRequest is null && !voiceSpeaking", code)
        self.assertIn("update.Epoch != backgroundEpoch", code)
        self.assertIn('RunAssistantAsync("plan")', code)
        self.assertNotIn('RunAssistantAsync("execute")', code)
        self.assertIn("await worker.PauseAsync", code)
        self.assertLess(code.index("await RefreshBackgroundAsync(); // Require microphone-closed"),
                        code.index("await localSpeech.SpeakAsync"))

    def test_installed_voice_only_and_owned_stream_lifetime(self):
        code = (ROOT / "Services/LocalSpeechService.cs").read_text()
        for required in ("SpeechSynthesizer.AllVoices", "VoiceGender.Female", '"ru-RU"', '"Irina"',
                         "SynthesizeTextToStreamAsync", "MediaSource.CreateFromStream", "using var stream",
                         "using var source", "using var media", "MediaEnded -=", "MediaFailed -=", "media.Pause()"):
            self.assertIn(required, code)
        for forbidden in ("HttpClient", "SynthesizeSsml", "WriteAll", "Download"):
            self.assertNotIn(forbidden, code)

    def test_bundled_helper_precedes_external_python_and_uses_explicit_roles(self):
        code = (ROOT / "Services/AssistantProcessStart.cs").read_text()
        self.assertIn('"runtime", "XASS.NativeHelper.exe"', code)
        self.assertIn('background ? "listener" : "assistant"', code)
        self.assertIn('FileName = bundled ? helper : python', code)
        self.assertIn("UseShellExecute = false", code)
        self.assertIn('ArgumentList.Add("-I")', code)
        self.assertIn('ArgumentList.Add("-B")', code)

    def test_cancel_removes_preview_and_failed_shutdown_stays_visible(self):
        window = (ROOT / "MainWindow.xaml.cs").read_text()
        handler = window.split("private void AssistantCancelClick", 1)[1].split("private async Task RunAsync", 1)[0]
        self.assertLess(handler.index("InvalidateAssistantPlan()"), handler.index("assistantRequest.Cancel()"))
        self.assertIn("assistantDraftPending = false", handler)
        voice = (ROOT / "MainWindow.Voice.cs").read_text()
        self.assertIn("if (microphoneShutdownFailed)", voice)
        self.assertIn("Остановка микрофона не подтверждена", voice)
        self.assertIn("speechCancellation?.Cancel()", voice)
        self.assertIn("cancel.Token.ThrowIfCancellationRequested()", voice)
        client = (ROOT / "Services/AssistantClient.cs").read_text()
        self.assertIn("await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3))", client)

    def test_background_and_one_shot_companions_are_published(self):
        project = ET.parse(ROOT / "Xass.Native.csproj").getroot()
        files = {node.get("Link") for node in project.findall("ItemGroup/Content")}
        self.assertTrue({"background_voice.py", "background_voice_bridge.py", "voice_capture.py",
                         "assistant_bridge.py", "voice_assistant.py"} <= files)
