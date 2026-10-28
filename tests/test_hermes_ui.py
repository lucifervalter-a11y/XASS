import unittest
from pathlib import Path


class HermesUiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent
        self.miniapp = (self.root / "miniapp.php").read_text(encoding="utf-8")
        self.hermes_js = (self.root / "assets" / "miniapp-hermes.js").read_text(encoding="utf-8")
        self.hermes_css = (self.root / "assets" / "miniapp-hermes.css").read_text(encoding="utf-8")

    def test_miniapp_has_hermes_view(self) -> None:
        self.assertIn('id="view-hermes"', self.miniapp)
        self.assertIn("Hermes Agent AI", self.miniapp)
        self.assertIn('id="hermesStatusPill"', self.miniapp)
        self.assertIn('id="hermesModelSelect"', self.miniapp)
        self.assertIn('id="hermesSessionSelect"', self.miniapp)
        self.assertIn('id="hermesNewSessionBtn"', self.miniapp)
        self.assertIn('id="hermesChatHistory"', self.miniapp)
        self.assertIn('id="hermesPromptInput"', self.miniapp)
        self.assertIn('id="hermesSendBtn"', self.miniapp)

    def test_miniapp_has_quick_action_and_tab(self) -> None:
        self.assertIn('id="quickHermes"', self.miniapp)
        self.assertIn('data-view="hermes"', self.miniapp)
        self.assertIn('Гермес AI', self.miniapp)
        self.assertIn('>Гермес</button>', self.miniapp)

    def test_miniapp_includes_hermes_assets(self) -> None:
        self.assertIn('/assets/miniapp-hermes.css?v=0170', self.miniapp)
        self.assertIn('/assets/miniapp-hermes.js?v=0170', self.miniapp)

    def test_hermes_client_script_functions(self) -> None:
        self.assertIn('loadHermesStatus', self.hermes_js)
        self.assertIn('sendHermesMessage', self.hermes_js)
        self.assertIn('hermes-thought', self.hermes_js)
        self.assertIn('Chain of Thought', self.hermes_js)
        self.assertIn('getDemoAgentResponse', self.hermes_js)
        self.assertIn('window.HermesAgent', self.hermes_js)

    def test_hermes_css_styles(self) -> None:
        self.assertIn('repeat(6, 1fr)', self.hermes_css)
        self.assertIn('.hermes-bubble-agent', self.hermes_css)
        self.assertIn('.hermes-thought', self.hermes_css)
        self.assertIn('.hermes-code-block', self.hermes_css)
        self.assertIn('.hermes-token-pill', self.hermes_css)


if __name__ == "__main__":
    unittest.main()
