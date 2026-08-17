from __future__ import annotations

import unittest
from types import SimpleNamespace

from app import scheduler


class SchedulerAlertTests(unittest.TestCase):
    def tearDown(self) -> None:
        scheduler._alert_state.clear()

    def test_low_disk_and_archive_error_are_deduplicated(self) -> None:
        source = SimpleNamespace(
            source_name="Home PC",
            last_payload={
                "metrics": {"disk_used_percent": 96},
                "archive_status": {"enabled": True, "free_bytes": 1024**3, "last_error": "media retry"},
            },
        )
        alerts = scheduler.source_health_alerts(source)
        first = scheduler._new_alerts(alerts, 1000)
        second = scheduler._new_alerts(alerts, 1001)
        self.assertEqual(len(first), 3)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
