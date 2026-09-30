"""Testy webových rout pro ruční příkazy (control.py, poer.py) přes arbitra."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi import HTTPException  # noqa: E402

from command_arbiter import CommandOutcome, CommandSource, CommandSuperseded  # noqa: E402
from web.routes import control, poer  # noqa: E402


class FakeArbiter:
    """Falešný arbitr: zaznamená požadavek a vrátí/vyhodí předem danou hodnotu."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def _request(arbiter: FakeArbiter):
    """Falešný FastAPI Request s app.state.api a app.state.arbiter."""
    state = SimpleNamespace(api=object(), api_error=None, arbiter=arbiter)
    return SimpleNamespace(app=SimpleNamespace(state=state))


class ControlRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_command_goes_through_arbiter(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True, steps=[{"step": "power_on"}]))
        body = control.CommandRequest(command="power_on", args=[])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertFalse(response.skipped)
        self.assertEqual(response.steps, [{"step": "power_on"}])
        submitted = arbiter.requests[0]
        self.assertEqual(submitted.device_key, "lg:dev1")
        self.assertEqual(submitted.key, "power_on")
        self.assertEqual(submitted.source, CommandSource.MANUAL)

    async def test_noop_returns_skipped(self) -> None:
        outcome = CommandOutcome(sent=False, skip_reason="Zařízení je již zapnuté.")
        arbiter = FakeArbiter(result=outcome)
        body = control.CommandRequest(command="power_on", args=[])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertTrue(response.skipped)
        self.assertEqual(response.skip_reason, "Zařízení je již zapnuté.")

    async def test_superseded_manual_command_returns_skipped(self) -> None:
        arbiter = FakeArbiter(error=CommandSuperseded("Nahrazen novějším příkazem."))
        body = control.CommandRequest(command="set_temperature", args=[22])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertTrue(response.skipped)
        self.assertEqual(response.skip_reason, "Nahrazen novějším příkazem.")

    async def test_failure_returns_503(self) -> None:
        arbiter = FakeArbiter(error=RuntimeError("cloud nedostupný"))
        body = control.CommandRequest(command="power_on", args=[])
        with self.assertRaises(HTTPException) as ctx:
            await control.send_command("dev1", body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 503)


class PoerRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = patch.dict("os.environ", {"LG_POER_API_KEY": "eu-token"})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch.object(poer, "_load_weather_config", return_value={"poer_device_id": "p1"})
        cfg.start()
        self.addCleanup(cfg.stop)

    async def test_set_temperature_goes_through_arbiter(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerTemperatureRequest(temperature=21.5)
        result = await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(result, {"success": True, "skipped": False, "skip_reason": None})
        submitted = arbiter.requests[0]
        self.assertEqual(submitted.device_key, "poer:p1")
        self.assertEqual(submitted.key, "set_temp")
        self.assertEqual(submitted.source, CommandSource.MANUAL)

    async def test_manual_poer_command_skips_noop_check(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        await poer.set_poer_temperature(
            poer.PoerTemperatureRequest(temperature=21.5), _request(arbiter)
        )
        result = {"success": True, "device_id": "p1", "error_text": None}
        with patch("device_jobs.fetch_poer_status", AsyncMock()) as fetch, \
             patch("device_jobs.send_poer_command", AsyncMock(return_value=result)) as send:
            await arbiter.requests[0].run()
        fetch.assert_not_awaited()
        send.assert_awaited_once()

    async def test_set_mode_superseded_is_skipped(self) -> None:
        arbiter = FakeArbiter(error=CommandSuperseded("Nahrazen."))
        body = poer.PoerModeRequest(mode="heat", preset="home")
        result = await poer.set_poer_mode(body, _request(arbiter))
        self.assertEqual(result, {"success": True, "skipped": True, "skip_reason": "Nahrazen."})

    async def test_failure_returns_503(self) -> None:
        arbiter = FakeArbiter(error=RuntimeError("POER command selhal: 500"))
        body = poer.PoerTemperatureRequest(temperature=21.5)
        with self.assertRaises(HTTPException) as ctx:
            await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("500", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
