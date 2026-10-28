"""Assistant safety and execution tests. Never opens an app or microphone."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pc_client"))
sys.path.insert(0, str(ROOT / "windows"))
import voice_assistant as va
import assistant_bridge as bridge


URL = "https://discord.com/channels/123456789012345678/234567890123456789"
JOIN = "подключись к голосовому каналу шаурма классическая"


class PlannerTests(unittest.TestCase):
    def test_all_supported_phrases_and_jarvis_prefix(self):
        planner = va.RulePlanner()
        for action, phrases in va.PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(planner.plan(phrase).action, action)
                    self.assertEqual(planner.plan("Джарвис, " + phrase.upper() + "!").action, action)

    def test_unknown_negated_compound_and_shell_commands_are_rejected(self):
        for text in ("", "не открой ютуб", "открой ютуб и дискорд", "открой ютуб; calc.exe",
                     "открой ютуб\nвыключи компьютер", "shutdown /s", "открой https://evil.invalid",
                     "открой ютуб на другом пк", "а" * 513, None, False, "открой\x00ютуб"):
            with self.subTest(text=text):
                with self.assertRaises(va.AssistantError):
                    va.RulePlanner().plan(text)

    def test_model_cannot_supply_extra_fields_or_parameters(self):
        for value in ({"action": "shell", "parameters": {}},
                      {"action": "open_youtube", "parameters": {"url": "file:///etc/passwd"}},
                      {"action": "launch_minecraft", "parameters": {"path": "cmd.exe"}},
                      {"action": "open_youtube", "parameters": {}, "confirmed": True},
                      {"action": "open_youtube", "parameters": []},
                      {"action": ["open_youtube"], "parameters": {}}, [], None):
            with self.subTest(value=value):
                with self.assertRaises(va.AssistantError):
                    va.validate_plan(value, "открой ютуб")

    def test_model_cannot_upgrade_navigation_to_join(self):
        for transcript in ("открой канал шаурма классическая", "подключись", "не " + JOIN,
                           JOIN + " и открой ютуб", "вступи в любой канал"):
            with self.assertRaises(va.AssistantError):
                va.validate_plan(va.Plan(va.Action.JOIN).to_dict(), transcript)

    def test_exact_join_is_a_plan_not_an_execution(self):
        self.assertEqual(va.RulePlanner().plan(JOIN), va.Plan(va.Action.JOIN))

    def test_settings_reject_unknown_types_and_provider(self):
        for value in ([], {"token": "unused"}, {"planner": "cloud"}, {"channel_url": True}):
            with self.assertRaises(va.AssistantError):
                va.Settings.from_dict(value)

    def test_channel_url_strict_host_scheme_and_ids(self):
        self.assertEqual(va.channel_ids(URL), ("123456789012345678", "234567890123456789"))
        for value in ("http://" + URL[8:], URL + "?join=true", URL + "/extra", URL + "#x",
                      URL.replace("discord.com", "discord.com.evil.invalid"), "discord://-/channels/1/2",
                      "https://discord.com/channels/@me/234567890123456789", "https://user@discord.com/channels/1/2"):
            with self.assertRaises(va.AssistantError):
                va.channel_ids(value)

    def test_ollama_uses_fixed_loopback_and_validates_returned_plan(self):
        response = Mock()
        response.read.return_value = json.dumps({"message": {"content": json.dumps(
            {"action": "open_youtube", "parameters": {}})}}).encode()
        opener = Mock()
        opener.open.return_value = contextlib.nullcontext(response)
        with patch.object(va.urllib.request, "build_opener", return_value=opener):
            result = va.OllamaPlanner("local-model:latest").plan("Открой YouTube, пожалуйста")
        self.assertEqual(result.action, va.Action.YOUTUBE)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 45)
        self.assertFalse(json.loads(request.data)["stream"])

    def test_ollama_cannot_join_without_explicit_user_phrase(self):
        response = Mock()
        response.read.return_value = json.dumps({"message": {"content": json.dumps(
            {"action": "join_discord_voice", "parameters": {}})}}).encode()
        opener = Mock()
        opener.open.return_value = contextlib.nullcontext(response)
        with patch.object(va.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(va.AssistantError):
                va.OllamaPlanner("model").plan("открой дискорд")

    def test_ollama_failure_never_silently_falls_back(self):
        opener = Mock()
        opener.open.side_effect = OSError("PRIVATE")
        with patch.object(va.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(va.AssistantError) as error:
                va.OllamaPlanner("model").plan("открой ютуб")
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_ollama_rejects_oversized_response(self):
        response = Mock()
        response.read.return_value = b"x" * 65537
        opener = Mock()
        opener.open.return_value = contextlib.nullcontext(response)
        with patch.object(va.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(va.AssistantError):
                va.OllamaPlanner("model").plan("открой ютуб")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.open = Mock(return_value=True)
        self.launch = Mock()
        self.discord = Mock()
        self.executor = va.LocalExecutor(open_url=self.open, launch=self.launch, discord=self.discord)

    def test_web_targets_are_constants_and_dispatch_is_not_success(self):
        for text, target in (("открой ютуб", "https://www.youtube.com/"),
                             ("открой дискорд", "discord://-/channels/@me")):
            result = self.executor.execute(va.RulePlanner().plan(text), text, va.Settings())
            self.open.assert_called_with(target)
            self.assertEqual(result.state, "dispatched")
        self.launch.assert_not_called()
        self.discord.join.assert_not_called()

    def test_navigation_never_calls_voice_connector(self):
        text = "перейди к каналу шаурма классическая"
        result = self.executor.execute(va.RulePlanner().plan(text), text, va.Settings(channel_url=URL))
        self.open.assert_called_once_with(URL)
        self.discord.join.assert_not_called()
        self.assertEqual(result.state, "dispatched")

    def test_bad_channel_url_has_no_side_effect(self):
        text = "открой канал шаурма классическая"
        with self.assertRaises(va.AssistantError):
            self.executor.execute(va.RulePlanner().plan(text), text, va.Settings(channel_url=""))
        self.open.assert_not_called()

    def test_failed_url_dispatch_is_reported(self):
        self.open.return_value = False
        result = self.executor.execute(va.Plan(va.Action.YOUTUBE), "открой ютуб", va.Settings())
        self.assertEqual(result.state, "failed")

    def test_join_needs_separate_true_confirmation(self):
        for value in (False, 1, "true", None):
            result = self.executor.execute(va.Plan(va.Action.JOIN), JOIN, va.Settings(channel_url=URL), confirmed=value)
            self.assertEqual(result.state, "needs_confirmation")
        self.discord.join.assert_not_called()
        self.open.assert_not_called()

    def test_default_join_is_blocked_without_authorized_connector(self):
        result = va.LocalExecutor(open_url=self.open, launch=self.launch).execute(
            va.Plan(va.Action.JOIN), JOIN, va.Settings(channel_url=URL), confirmed=True)
        self.assertEqual(result.state, "blocked")
        self.open.assert_not_called()

    def test_join_success_requires_connector_evidence(self):
        self.discord.join.return_value = False
        result = self.executor.execute(va.Plan(va.Action.JOIN), JOIN, va.Settings(channel_url=URL), confirmed=True)
        self.assertEqual(result.state, "unverified")
        self.discord.join.return_value = True
        result = self.executor.execute(va.Plan(va.Action.JOIN), JOIN, va.Settings(channel_url=URL), confirmed=True)
        self.assertEqual(result.state, "succeeded")
        self.open.assert_not_called()


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.exe = self.root / "TLauncher.exe"
        self.exe.write_bytes(b"fixture")
        self.properties = self.root / "tlauncher-2.0.properties"
        self.properties.write_text("login.version.game=forgeai2\nminecraft.gamedir=" + str(self.root).replace("\\", "\\\\") + "\n", encoding="utf-8")
        self.version_file = self.root / "versions" / "forgeai2" / "forgeai2.json"
        self.version_file.parent.mkdir(parents=True)
        self.version = {"id": "forgeai2", "arguments": {"game": ["--fml.mcVersion", "1.20.1"]},
                        "libraries": [{"name": "net.minecraftforge:fmlloader:1.20.1-47.4.10"}]}
        self.write_version()
        self.settings = va.Settings(launcher_path=str(self.exe), launcher_properties_path=str(self.properties), minecraft_dir=str(self.root))

    def write_version(self):
        self.version_file.write_text(json.dumps(self.version), encoding="utf-8")

    def test_launch_uses_no_shell_or_profile_mutation(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        launch = Mock()
        result = va.LocalExecutor(launch=launch).execute(va.Plan(va.Action.MINECRAFT), "запусти майнкрафт", self.settings)
        self.assertEqual(result.state, "dispatched")
        self.assertEqual(launch.call_args.args[0], [str(self.exe)])
        self.assertIs(launch.call_args.kwargs["shell"], False)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_tlauncher_values_argument_format_is_supported(self):
        self.version["arguments"]["game"] = [{"values": ["--fml.mcVersion"], "rules": []},
                                             {"values": ["1.20.1"], "rules": []}]
        self.write_version()
        self.assertEqual(va.launcher_target(self.settings), self.exe)

    def test_different_profile_does_not_launch_or_rewrite(self):
        self.properties.write_text("login.version.game=other\n", encoding="utf-8")
        launch = Mock()
        with self.assertRaises(va.AssistantError):
            va.LocalExecutor(launch=launch).execute(va.Plan(va.Action.MINECRAFT), "запусти майнкрафт", self.settings)
        launch.assert_not_called()
        self.assertIn("other", self.properties.read_text())

    def test_wrong_minecraft_or_forge_version_is_rejected(self):
        for args, libs in ((["--fml.mcVersion", "1.21"], self.version["libraries"]),
                           (["--fml.mcVersion", "1.20.1"], []), ([], self.version["libraries"])):
            self.version["arguments"]["game"] = args
            self.version["libraries"] = libs
            self.write_version()
            with self.assertRaises(va.AssistantError):
                va.launcher_target(self.settings)

    def test_other_executables_and_relative_paths_rejected(self):
        for value in ("TLauncher.exe", str(self.root / "cmd.exe"), str(self.exe) + " --flag"):
            with self.assertRaises(va.AssistantError):
                va.launcher_target(va.Settings(launcher_path=value))

    def test_different_launcher_game_directory_rejected(self):
        self.properties.write_text("login.version.game=forgeai2\nminecraft.gamedir=C:/other\n", encoding="utf-8")
        with self.assertRaises(va.AssistantError):
            va.launcher_target(self.settings)


class DiscordRpcTests(unittest.TestCase):
    def channel(self):
        return {"id": "234567890123456789", "guild_id": "123456789012345678",
                "name": va.CHANNEL_NAME, "type": 2}

    def test_verified_join_uses_force_false_and_readback(self):
        rpc = Mock(side_effect=[self.channel(), None, {}, self.channel()])
        connector = va.AuthorizedDiscordRpc(rpc)
        self.assertTrue(connector.join(*va.channel_ids(URL)))
        self.assertEqual([c.args[0] for c in rpc.call_args_list],
                         ["GET_CHANNEL", "GET_SELECTED_VOICE_CHANNEL", "SELECT_VOICE_CHANNEL", "GET_SELECTED_VOICE_CHANNEL"])
        self.assertEqual(rpc.call_args_list[2].args[1], {"channel_id": "234567890123456789", "force": False})

    def test_wrong_identity_or_nonvoice_channel_prevents_join(self):
        for key, value in (("id", "other"), ("guild_id", "other"), ("name", "other"), ("type", 0)):
            channel = self.channel()
            channel[key] = value
            rpc = Mock(return_value=channel)
            with self.assertRaises(va.AssistantError):
                va.AuthorizedDiscordRpc(rpc).join(*va.channel_ids(URL))
            self.assertEqual(rpc.call_count, 1)

    def test_existing_other_voice_channel_is_not_switched(self):
        rpc = Mock(side_effect=[self.channel(), {"id": "other"}])
        with self.assertRaises(va.AssistantError):
            va.AuthorizedDiscordRpc(rpc).join(*va.channel_ids(URL))
        self.assertEqual(rpc.call_count, 2)

    def test_no_readback_is_not_success(self):
        rpc = Mock(side_effect=[self.channel(), None, {}, None])
        self.assertFalse(va.AuthorizedDiscordRpc(rpc).join(*va.channel_ids(URL)))

    def test_already_in_target_does_not_rejoin(self):
        rpc = Mock(side_effect=[self.channel(), self.channel(), self.channel()])
        self.assertTrue(va.AuthorizedDiscordRpc(rpc).join(*va.channel_ids(URL)))
        self.assertNotIn("SELECT_VOICE_CHANNEL", [c.args[0] for c in rpc.call_args_list])


class BridgeTests(unittest.TestCase):
    def test_plan_does_not_execute_or_record(self):
        executor = Mock()
        result = bridge.handle({"operation": "plan", "text": "открой ютуб"}, executor=executor)
        self.assertEqual(result["state"], "ready")
        executor.execute.assert_not_called()

    def test_bad_request_and_operation_rejected(self):
        for request in ([], {"operation": "shell"}, {"operation": "plan", "token": "unused"}):
            with self.assertRaises(va.AssistantError):
                bridge.handle(request)

    def test_record_stages_close_microphone_before_transcription(self):
        import voice_capture
        transcriber = Mock()
        transcriber.transcribe.return_value = "открой ютуб"
        events = []
        with patch.object(voice_capture, "WhisperTranscriber", return_value=transcriber), \
             patch.object(voice_capture, "record_pcm", return_value=b"pcm") as record:
            result = bridge.handle({"operation": "record"}, progress=events.append)
        self.assertEqual(events, ["loading_model", "recording", "transcribing", "planning"])
        self.assertEqual(result["state"], "ready")
        record.assert_called_once_with()

    def test_capture_cleanup_failure_never_announces_transcribing_or_uses_audio(self):
        import voice_capture
        transcriber = Mock()
        events = []
        with patch.object(voice_capture, "WhisperTranscriber", return_value=transcriber), \
             patch.object(voice_capture, "record_pcm", side_effect=va.AssistantError("cleanup not confirmed")):
            with self.assertRaises(va.AssistantError):
                bridge.handle({"operation": "record"}, progress=events.append)
        self.assertEqual(events, ["loading_model", "recording"])
        transcriber.transcribe.assert_not_called()

    def test_model_error_does_not_open_microphone(self):
        import voice_capture
        with patch.object(voice_capture, "WhisperTranscriber", side_effect=va.AssistantError("no model")), \
             patch.object(voice_capture, "record_pcm") as record:
            with self.assertRaises(va.AssistantError):
                bridge.handle({"operation": "record"})
        record.assert_not_called()

    def test_unknown_recorded_text_remains_editable(self):
        import voice_capture
        transcriber = Mock()
        transcriber.transcribe.return_value = "неизвестная команда"
        with patch.object(voice_capture, "WhisperTranscriber", return_value=transcriber), \
             patch.object(voice_capture, "record_pcm", return_value=b"pcm"):
            result = bridge.handle({"operation": "record"})
        self.assertEqual(result["text"], "неизвестная команда")
        self.assertEqual(result["state"], "rejected")
        self.assertIsNone(result["plan"])

    def test_execute_revalidates_untrusted_plan(self):
        executor = Mock()
        with self.assertRaises(va.AssistantError):
            bridge.handle({"operation": "execute", "text": "открой ютуб",
                           "plan": {"action": "shell", "parameters": {}}}, executor=executor)
        executor.execute.assert_not_called()

    def test_real_isolated_bridge_text_roundtrip_without_agent_or_secrets(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "windows" / "assistant_bridge.py")],
                                input=json.dumps({"operation": "plan", "text": "открой ютуб"}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(rows[-1]["ok"])
        self.assertEqual(rows[-1]["result"]["plan"]["action"], "open_youtube")
        self.assertEqual(result.stderr, "")

    def test_oversized_request_rejected_by_process(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "windows" / "assistant_bridge.py")],
                                input="x" * 16385, text=True, capture_output=True, timeout=10)
        self.assertFalse(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
