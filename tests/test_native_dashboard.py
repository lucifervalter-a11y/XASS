import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.native_dashboard_api import build_router
from app.services.weather_card import WeatherCard


class NativeWeatherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "profile.json"
        self.profile.write_text('{"weather_location_name":"Кострома","weather_latitude":57.77}', encoding="utf-8")

        async def owner(request: Request):
            if request.headers.get("x-test-owner") != "yes":
                raise HTTPException(401)
            return SimpleNamespace(user_id=42)

        app = FastAPI()
        app.include_router(build_router(SimpleNamespace(profile_json_path=str(self.profile)), owner))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid")
        self.addAsyncCleanup(self.client.aclose)

    async def test_weather_requires_owner_before_fetching(self):
        with patch("app.native_dashboard_api.build_weather_card", new_callable=AsyncMock) as fetch:
            response = await self.client.get("/api/mini/weather")
        self.assertEqual(response.status_code, 401)
        fetch.assert_not_awaited()

    async def test_native_response_uses_configured_city_and_preserves_units(self):
        card = WeatherCard("Кострома", 57.77, 40.92, "Europe/Moscow", "12.0°C", "10.0°C", "3.0 м/с", "70%", "Дождь", "16:00 MSK")
        with patch("app.native_dashboard_api.build_weather_card", new_callable=AsyncMock, return_value=card) as fetch:
            response = await self.client.get("/api/mini/weather", headers={"x-test-owner": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["weather"]["wind_speed"], "3.0 м/с")
        self.assertEqual(response.json()["weather"]["location_name"], "Кострома")
        self.assertEqual(fetch.await_args.args[0], "")
        self.assertEqual(fetch.await_args.args[1]["weather_latitude"], 57.77)

    async def test_provider_failure_is_not_successful_empty_weather(self):
        with patch("app.native_dashboard_api.build_weather_card", new_callable=AsyncMock, return_value=None):
            response = await self.client.get("/api/mini/weather", headers={"x-test-owner": "yes"})
        self.assertEqual(response.status_code, 503)


class NativeScenarioRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_pwa_bodyless_request_keeps_header_proof(self):
        from app import main

        app = FastAPI()
        app.add_api_route("/api/mini/scenarios/{scenario_id}/run", main.mini_scenario_run, methods=["POST"])
        app.dependency_overrides[main.require_mini_owner] = lambda: SimpleNamespace(user_id=42)
        app.dependency_overrides[main.get_session] = lambda: object()
        scenario = {"id": "night", "enabled": True, "actions": ["lock_all"], "devices": [], "delay_sec": 0}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            with (patch.object(main, "find_scenario", return_value=scenario),
                  patch.object(main, "_require_pwa_action_proof", new_callable=AsyncMock) as verify,
                  patch.object(main, "try_start_scenario", return_value=False)):
                response = await client.post("/api/mini/scenarios/night/run",
                    headers={"X-XASS-Action-Proof": "legacy-passkey-proof"})
        self.assertEqual(response.status_code, 409)  # Existing run remains protected.
        self.assertEqual(verify.await_args.kwargs["action_proof"], "legacy-passkey-proof")
        self.assertEqual(verify.await_args.kwargs["purpose"], "scenario:night")

    async def test_native_json_proof_is_checked_against_current_scenario_snapshot(self):
        from app import main

        app = FastAPI()
        app.add_api_route("/api/mini/scenarios/{scenario_id}/run", main.mini_scenario_run, methods=["POST"])
        app.dependency_overrides[main.require_mini_owner] = lambda: SimpleNamespace(user_id=42)
        app.dependency_overrides[main.get_session] = lambda: object()
        scenario = {"id": "night", "enabled": True, "actions": ["lock_all"], "devices": ["Studio"], "delay_sec": 3}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            with (patch.object(main, "find_scenario", return_value=scenario),
                  patch.object(main, "_require_pwa_action_proof", new_callable=AsyncMock) as verify,
                  patch.object(main, "try_start_scenario", return_value=False)):
                response = await client.post("/api/mini/scenarios/night/run", json={"action_proof": "xna_native-proof"})
        self.assertEqual(response.status_code, 409)  # Auth passed; existing run stays protected.
        self.assertEqual(verify.await_args.kwargs["action_proof"], "xna_native-proof")
        self.assertEqual(verify.await_args.kwargs["binding"], {"scenario_id": "night", "actions": ["lock_all"], "devices": ["Studio"], "delay_sec": 3})
