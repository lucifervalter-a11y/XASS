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

    def test_projects_without_url_do_not_open_legacy_archive(self) -> None:
        self.assertNotIn("xass_text($project['url'] ?? '', '/projects.php')", self.source)
        self.assertIn("project-row-static", self.source)
        self.assertNotIn('class="project-row" href="/projects.php"', self.source)

    def test_production_project_defaults_do_not_inject_demo_content(self) -> None:
        from app.services.projects_store import DEFAULT_PROJECTS, normalize_projects

        self.assertEqual(DEFAULT_PROJECTS, [])
        self.assertEqual(normalize_projects([]), [])

    def test_site_accent_and_widget_visibility_come_from_existing_config(self) -> None:
        self.assertIn("SITE_CONFIG_JSON_PATH", self.source)
        self.assertIn("$accentColor", self.source)
        self.assertIn("$widgetVisible('weather')", self.source)
        self.assertIn("$widgetVisible('projects')", self.source)

    def test_site_config_normalizer_is_backward_compatible(self) -> None:
        from app.services.projects_store import SITE_WIDGETS, normalize_site_config

        legacy = normalize_site_config({"projects_background": {"type": "image", "src": "/cover.jpg"}})
        self.assertEqual(legacy["accent_color"], "#376dff")
        self.assertEqual(legacy["widgets"], list(SITE_WIDGETS))

        custom = normalize_site_config({"accent_color": "#ABCDEF", "widgets": ["music", "music", "invalid"]})
        self.assertEqual(custom["accent_color"], "#abcdef")
        self.assertEqual(custom["widgets"], ["music"])


if __name__ == "__main__":
    unittest.main()
