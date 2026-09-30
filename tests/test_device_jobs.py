"""Testy úloh pro arbitra (device_jobs.py) s falešným LG API a POER klientem."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import device_jobs  # noqa: E402
from device_jobs import (  # noqa: E402
    lg_apply_action_job,
    lg_command_job,
    lg_device_key,
    poer_command_job,
    poer_device_key,
)


def _lg_status(power: str = "POWER_ON", mode: str = "COOL") -> dict:
    """Minimální stav LG klimatizace ve formátu ThinQ API."""
    return {
        "operation": {"airConOperationMode": power},
        "airConJobMode": {"currentJobMode": mode},
        "temperature": {"targetTemperature": 24.0},
        "airFlow": {"windStrength": "AUTO"},
    }


class FakeThinQAPI:
    """Falešné ThinQ API: vrací pevný stav a zaznamenává odeslané payloady."""

    def __init__(self, status: dict) -> None:
        self.status = status
        self.sent: list[dict] = []
        self.status_reads = 0

    async def get_device_status(self, device_id: str) -> dict:
        self.status_reads += 1
        return copy.deepcopy(self.status)

    async def send_device_command(self, device_id: str, payload: dict) -> dict:
        self.sent.append(payload)
        return {"ok": True}


class DeviceKeyTests(unittest.TestCase):
    def test_keys(self) -> None:
        self.assertEqual(lg_device_key("abc"), "lg:abc")
        self.assertEqual(poer_device_key("p1"), "poer:p1")
        self.assertEqual(poer_device_key(None), "poer:default")


class LgJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_job_reads_status_at_run_time(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_OFF"))
        job = lg_command_job(api, "dev", "power_on", ())
        self.assertEqual(api.status_reads, 0)
        outcome = await job()
        self.assertEqual(api.status_reads, 1)
        self.assertTrue(outcome.sent)
        self.assertEqual([s["step"] for s in outcome.steps], ["power_on"])
        self.assertEqual(len(api.sent), 1)

    async def test_command_job_skips_noop(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_OFF"))
        outcome = await lg_command_job(api, "dev", "power_off", ())()
        self.assertFalse(outcome.sent)
        self.assertEqual(outcome.skip_reason, "Zařízení je již vypnuté.")
        self.assertEqual(api.sent, [])

    @patch("command_policy.get_temp_limits", return_value=None)
    @patch("command_policy._apply_setpoint_correction", side_effect=lambda t: t)
    async def test_apply_action_runs_mode_then_temperature(self, *_mocks) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_ON", mode="COOL"))
        with patch.object(device_jobs, "_SETTLE_SECONDS", 0):
            outcome = await lg_apply_action_job(
                api, "dev", {"mode": "HEAT", "temperature": 22.0}
            )()
        self.assertTrue(outcome.sent)
        self.assertEqual(
            [s["step"] for s in outcome.steps], ["change_mode", "set_temperature"]
        )

    @patch("command_policy.get_temp_limits", return_value=None)
    @patch("command_policy._apply_setpoint_correction", side_effect=lambda t: t)
    async def test_apply_action_does_not_reread_after_last_step(self, *_mocks) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_ON", mode="COOL"))
        with patch.object(device_jobs, "_SETTLE_SECONDS", 0):
            await lg_apply_action_job(api, "dev", {"mode": "HEAT", "temperature": 22.0})()
        # 1× úvodní čtení + 1× po change_mode; po posledním kroku už nic (šetří limit LG API).
        self.assertEqual(api.status_reads, 2)

    async def test_apply_action_skips_when_already_on(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_ON"))
        with patch.object(device_jobs, "_SETTLE_SECONDS", 0):
            outcome = await lg_apply_action_job(api, "dev", {})()
        self.assertFalse(outcome.sent)
        self.assertEqual(api.sent, [])


class PoerJobTests(unittest.IsolatedAsyncioTestCase):
    def _status(self, **overrides) -> dict:
        status = {
            "target_temperature_c": 21.0,
            "mode": "heat",
            "preset": "home",
            "device_id": "p1",
            "error_text": None,
        }
        status.update(overrides)
        return status

    async def test_set_temp_skips_when_equal(self) -> None:
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command", AsyncMock()) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 21.0})()
        self.assertFalse(outcome.sent)
        send.assert_not_awaited()

    async def test_set_temp_sends_when_different(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 22.5})()
        self.assertTrue(outcome.sent)
        send.assert_awaited_once_with(
            api_key="key", endpoint="set_temp", data={"temperature": 22.5},
            preferred_device_id="p1",
        )

    async def test_set_mode_away_matches_eco_status(self) -> None:
        status = self._status(mode="heat", preset="away")
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=status)), \
             patch.object(device_jobs, "send_poer_command", AsyncMock()) as send:
            outcome = await poer_command_job(
                "key", "p1", "set_mode", {"mode": "auto", "preset": "away"}
            )()
        self.assertFalse(outcome.sent)
        send.assert_not_awaited()

    async def test_status_error_does_not_skip(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        status = self._status(target_temperature_c=None, error_text="POER sitova chyba")
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=status)), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)):
            outcome = await poer_command_job("key", None, "set_temp", {"temperature": 21.0})()
        self.assertTrue(outcome.sent)

    async def test_noop_check_ignores_status_cache(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        stale = self._status(target_temperature_c=22.0)
        fresh = self._status(target_temperature_c=21.0)
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=stale), create=True), \
             patch.object(device_jobs, "fetch_poer_status", AsyncMock(return_value=fresh)), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 22.0})()
        self.assertTrue(outcome.sent)
        send.assert_awaited_once()

    async def test_without_noop_check_always_sends(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        with patch.object(device_jobs, "fetch_poer_status", AsyncMock()) as fetch, \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            outcome = await poer_command_job(
                "key", "p1", "set_temp", {"temperature": 21.0}, check_noop=False
            )()
        self.assertTrue(outcome.sent)
        fetch.assert_not_awaited()
        send.assert_awaited_once()

    async def test_failed_send_raises(self) -> None:
        result = {"success": False, "device_id": "p1", "error_text": "POER command selhal: 500"}
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)):
            with self.assertRaisesRegex(RuntimeError, "500"):
                await poer_command_job("key", "p1", "set_temp", {"temperature": 25.0})()


if __name__ == "__main__":
    unittest.main()
