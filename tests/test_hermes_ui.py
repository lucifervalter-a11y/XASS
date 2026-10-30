import unittest
from pathlib import Path


class HermesUiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent
        self.miniapp = (self.root / "miniapp.php").read_text(encoding="utf-8")
        self.hermes_js = (self.root / "assets" / "miniapp-hermes.js").read_text(encoding="utf-8")
        self.hermes_css = (self.root / "assets" / "miniapp-hermes.css").read_text(encoding="utf-8")
        self.telegram_panel = (self.root / "app" / "services" / "panel.py").read_text(encoding="utf-8")

    def test_miniapp_has_hermes_view(self) -> None:
        self.assertIn('id="view-hermes"', self.miniapp)
        self.assertIn("<h1>XASS AI</h1>", self.miniapp)
        self.assertNotIn("Hermes Agent AI", self.miniapp)
        self.assertNotIn("Гермес", self.miniapp)
        self.assertIn('id="hermesStatusPill"', self.miniapp)
        self.assertIn('hermes-status-badge offline', self.miniapp)
        self.assertIn('id="hermesModelSelect"', self.miniapp)
        self.assertIn('id="hermesSessionSelect"', self.miniapp)
        self.assertIn('id="hermesNewSessionBtn"', self.miniapp)
        self.assertIn('id="hermesChatHistory"', self.miniapp)
        self.assertIn('id="hermesPromptInput"', self.miniapp)
        self.assertIn('id="hermesSendBtn"', self.miniapp)
        self.assertIn('Диалоги и выполнение действий недоступны', self.miniapp)
        self.assertNotIn('выполнение команд и скриптов в реальном времени', self.miniapp)

    def test_miniapp_has_quick_action_and_tab(self) -> None:
        self.assertIn('id="quickHermes"', self.miniapp)
        self.assertIn('data-view="hermes"', self.miniapp)
        self.assertIn('>XASS AI</button>', self.miniapp)

    def test_miniapp_includes_hermes_assets(self) -> None:
        self.assertIn('/assets/miniapp-hermes.css?v=0171', self.miniapp)
        self.assertIn('/assets/miniapp-hermes.js?v=0171', self.miniapp)

    def test_hermes_client_script_functions(self) -> None:
        self.assertIn('loadHermesStatus', self.hermes_js)
        self.assertIn('sendHermesMessage', self.hermes_js)
        self.assertIn("status: 'offline'", self.hermes_js)
        self.assertIn("statusData.online === true", self.hermes_js)
        self.assertIn("statusData.interactive_available === true", self.hermes_js)
        self.assertIn("interactiveAvailable: false", self.hermes_js)
        self.assertIn("state.interactiveAvailable === true", self.hermes_js)
        self.assertIn("headers['X-XASS-Action-Proof']", self.hermes_js)
        self.assertIn("X.passkeyAction('xass-ai:prompt'", self.hermes_js)
        self.assertIn("data?.ok !== true", self.hermes_js)
        self.assertNotIn('Chain of Thought', self.hermes_js)
        self.assertNotIn('getDemoAgentResponse', self.hermes_js)
        self.assertNotIn('DEFAULT_INITIAL_MESSAGES', self.hermes_js)
        self.assertIn("fetch(apiUrl('sessions')", self.hermes_js)
        self.assertNotIn("const newId = 'session-'", self.hermes_js)
        self.assertNotIn('Гермес', self.hermes_js)
        self.assertIn('window.HermesAgent', self.hermes_js)

    def test_hermes_css_styles(self) -> None:
        self.assertIn('repeat(6, 1fr)', self.hermes_css)
        self.assertIn('.hermes-bubble-agent', self.hermes_css)
        self.assertIn('.hermes-thought', self.hermes_css)
        self.assertIn('.hermes-code-block', self.hermes_css)
        self.assertIn('.hermes-token-pill', self.hermes_css)

    def test_telegram_panel_uses_xass_ai_branding(self) -> None:
        self.assertIn("XASS AI", self.telegram_panel)
        self.assertNotIn("Hermes Agent", self.telegram_panel)
        self.assertNotIn("задержка Hermes", self.telegram_panel)


if __name__ == "__main__":
    unittest.main()
