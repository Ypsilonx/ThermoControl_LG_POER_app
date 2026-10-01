"""Testy registru čidel zón: sběr hodnot ze zdrojů a výběr čerstvého zdroje pro roli."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zones import sensors as sensors_mod  # noqa: E402
from zones.config import parse_zones  # noqa: E402
from zones.sensors import (  # noqa: E402
    Reading,
    chmi_values,
    http_values,
    lg_values,
    poer_values,
    resolve_role,
)

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

CFG = parse_zones({
    "zones": {"kuchoobyvak": {
        "heaters": [{"device": "lg:*"}],
        "roles": {"indoor_temperature": ["poer_k", "lg"], "outdoor_temperature": ["vychod", "chmi"],
                  "fireplace": ["plug"]},
    }},
    "sensors": {
        "poer_k": {"source": "poer", "device_id": "p1"},
        "lg": {"source": "lg"},
        "chmi": {"source": "chmi", "max_age_min": 240},
        "vychod": {"source": "http", "url": "http://cidlo/vychod"},
        "plug": {"source": "smart_plug"},
    },
})
KITCHEN = CFG.zones["kuchoobyvak"]


def _r(sensor_id: str, value: float, age_min: float) -> Reading:
    """Hodnota stará ``age_min`` minut."""
    return Reading(value=value, ts=NOW - timedelta(minutes=age_min), sensor_id=sensor_id)


class ResolveRoleTests(unittest.TestCase):
    def test_first_fresh_source_wins(self) -> None:
        values = {("poer_k", "temperature"): _r("poer_k", 21.5, 3),
                  ("lg", "temperature"): _r("lg", 23.0, 1)}
        reading = resolve_role(KITCHEN, "indoor_temperature", CFG.sensors, values, NOW, 15)
        self.assertEqual(reading.sensor_id, "poer_k")

    def test_stale_source_falls_back_to_next(self) -> None:
        values = {("poer_k", "temperature"): _r("poer_k", 21.5, 20),
                  ("lg", "temperature"): _r("lg", 23.0, 1)}
        reading = resolve_role(KITCHEN, "indoor_temperature", CFG.sensors, values, NOW, 15)
        self.assertEqual(reading.sensor_id, "lg")

    def test_sensor_specific_max_age(self) -> None:
        values = {("chmi", "temperature"): _r("chmi", 8.0, 180)}
        reading = resolve_role(KITCHEN, "outdoor_temperature", CFG.sensors, values, NOW, 15)
        self.assertEqual(reading.value, 8.0)

    def test_all_stale_or_missing_is_none(self) -> None:
        values = {("poer_k", "temperature"): _r("poer_k", 21.5, 60)}
        self.assertIsNone(resolve_role(KITCHEN, "indoor_temperature", CFG.sensors, values, NOW, 15))
        self.assertIsNone(resolve_role(KITCHEN, "indoor_humidity", CFG.sensors, values, NOW, 15))


class SourceTests(unittest.TestCase):
    def test_poer_values_skip_offline(self) -> None:
        statuses = [
            {"device_id": "p1", "online": True, "current_temperature_c": 21.4,
             "current_humidity_pct": 50.0},
            {"device_id": "p2", "online": False, "current_temperature_c": None},
        ]
        values = poer_values(CFG.sensors, statuses, NOW)
        self.assertEqual(values[("poer_k", "temperature")].value, 21.4)
        self.assertEqual(values[("poer_k", "humidity")].value, 50.0)
        offline = poer_values(CFG.sensors, [{**statuses[0], "online": False}], NOW)
        self.assertEqual(offline, {})

    def test_lg_values_apply_proxy_offset(self) -> None:
        lg_status = {"dev": (NOW, {"temperature": {"currentTemperature": 24.0}})}
        values = lg_values(CFG.sensors, lg_status, proxy_offset_c=-2.0)
        self.assertEqual(values[("lg", "temperature")].value, 22.0)
        self.assertEqual(values[("lg", "temperature")].ts, NOW)

    def test_lg_without_temperature_has_no_value(self) -> None:
        self.assertEqual(lg_values(CFG.sensors, {"dev": (NOW, {})}, 0.0), {})

    def test_chmi_values_use_fetch_time(self) -> None:
        cache = {"current_temperature_c": 15.1, "fetched_at": "2026-10-01T06:52:36+00:00"}
        reading = chmi_values(CFG.sensors, cache)[("chmi", "temperature")]
        self.assertEqual(reading.value, 15.1)
        self.assertEqual(reading.ts, datetime(2026, 10, 1, 6, 52, 36, tzinfo=timezone.utc))
        self.assertEqual(chmi_values(CFG.sensors, None), {})


class HttpSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_sensor_reads_temperature(self) -> None:
        fetch = AsyncMock(return_value={"temperature_c": 7.5, "humidity_pct": 80})
        with patch.object(sensors_mod, "_fetch_json", fetch):
            values = await http_values(CFG.sensors, NOW)
        self.assertEqual(values[("vychod", "temperature")].value, 7.5)
        self.assertEqual(values[("vychod", "humidity")].value, 80.0)

    async def test_http_error_gives_no_value(self) -> None:
        with patch.object(sensors_mod, "_fetch_json", AsyncMock(side_effect=OSError("timeout"))):
            self.assertEqual(await http_values(CFG.sensors, NOW), {})


if __name__ == "__main__":
    unittest.main()
