"""Native-shell contract tests: temporary fixtures, no network, audio or keys.

Run from the repository root with:
    python -m unittest discover -s windows/tests -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "bridge.py"
SPEC = importlib.util.spec_from_file_location("xass_native_bridge_contract", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

NOW = 1_700_000_000.0
PRIVATE = "private-fixture-must-not-cross-stdout"


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        # Avoid the constructor's imports entirely: these tests cannot load an
        # installed agent, inspect real configuration, or access a real player.
        self.adapter = bridge.AgentAdapter.__new__(bridge.AgentAdapter)
        self.adapter.data = self.data
        self.config = {
            "source_name": "Fixture PC", "api_key": "ag_" + PRIVATE,
            "server_url": "https://xass.example.invalid", "private_jwk": PRIVATE,
        }
        self.adapter.unseal = Mock(side_effect=lambda value, **_: dict(value))
        self.adapter.secure_url = Mock(side_effect=lambda value, **_: value)
        self.response = Mock(content=b"{}")
        self.response.json.return_value = {
            "tracks": [], "offset": 0, "has_more": False, "total": 0,
        }
        self.client = Mock()
        self.client.get.return_value = self.response
        self.client.post.return_value = self.response
        self.adapter.http = Mock(return_value=contextlib.nullcontext(self.client))
        self.adapter.read_playback = Mock(return_value={
            "state": "playing", "track_id": 5, "title": "Track", "artist": "Artist",
            "position_sec": 12.5, "duration_sec": 240, "volume": 70,
            "api_key": PRIVATE, "stream_url": "https://private.invalid/" + PRIVATE,
        })
        self.adapter.send_command = Mock(return_value={"token": PRIVATE})
        self.write("config.json", self.config)
        self.write(".agent-status.json", {"state": "online", "updated_at": NOW,
                                          "detail": PRIVATE, "api_key": PRIVATE})
        self.write("music-playback.json", {})
        os.utime(self.data / "music-playback.json", (NOW, NOW))
        self.clock = patch.object(bridge.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write(self, name, value):
        (self.data / name).write_text(json.dumps(value), encoding="utf-8")

    def catalog(self, **changes):
        body = {"tracks": [{"id": 5, "title": "Track", "artist": "Artist", "album": "Album"}],
                "offset": 0, "has_more": False, "total": 1}
        body.update(changes)
        self.response.json.return_value = body
        return self.adapter.request("catalog", {})

    def test_exact_action_allowlist_rejects_dangerous_commands_before_io(self):
        self.assertEqual(bridge.ACTIONS, {"snapshot", "catalog", "play", "pause", "resume",
                                         "stop", "seek", "volume"})
        for action in ("", "reboot", "shutdown", "exec", "music_play", "save_config", "import", "PLAY"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.adapter.request(action, {})
        self.adapter.unseal.assert_not_called()
        self.adapter.http.assert_not_called()
        self.adapter.send_command.assert_not_called()
        self.adapter.read_playback.assert_not_called()

    def test_transport_actions_send_only_the_allowed_payload(self):
        for action in ("pause", "resume", "stop"):
            with self.subTest(action=action):
                result = self.adapter.request(action, {"command": "shutdown", "output_id": PRIVATE,
                                                       "token": PRIVATE})
                self.assertEqual(result, {"accepted": True})
                self.adapter.send_command.assert_called_with("music_" + action, {}, root=self.data)
        self.adapter.unseal.assert_not_called()
        self.adapter.http.assert_not_called()

    def test_seek_and_volume_send_validated_numbers_without_extra_fields(self):
        for action, field, sent_field, limit in (("seek", "position", "position_sec", 86400),
                                                ("volume", "volume", "volume", 100)):
            for value in (0, 1.25, limit):
                with self.subTest(action=action, value=value):
                    result = self.adapter.request(action, {field: value, "output_id": PRIVATE,
                                                           "command": "shutdown"})
                    self.assertEqual(result, {"accepted": True})
                    self.adapter.send_command.assert_called_with(
                        "music_" + action, {sent_field: float(value)}, root=self.data)

    def test_number_actions_reject_booleans_nonfinite_and_out_of_range(self):
        for action, field, maximum in (("seek", "position", 86400), ("volume", "volume", 100),
                                       ("play", "volume", 100)):
            for value in (True, False, None, "1", [], {}, -0.01, maximum + 0.01,
                          float("nan"), float("inf"), float("-inf")):
                with self.subTest(action=action, value=value), self.assertRaises(ValueError):
                    self.adapter.request(action, {field: value, "track_id": 5})
        self.adapter.http.assert_not_called()
        self.adapter.send_command.assert_not_called()
        self.adapter.unseal.assert_not_called()

    def test_play_rejects_noninteger_or_nonpositive_ids_before_credentials(self):
        for value in (None, True, False, 0, -1, 2147483648, 10**100,
                      1.0, "1", "../shutdown", {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.adapter.request("play", {"track_id": value})
        self.adapter.http.assert_not_called()
        self.adapter.unseal.assert_not_called()

    def test_play_uses_saved_credentials_secure_transport_and_default_output(self):
        result = self.adapter.request("play", {"track_id": 5, "volume": 42.2,
                                              "output_id": "attacker-device", "url": PRIVATE})
        self.assertEqual(result, {"accepted": True})
        self.adapter.secure_url.assert_called_once_with("https://xass.example.invalid",
                                                       allow_insecure_http=False)
        self.adapter.http.assert_called_once_with("https://xass.example.invalid", timeout=10,
                                                  trust_env=False, follow_redirects=False)
        self.client.post.assert_called_once_with("https://xass.example.invalid/agent/music/library/5/play",
                                                 headers={"X-Api-Key": "ag_" + PRIVATE,
                                                          "Accept": "application/json"},
                                                 json={"output_id": "default", "volume": 42})
        self.response.raise_for_status.assert_called_once()
        self.response.json.assert_not_called()
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_play_accepts_largest_int32_track_id(self):
        self.assertEqual(self.adapter.request("play", {"track_id": 2147483647}), {"accepted": True})
        self.assertEqual(self.client.post.call_args.args[0],
                         "https://xass.example.invalid/agent/music/library/2147483647/play")

    def test_rejected_secure_transport_never_opens_http_client(self):
        self.adapter.secure_url.side_effect = RuntimeError(PRIVATE)
        for action, request in (("catalog", {}), ("play", {"track_id": 1})):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                self.adapter.request(action, request)
        self.adapter.http.assert_not_called()

    def test_unpaired_config_never_opens_http_client(self):
        for key in (None, "", "not-an-agent-key", True, {}, []):
            self.write("config.json", {**self.config, "api_key": key})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.adapter.request("catalog", {})
        self.adapter.http.assert_not_called()
        self.adapter.secure_url.assert_not_called()

    def test_catalog_projection_discards_all_nonpublic_fields(self):
        result = self.catalog(tracks=[{"id": 5, "title": "Track", "artist": "Artist", "album": "Album",
                                       "api_key": PRIVATE, "url": PRIVATE, "path": PRIVATE,
                                       "credentials": {"token": PRIVATE}, "lyrics": PRIVATE,
                                       "artwork_url": PRIVATE}], api_key=PRIVATE, server_url=PRIVATE)
        self.assertEqual(result, {"tracks": [{"id": 5, "title": "Track", "artist": "Artist", "album": "Album"}],
                                  "total": 1, "next_offset": None})
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_catalog_query_and_display_strings_are_bounded(self):
        self.response.json.return_value = {"tracks": [{"id": 5, "title": "T" * 600,
                                                       "artist": "A" * 600, "album": "B" * 600}],
                                           "offset": 100, "total": 201, "has_more": True,
                                           "next_offset": 101}
        result = self.adapter.request("catalog", {"query": " " + "Q" * 500 + " ", "offset": 100})
        self.assertEqual(self.client.get.call_args.kwargs["params"],
                         {"limit": 100, "offset": 100, "q": "Q" * 120})
        self.assertEqual([len(result["tracks"][0][field]) for field in ("title", "artist", "album")],
                         [240, 240, 240])
        self.assertEqual(result["next_offset"], 101)

    def test_catalog_rejects_invalid_request_offsets_before_credentials(self):
        for value in (True, False, -1, 100001, 1.0, "0", None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.adapter.request("catalog", {"offset": value})
        self.adapter.unseal.assert_not_called()
        self.adapter.http.assert_not_called()

    def test_catalog_rejects_oversized_response_before_json_decode(self):
        self.response.content = b"x" * (bridge.MAX_RESPONSE + 1)
        with self.assertRaises(ValueError):
            self.adapter.request("catalog", {})
        self.response.json.assert_not_called()

    def test_catalog_rejects_bad_body_shape_or_excessive_rows(self):
        for body in ([], None, "text", {}, {"tracks": {}},
                     {"tracks": [{"id": 1}] * 101, "offset": 0}):
            self.response.json.return_value = body
            with self.subTest(body=type(body).__name__), self.assertRaises(ValueError):
                self.adapter.request("catalog", {})

    def test_catalog_rejects_invalid_rows_and_ids(self):
        for row in (None, [], "row", {}, {"id": True}, {"id": 0}, {"id": -1},
                    {"id": False}, {"id": 1.0}, {"id": "1"}, {"id": 2147483648}, {"id": 10**100}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.catalog(tracks=[row])

    def test_catalog_rejects_invalid_or_mismatched_echoed_offsets(self):
        for value in (None, True, False, -1, 1, 0.0, "0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.catalog(offset=value)

    def test_catalog_requires_advancing_next_page_when_more_is_true(self):
        for next_offset in (None, True, False, 0, -1, 100001, 1.0, "1"):
            with self.subTest(next_offset=next_offset), self.assertRaises(ValueError):
                self.catalog(has_more=True, next_offset=next_offset)
        with self.assertRaises(ValueError):
            self.catalog(has_more=True)
        with self.assertRaises(ValueError):
            self.catalog(tracks=[], has_more=True, next_offset=1)
        result = self.catalog(has_more=True, next_offset=1, total=2)
        self.assertEqual(result["next_offset"], 1)

    def test_catalog_rejects_invalid_totals(self):
        for value in (None, True, False, -1, 1.0, "1", [], {}, 2147483648, 10**100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.catalog(total=value)

    def test_catalog_requires_boolean_pagination_flag(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.catalog(has_more=value, next_offset=1)
        self.response.json.return_value = {"tracks": [], "offset": 0, "total": 0}
        with self.assertRaises(ValueError):
            self.adapter.request("catalog", {})

    def test_catalog_accepts_int32_boundaries_and_ignores_unused_next_page(self):
        result = self.catalog(tracks=[{"id": 2147483647}], total=2147483647,
                              has_more=False, next_offset={"token": PRIVATE})
        self.assertEqual(result["tracks"][0]["id"], 2147483647)
        self.assertEqual(result["total"], 2147483647)
        self.assertIsNone(result["next_offset"])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_snapshot_is_a_public_projection_without_decrypting(self):
        result = self.adapter.request("snapshot", {})
        self.assertEqual(result, {"device": {"name": "Fixture PC", "state": "online"},
                                  "playback": {"state": "playing", "track_id": 5, "title": "Track", "artist": "Artist",
                                               "position": 12.5, "duration": 240.0, "volume": 70.0}})
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.adapter.unseal.assert_not_called()
        self.adapter.http.assert_not_called()
        self.adapter.send_command.assert_not_called()

    def test_snapshot_track_id_is_positive_int32_or_null(self):
        for value in (1, 2147483647):
            self.adapter.read_playback.return_value = {"track_id": value}
            with self.subTest(value=value):
                result = self.adapter.request("snapshot", {})
                self.assertEqual(result["playback"]["track_id"], value)
                self.assertIs(type(result["playback"]["track_id"]), int)
        for value in (None, True, False, 0, -1, 2147483648, 10**100, 1.0, "1", [], {"token": PRIVATE}):
            self.adapter.read_playback.return_value = {"track_id": value}
            with self.subTest(value=value):
                result = self.adapter.request("snapshot", {})
                self.assertIsNone(result["playback"]["track_id"])
                self.assertNotIn(PRIVATE, json.dumps(result))
        self.adapter.read_playback.return_value = {}
        self.assertIsNone(self.adapter.request("snapshot", {})["playback"]["track_id"])

    def test_snapshot_expired_missing_or_future_device_status_is_offline(self):
        for updated_at in (0, NOW - 106, NOW + 6):
            self.write(".agent-status.json", {"updated_at": updated_at, "state": "online"})
            with self.subTest(updated_at=updated_at):
                self.assertEqual(self.adapter.request("snapshot", {})["device"]["state"], "offline")
        (self.data / ".agent-status.json").unlink()
        self.assertEqual(self.adapter.request("snapshot", {})["device"]["state"], "offline")

    def test_snapshot_old_missing_or_future_playback_is_offline(self):
        for timestamp in (NOW - 11, NOW + 60):
            os.utime(self.data / "music-playback.json", (timestamp, timestamp))
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.adapter.request("snapshot", {})["playback"]["state"], "offline")
        (self.data / "music-playback.json").unlink()
        self.assertEqual(self.adapter.request("snapshot", {})["playback"]["state"], "offline")

    def test_snapshot_invalid_numeric_values_do_not_leave_nonfinite_outputs(self):
        for field, maximum in (("position_sec", 86400), ("duration_sec", 86400), ("volume", 100)):
            for value in (True, -1, maximum + 1, "12", float("nan"), float("inf")):
                self.adapter.read_playback.return_value = {field: value, "state": "playing"}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.adapter.request("snapshot", {})

    def test_catalog_nested_display_data_never_exposes_secrets(self):
        for field in ("title", "artist", "album"):
            with self.subTest(field=field):
                try:
                    result = self.catalog(tracks=[{"id": 5, field: {"token": PRIVATE}}])
                except ValueError:
                    continue
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_snapshot_unknown_states_and_nested_display_data_are_not_exposed(self):
        self.write("config.json", {**self.config, "source_name": {"token": PRIVATE}})
        self.write(".agent-status.json", {"updated_at": NOW, "state": PRIVATE})
        self.adapter.read_playback.return_value = {"state": PRIVATE, "title": {"token": PRIVATE},
                                                   "artist": {"token": PRIVATE}}
        try:
            result = self.adapter.request("snapshot", {})
        except ValueError:
            return
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_all_successful_actions_leave_configuration_and_data_files_unchanged(self):
        self.write("config.json.bak", {"api_key": PRIVATE})
        before = {path.name: path.read_bytes() for path in self.data.iterdir()}
        for action, request in (("snapshot", {}), ("catalog", {}), ("play", {"track_id": 1}),
                                ("pause", {}), ("resume", {}), ("stop", {}),
                                ("seek", {"position": 0}), ("volume", {"volume": 0})):
            self.adapter.request(action, request)
        after = {path.name: path.read_bytes() for path in self.data.iterdir()}
        self.assertEqual(after, before)

    def test_broken_config_is_not_restored_from_backup(self):
        (self.data / "config.json").write_text("broken", encoding="utf-8")
        self.write("config.json.bak", self.config)
        with self.assertRaises(ValueError):
            self.adapter.config()
        self.assertEqual((self.data / "config.json").read_text(encoding="utf-8"), "broken")
        self.adapter.unseal.assert_not_called()

    def test_aes_config_with_missing_or_invalid_master_key_does_not_create_one(self):
        self.write("config.json", {"sealed": {"cipher": "aes-256-gcm", "data": PRIVATE}})
        master = self.data / ".xass-master.key"
        with self.assertRaises(ValueError):
            self.adapter.config()
        self.assertFalse(master.exists())
        for value in (b"", b"x" * 31, b"x" * 33):
            master.write_bytes(value)
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                self.adapter.config()
            self.assertEqual(master.read_bytes(), value)
        self.adapter.unseal.assert_not_called()


class ProtocolContractTests(unittest.TestCase):
    def run_main(self, text, adapter=None, error=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(bridge.sys, "argv", ["bridge.py", "--source", "fixture-source", "--data", "fixture-data"]), \
             patch.object(bridge.sys, "stdin", io.StringIO(text)), \
             patch.object(bridge.sys, "stdout", output), patch.object(bridge.sys, "stderr", errors), \
             patch.object(bridge, "AgentAdapter", return_value=adapter, side_effect=error) as constructor:
            bridge.main()
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        return json.loads(output.getvalue()), constructor

    def test_malformed_oversized_or_unsupported_requests_do_not_construct_adapter(self):
        for value in ("", "{", "[]", "null", '"snapshot"', '{"action":"shutdown"}',
                      '{"action":[]}', " " * (bridge.MAX_REQUEST + 1)):
            with self.subTest(value=value[:60]):
                result, constructor = self.run_main(value)
                self.assertIs(result["ok"], False)
                self.assertEqual(set(result), {"ok", "error"})
                constructor.assert_not_called()

    def test_errors_do_not_expose_exception_details(self):
        result, _ = self.run_main('{"action":"snapshot"}', error=RuntimeError(PRIVATE))
        self.assertIs(result["ok"], False)
        self.assertNotIn(PRIVATE, json.dumps(result))
        adapter = Mock()
        adapter.request.side_effect = RuntimeError(PRIVATE)
        result, _ = self.run_main('{"action":"catalog"}', adapter=adapter)
        self.assertIs(result["ok"], False)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_dependency_diagnostics_never_contaminate_stdout_or_stderr(self):
        def request(*_):
            print(PRIVATE)
            print(PRIVATE, file=bridge.sys.stderr)
            return {"accepted": True}

        adapter = Mock()
        adapter.request.side_effect = request
        result, constructor = self.run_main('{"action":"pause"}', adapter=adapter)
        self.assertEqual(result, {"ok": True, "result": {"accepted": True}})
        constructor.assert_called_once_with(Path("fixture-source"), Path("fixture-data"))
        adapter.request.assert_called_once_with("pause", {"action": "pause"})

    def test_bounded_object_reader_requires_a_json_object(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.json"
            for value in ("[]", "null", "broken", "{" + " " * 100 + "}"):
                path.write_text(value, encoding="utf-8")
                with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                    bridge.read_object(path, limit=32)
            path.write_text('\ufeff{"name":"fixture"}', encoding="utf-8")
            self.assertEqual(bridge.read_object(path), {"name": "fixture"})


if __name__ == "__main__":
    unittest.main()
