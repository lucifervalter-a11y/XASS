from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas import HeartbeatPayload
from app.services.agent_workspace import normalize_remote_location
from app.rate_limit import SlidingWindowLimiter


class SecurityGuardTests(unittest.TestCase):
    def test_heartbeat_source_name_rejects_overlong_and_empty(self):
        with self.assertRaises(ValidationError):
            HeartbeatPayload(source_name="")
        with self.assertRaises(ValidationError):
            HeartbeatPayload(source_name="x" * 129)
        payload = HeartbeatPayload(source_name="pc'; DROP TABLE heartbeat_sources;--")
        self.assertEqual(payload.source_name, "pc'; DROP TABLE heartbeat_sources;--")
        self.assertLessEqual(len(payload.source_name), 128)

    def test_workspace_blocks_path_traversal(self):
        for value in ("../etc/passwd", "ok/../../secret", "/etc/passwd", "C:\\Windows\\win.ini"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_remote_location("documents", value)

    def test_heartbeat_limiter_caps_one_request_per_window(self):
        limiter = SlidingWindowLimiter(max_requests=1, window_seconds=30)
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("5.6.7.8"))


if __name__ == "__main__":
    unittest.main()
