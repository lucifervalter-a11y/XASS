from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SiteTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (ROOT / "site.php").read_text(encoding="utf-8")

    def test_public_copy_has_no_legacy_duplicate(self) -> None:
        self.assertNotIn("Занимаюсь", self.source)
        self.assertEqual(self.source.count("Делаю сложное <em>спокойным.</em>"), 1)

    def test_contacts_do_not_contain_weather(self) -> None:
        match = re.search(
            r'<section class="section" id="contacts"[\s\S]*?</section>',
            self.source,
        )
        self.assertIsNotNone(match)
        contacts = match.group(0)
        self.assertNotIn("Погода", contacts)
        self.assertNotIn("$weather", contacts)

    def test_quotes_and_interactions_are_wired(self) -> None:
        self.assertIn('id="quotes"', self.source)
        self.assertIn('id="quotePrev"', self.source)
        self.assertIn('id="quoteNext"', self.source)
        self.assertRegex(self.source, r"\}\)\(\);\s*</script>")

    def test_profile_entry_has_no_dead_legacy_markup(self) -> None:
        entry = (ROOT / "profile.php").read_text(encoding="utf-8")
        self.assertLess(len(entry), 200)
        self.assertIn("require __DIR__ . '/site.php';", entry)


if __name__ == "__main__":
    unittest.main()
