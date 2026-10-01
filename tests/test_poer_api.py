"""Testy POER klienta pro více termostatů na zaznamenaných odpovědích cloudu."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import poer_api  # noqa: E402

KEY = "eutoken"

# Zaznamenáno 2026-09-30 (zkráceno na použitá pole).
SYNC = {"payload": {"devices": [
    {"id": "fee89300f2a5", "name": {"name": "Kuchobyvak"},
     "attributes": {"thermostatTemperatureRange": {"maxThresholdCelsius": 32,
                                                   "minThresholdCelsius": 5}}},
    {"id": "fee89300fac5", "name": {"name": "Koupelna"},
     "attributes": {"thermostatTemperatureRange": {"maxThresholdCelsius": 32,
                                                   "minThresholdCelsius": 5}}},
]}}
QUERY = {"payload": {"devices": {
    "fee89300f2a5": {"online": True, "status": "SUCCESS", "thermostatAction": "idle",
                     "thermostatHumidityAmbient": 49, "thermostatMode": "auto",
                     "thermostatTemperatureAmbient": 21.8, "thermostatTemperatureSetpoint": 20},
    "fee89300fac5": {"online": True, "status": "SUCCESS", "thermostatAction": "heating",
                     "thermostatHumidityAmbient": 54, "thermostatMode": "heat",
                     "thermostatTemperatureAmbient": 20.6, "thermostatTemperatureSetpoint": 22},
}}}
EXECUTE_OK = {"payload": {"commands": [{"ids": ["fee89300fac5"], "status": "SUCCESS"}]}}
EXECUTE_ERR = {"payload": {"commands": [{"ids": ["fee89300fac5"], "status": "ERROR"}]}}


class PoerClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        poer_api._devices_cache.clear()
        poer_api._status_cache.clear()

    async def test_statuses_for_all_devices(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            statuses = await poer_api.fetch_poer_statuses(KEY)
        self.assertEqual([s["device_id"] for s in statuses], ["fee89300f2a5", "fee89300fac5"])
        bathroom = statuses[1]
        self.assertEqual(bathroom["name"], "Koupelna")
        self.assertEqual(bathroom["current_temperature_c"], 20.6)
        self.assertEqual(bathroom["target_temperature_c"], 22.0)
        self.assertEqual(bathroom["mode"], "heat")
        self.assertTrue(bathroom["online"])
        self.assertIsNone(bathroom["error_text"])
        self.assertEqual(bathroom["min_temp_c"], 5.0)

    async def test_device_list_is_cached(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY, QUERY])
        with patch.object(poer_api, "_post_ha", post):
            await poer_api.fetch_poer_statuses(KEY)
            await poer_api.fetch_poer_statuses(KEY)
        # SYNC jen jednou, QUERY dvakrát
        self.assertEqual(post.await_count, 3)

    async def test_status_picks_preferred_device(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            status = await poer_api.fetch_poer_status(KEY, "fee89300fac5")
        self.assertEqual(status["device_id"], "fee89300fac5")

    async def test_status_falls_back_to_first_device(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            status = await poer_api.fetch_poer_status(KEY, "neznamy")
        self.assertEqual(status["device_id"], "fee89300f2a5")

    async def test_status_error_is_returned_as_dict(self) -> None:
        err = poer_api.PoerApiError("POER SYNC selhal: 500")
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=err)):
            status = await poer_api.fetch_poer_status(KEY)
        self.assertIsNone(status["device_id"])
        self.assertEqual(status["error_text"], "POER SYNC selhal: 500")

    async def test_offline_device_has_error_text(self) -> None:
        query = {"payload": {"devices": {"fee89300f2a5": {"online": False},
                                         "fee89300fac5": {"online": False}}}}
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, query])):
            status = await poer_api.fetch_poer_status(KEY, "fee89300fac5")
        self.assertFalse(status["online"])
        self.assertEqual(status["error_text"], "POER termostat Koupelna je offline.")

    async def test_invalid_key_is_error(self) -> None:
        status = await poer_api.fetch_poer_status("xx")
        self.assertIn("Neplatny POER API key", status["error_text"])

    async def test_command_with_device_id_makes_single_call(self) -> None:
        post = AsyncMock(return_value=EXECUTE_OK)
        with patch.object(poer_api, "_post_ha", post):
            result = await poer_api.send_poer_command(
                KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5"
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["device_id"], "fee89300fac5")
        post.assert_awaited_once()
        payload = post.await_args.args[1]
        self.assertEqual(payload["inputs"][0]["intent"], "action.devices.EXECUTE")
        command = payload["inputs"][0]["payload"]["commands"][0]
        self.assertEqual(command["devices"], [{"id": "fee89300fac5"}])
        self.assertEqual(command["execution"][0]["params"],
                         {"thermostatTemperatureSetpoint": 21.0})

    async def test_command_without_device_id_resolves_first(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY, EXECUTE_OK])
        with patch.object(poer_api, "_post_ha", post):
            result = await poer_api.send_poer_command(KEY, "set_mode", {"mode": "heat"})
        self.assertTrue(result["success"])
        self.assertEqual(result["device_id"], "fee89300f2a5")

    async def test_execute_error_status_is_failure(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(return_value=EXECUTE_ERR)):
            result = await poer_api.send_poer_command(
                KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5"
            )
        self.assertFalse(result["success"])
        self.assertIn("ERROR", result["error_text"])

    async def test_command_clears_all_status_caches(self) -> None:
        poer_api._status_cache[f"{KEY}:*"] = (0.0, [])
        poer_api._status_cache[f"{KEY}:fee89300f2a5"] = (0.0, {})
        with patch.object(poer_api, "_post_ha", AsyncMock(return_value=EXECUTE_OK)):
            await poer_api.send_poer_command(KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5")
        self.assertEqual(poer_api._status_cache, {})

    async def test_statuses_cached(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY])
        with patch.object(poer_api, "_post_ha", post):
            first = await poer_api.fetch_poer_statuses_cached(KEY)
            second = await poer_api.fetch_poer_statuses_cached(KEY)
        self.assertEqual(first, second)
        self.assertEqual(post.await_count, 2)

    async def test_single_status_shares_cache_with_all_statuses(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY])
        with patch.object(poer_api, "_post_ha", post):
            await poer_api.fetch_poer_statuses_cached(KEY)
            kitchen = await poer_api.fetch_poer_status_cached(KEY, "fee89300f2a5")
            default = await poer_api.fetch_poer_status_cached(KEY)
        self.assertEqual(kitchen["device_id"], "fee89300f2a5")
        self.assertEqual(default["device_id"], "fee89300f2a5")
        self.assertEqual(post.await_count, 2)

    async def test_offline_device_status_is_cached(self) -> None:
        query = {"payload": {"devices": {"fee89300f2a5": {"online": False},
                                         "fee89300fac5": {"online": False}}}}
        post = AsyncMock(side_effect=[SYNC, query])
        with patch.object(poer_api, "_post_ha", post):
            first = await poer_api.fetch_poer_status_cached(KEY, "fee89300fac5")
            second = await poer_api.fetch_poer_status_cached(KEY, "fee89300fac5")
        self.assertFalse(first["online"])
        self.assertEqual(first, second)
        self.assertEqual(post.await_count, 2)

    async def test_cached_status_retries_once_and_returns_error_dict(self) -> None:
        err = poer_api.PoerApiError("POER SYNC selhal: 500")
        post = AsyncMock(side_effect=err)
        with patch.object(poer_api, "_post_ha", post), \
             patch.object(poer_api, "_RETRY_DELAY_S", 0):
            status = await poer_api.fetch_poer_status_cached(KEY)
        self.assertEqual(status["error_text"], "POER SYNC selhal: 500")
        self.assertEqual(post.await_count, 2)


class _RaisingSession:
    """Falešná aiohttp session, jejíž post() vyhodí zadanou výjimku."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def post(self, *args, **kwargs):
        raise self.exc


class PoerTransportErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_poer_api_error(self) -> None:
        with self.assertRaises(poer_api.PoerApiError):
            await poer_api._post_ha(
                KEY, {"inputs": [{"intent": "action.devices.SYNC"}]},
                _RaisingSession(asyncio.TimeoutError()),
            )

    async def test_command_timeout_returns_failure_dict(self) -> None:
        result = await poer_api.send_poer_command(
            KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5",
            session=_RaisingSession(asyncio.TimeoutError()),
        )
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
