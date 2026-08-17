from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.scenarios_store import (
    all_scenarios,
    delete_scenario,
    find_scenario,
    upsert_scenario,
)


class ScenarioStoreTests(unittest.TestCase):
    def test_builtin_and_custom_scenarios_are_kept_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            custom = upsert_scenario(
                path,
                {"name": "Рабочий режим", "actions": ["quiet_on", "update_all", "invalid"]},
            )

            self.assertEqual(custom["id"], "рабочий-режим")
            self.assertEqual(custom["actions"], ["quiet_on", "update_all"])
            self.assertEqual(len(all_scenarios(path)), 8)
            self.assertEqual(find_scenario(path, custom["id"]), custom)

    def test_scenario_metadata_is_normalized_without_breaking_legacy_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            custom = upsert_scenario(
                path,
                {
                    "name": "Вечер",
                    "icon": "moon!",
                    "color": "#ABCDEF",
                    "devices": ["PC", "PC", "Laptop"],
                    "actions": ["away_on", "check_all"],
                    "delay_sec": 15,
                    "schedule": "23:10",
                    "enabled": False,
                },
            )
            self.assertEqual(custom["icon"], "moon")
            self.assertEqual(custom["color"], "#abcdef")
            self.assertEqual(custom["devices"], ["PC", "Laptop"])
            self.assertEqual(custom["steps"], [{"action": "away_on"}, {"action": "check_all"}])
            self.assertEqual(custom["schedule"], "23:10")
            self.assertFalse(custom["enabled"])

    def test_builtin_cannot_be_deleted_but_custom_can(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            custom = upsert_scenario(path, {"name": "Тест", "actions": ["away_on"]})

            self.assertFalse(delete_scenario(path, "away"))
            self.assertTrue(delete_scenario(path, custom["id"]))
            self.assertIsNone(find_scenario(path, custom["id"]))


if __name__ == "__main__":
    unittest.main()
