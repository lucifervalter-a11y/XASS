from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.rules_store import delete_rule, load_rules, save_rules, upsert_rule


class RulesStoreTests(unittest.TestCase):
    def test_round_trip_upsert_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            saved = save_rules(path, [{
                "name": "PC offline", "condition": "agent_offline", "threshold": 5,
                "duration_minutes": 1, "cooldown_minutes": 60, "priority": "warning",
            }])
            self.assertEqual(len(saved), 1)
            self.assertEqual(load_rules(path)[0]["condition"], "agent_offline")
            updated = upsert_rule(path, {**saved[0], "priority": "critical", "enabled": False})
            self.assertEqual(updated["priority"], "critical")
            self.assertFalse(load_rules(path)[0]["enabled"])
            self.assertTrue(delete_rule(path, updated["id"]))
            self.assertEqual(load_rules(path), [])

    def test_invalid_conditions_are_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            self.assertEqual(save_rules(path, [{"name": "unsafe", "condition": "run_command"}]), [])
            with self.assertRaises(ValueError):
                upsert_rule(path, {"name": "unsafe", "condition": "run_command"})


if __name__ == "__main__":
    unittest.main()
