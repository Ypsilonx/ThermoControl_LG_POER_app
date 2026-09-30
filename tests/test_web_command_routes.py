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
from poer_api import PoerApiError  # noqa: E402
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
        limits = {"min_temp_c": 5.0, "max_temp_c": 32.0}
        self.devices = [
            {"device_id": "p1", "name": "Kuchobyvak", **limits},
            {"device_id": "fee89300fac5", "name": "Koupelna", **limits},
        ]
        dev = patch.object(poer, "fetch_poer_devices", AsyncMock(return_value=self.devices))
        dev.start()
        self.addCleanup(dev.stop)

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
        self.assertEqual(send.await_count, 2)

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

    async def test_command_for_selected_device(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerTemperatureRequest(temperature=24.0, device_id="fee89300fac5")
        await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(arbiter.requests[0].device_key, "poer:fee89300fac5")

    async def test_unknown_device_returns_404(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerModeRequest(mode="heat", device_id="cizi")
        with self.assertRaises(HTTPException) as ctx:
            await poer.set_poer_mode(body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(arbiter.requests, [])

    async def test_status_for_selected_device(self) -> None:
        status = {"device_id": "fee89300fac5"}
        with patch.object(poer, "fetch_poer_status_cached",
                          AsyncMock(return_value=status)) as fetch:
            result = await poer.get_poer_status(device_id="fee89300fac5")
        self.assertEqual(result, status)
        self.assertEqual(fetch.await_args.kwargs["preferred_device_id"], "fee89300fac5")

    async def test_devices_lists_all_with_default_flag(self) -> None:
        statuses = [{"device_id": "p1"}, {"device_id": "fee89300fac5"}]
        with patch.object(poer, "fetch_poer_statuses_cached", AsyncMock(return_value=statuses)):
            result = await poer.get_poer_devices()
        self.assertEqual(result, [{"device_id": "p1", "is_default": True},
                                  {"device_id": "fee89300fac5", "is_default": False}])

    async def test_temperature_out_of_device_range_returns_422(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerTemperatureRequest(temperature=34.0, device_id="fee89300fac5")
        with self.assertRaises(HTTPException) as ctx:
            await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(arbiter.requests, [])

    async def test_missing_config_uses_first_device_key(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        with patch.object(poer, "_load_weather_config", return_value={"poer_device_id": None}):
            await poer.set_poer_temperature(
                poer.PoerTemperatureRequest(temperature=21.0), _request(arbiter)
            )
        self.assertEqual(arbiter.requests[0].device_key, "poer:p1")

    async def test_stale_config_id_falls_back_to_first_device(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        with patch.object(poer, "_load_weather_config", return_value={"poer_device_id": "stary"}):
            await poer.set_poer_temperature(
                poer.PoerTemperatureRequest(temperature=21.0), _request(arbiter)
            )
        self.assertEqual(arbiter.requests[0].device_key, "poer:p1")

    async def test_devices_cloud_error_returns_503(self) -> None:
        err = PoerApiError("POER SYNC selhal: 500")
        with patch.object(poer, "fetch_poer_statuses_cached", AsyncMock(side_effect=err)):
            with self.assertRaises(HTTPException) as ctx:
                await poer.get_poer_devices()
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
