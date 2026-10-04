"""One request per process. Agent secrets stay in Python and never cross stdout.

Run only with trusted, local XASS source/dependency paths. This adapter creates
no listener, tokens, settings or account; the existing agent owns playback.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

MAX_REQUEST = 8192
MAX_RESPONSE = 512 * 1024
ACTIONS = frozenset({"snapshot", "catalog", "play", "pause", "resume", "stop", "seek", "volume"})
DEVICE_STATES = frozenset({"online", "connecting", "offline", "error", "stale"})
PLAYER_STATES = frozenset({"idle", "loading", "stopping", "playing", "paused", "stopped", "ended", "error", "offline"})


def label(value: Any, fallback: str = "") -> str:
    return value[:240] if isinstance(value, str) and value else fallback


def number(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Number out of range")
    return value


def read_object(path: Path, limit: int = 65536) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("File too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


class AgentAdapter:
    def __init__(self, source: Path, data: Path):
        source, self.data = source.resolve(strict=True), data.resolve(strict=True)
        if not source.is_dir() or not self.data.is_dir():
            raise ValueError("Agent folders are unavailable")
        for module in ("secret_store.py", "network_client.py", "music_bridge.py"):
            if not (source / module).is_file():
                raise ValueError("Select the existing pc_client source folder")
        sys.path.insert(0, str(source))
        # Five-second status polling stays stdlib-only: do not repeatedly load
        # HTTP, crypto/audio modules or provision runtime directories for it.
        self.read_playback = self._read_playback

    def _read_playback(self, *, root: Path) -> dict:
        try:
            return read_object(root / "music-playback.json")
        except (OSError, ValueError):
            return {}

    def _load_dependencies(self) -> None:
        if hasattr(self, "http"):
            return
        # Only explicit library/player actions need the existing agent modules.
        from secret_store import unseal_config
        from network_client import create_http_client, require_secure_transport
        from music_bridge import read_playback, send_command
        self.unseal = unseal_config
        self.http = create_http_client
        self.secure_url = require_secure_transport
        self.read_playback = read_playback
        self.send_command = send_command

    def config(self) -> dict:
        self._load_dependencies()
        raw = read_object(self.data / "config.json")
        if isinstance(raw.get("sealed"), dict) and raw["sealed"].get("cipher") == "aes-256-gcm":
            # secret_store's old AES helper provisions a missing master key.
            # A native reader must never provision or replace credentials.
            master = self.data / ".xass-master.key"
            if not master.is_file() or master.stat().st_size != 32:
                raise ValueError("Existing agent key is unavailable")
        return self.unseal(raw, data_dir=self.data)

    def request(self, action: str, request: dict) -> dict:
        if action not in ACTIONS:
            raise ValueError("Unsupported action")
        if action == "snapshot":
            # Public projection only; do not decrypt configuration for status.
            config = read_object(self.data / "config.json")
            try:
                report = read_object(self.data / ".agent-status.json")
            except (OSError, ValueError):
                report = {}
            age = time.time() - number(report.get("updated_at", 0), 0, 1e12)
            state = label(report.get("state"), "offline") if -5 <= age <= 105 else "offline"
            if state not in DEVICE_STATES:
                state = "offline"
            playback = self.read_playback(root=self.data)
            # Playback file may survive an exited agent. Never show stale play.
            try:
                fresh = -5 <= time.time() - (self.data / "music-playback.json").stat().st_mtime <= 10
            except OSError:
                fresh = False
            player_state = label(playback.get("state"), "idle")
            track_id = playback.get("track_id")
            if type(track_id) is not int or not 0 < track_id <= 2147483647:
                track_id = None
            return {"device": {"name": label(config.get("source_name"), "Этот компьютер"),
                               "state": state},
                    "playback": {"state": player_state if fresh and player_state in PLAYER_STATES else "offline",
                                 "track_id": track_id,
                                 "title": label(playback.get("title"), "Ничего не играет"),
                                 "artist": label(playback.get("artist")),
                                 "position": number(playback.get("position_sec") or 0, 0, 86400),
                                 "duration": number(playback.get("duration_sec") or 0, 0, 86400),
                                 "volume": number(playback.get("volume") or 0, 0, 100)}}
        if action in {"pause", "resume", "stop", "seek", "volume"}:
            payload = {}
            if action == "seek":
                payload["position_sec"] = number(request.get("position"), 0, 86400)
            if action == "volume":
                payload["volume"] = number(request.get("volume"), 0, 100)
            self._load_dependencies()
            self.send_command("music_" + action, payload, root=self.data)
            return {"accepted": True}
        offset = request.get("offset", 0)
        if action == "catalog" and (type(offset) is not int or not 0 <= offset <= 100000):
            raise ValueError("Invalid offset")
        track_id = request.get("track_id")
        if action == "play" and (type(track_id) is not int or not 0 < track_id <= 2147483647):
            raise ValueError("Invalid track")
        volume = number(request.get("volume", 70), 0, 100)
        config = self.config()
        key = config.get("api_key", "")
        if not isinstance(key, str) or not key.startswith("ag_"):
            raise ValueError("Pair this PC in the existing XASS application first")
        base = self.secure_url(str(config.get("server_url") or ""),
                               allow_insecure_http=config.get("allow_insecure_http") is True).rstrip("/")
        with self.http(base, timeout=10, trust_env=bool(config.get("trust_env_proxy", False)),
                       follow_redirects=False) as client:
            headers = {"X-Api-Key": key, "Accept": "application/json"}
            if action == "play":
                response = client.post(f"{base}/agent/music/library/{track_id}/play", headers=headers,
                                       json={"output_id": "default", "volume": round(volume)})
                response.raise_for_status()
                return {"accepted": True}
            response = client.get(base + "/agent/music/library", headers=headers,
                                  params={"limit": 100, "offset": offset, "q": str(request.get("query") or "").strip()[:120]})
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE:
                raise ValueError("Catalog too large")
            body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("tracks"), list) or len(body["tracks"]) > 100:
            raise ValueError("Invalid catalog")
        if type(body.get("offset")) is not int or body["offset"] != offset:
            raise ValueError("Invalid catalog offset")
        rows = []
        for track in body["tracks"]:
            if not isinstance(track, dict) or type(track.get("id")) is not int or not 0 < track["id"] <= 2147483647:
                raise ValueError("Invalid catalog track")
            rows.append({"id": track["id"], "title": label(track.get("title"), "Без названия"),
                         "artist": label(track.get("artist"), "Неизвестный исполнитель"),
                         "album": label(track.get("album"))})
        if type(body.get("has_more")) is not bool:
            raise ValueError("Invalid pagination flag")
        next_offset = body.get("next_offset") if body.get("has_more") else None
        if body.get("has_more") and (type(next_offset) is not int or not offset < next_offset <= 100000 or not rows):
            raise ValueError("Invalid next page")
        total = body.get("total", len(rows))
        if type(total) is not int or not 0 <= total <= 2147483647:
            raise ValueError("Invalid total")
        return {"tracks": rows, "total": total, "next_offset": next_offset}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(MAX_REQUEST + 1)
        request = json.loads(raw) if len(raw) <= MAX_REQUEST else None
        if not isinstance(request, dict) or request.get("action") not in ACTIONS:
            raise ValueError("Invalid request")
        # Dependencies must not accidentally contaminate the JSON protocol.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = AgentAdapter(args.source, args.data).request(request["action"], request)
        response = {"ok": True, "result": result}
    except Exception:
        # No exception text, URLs, headers, tokens, server bodies or traceback.
        response = {"ok": False, "error": "Не удалось выполнить действие. Проверьте папки, привязку, агент и сеть."}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
