"""Testy historie dat: úložiště SQLite, intervaly topení, sběrač a CSV export."""

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from history import collector as collector_mod  # noqa: E402
from history.collector import HistoryCollector, parse_lg_push  # noqa: E402
from history.export import build_csv  # noqa: E402
from history.intervals import heating_intervals  # noqa: E402
from history.store import HistoryStore, Measurement  # noqa: E402


def _m(ts: str, device: str, value: float, source: str = "poer",
       metric: str = "heating") -> Measurement:
    """Zkratka pro měření v testech."""
    return Measurement(ts=ts, source=source, device=device, metric=metric, value=value)


class _TempStoreCase(unittest.TestCase):
    """Základ testů s dočasnou databází."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = HistoryStore(Path(self._tmp.name) / "history.db")


class HistoryStoreTests(_TempStoreCase):
    def test_measurements_roundtrip_in_range(self) -> None:
        self.store.add_measurements([
            _m("2026-10-01T10:00:00+00:00", "p1", 1),
            Measurement("2026-10-01T10:05:00+00:00", "poer", "p1", "mode", None, "heat"),
            _m("2026-10-02T10:00:00+00:00", "p1", 0),
        ])
        rows = self.store.measurements("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")
        self.assertEqual([(r.metric, r.value, r.text) for r in rows],
                         [("heating", 1.0, None), ("mode", None, "heat")])

    def test_forecast_snapshot_is_idempotent(self) -> None:
        hourly = [{"time_utc": "2026-10-01T12:00:00+00:00", "temp_c": 12.5,
                   "cloudiness_pct": 80.0, "precip_mm_h": 0.4, "humidity_pct": 70.0,
                   "wind_ms": 3.0}]
        self.store.add_forecast("2026-10-01T09:00:00+00:00", hourly)
        self.store.add_forecast("2026-10-01T09:00:00+00:00", hourly)
        rows = self.store.forecast_rows("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["precip_mm_h"], 0.4)

    def test_energy_upsert(self) -> None:
        self.store.upsert_energy_daily("ac1", "2026-10-01", 500)
        self.store.upsert_energy_daily("ac1", "2026-10-01", 508)
        self.assertEqual(self.store.energy_rows("2026-10-01", "2026-10-01"),
                         [{"device": "ac1", "day": "2026-10-01", "wh": 508.0}])

    def test_purge_removes_old_data(self) -> None:
        self.store.add_measurements([_m("2026-06-01T10:00:00+00:00", "p1", 1),
                                     _m("2026-10-01T10:00:00+00:00", "p1", 1)])
        self.store.add_forecast("2026-06-01T09:00:00+00:00",
                                [{"time_utc": "2026-06-01T12:00:00+00:00"}])
        self.store.upsert_energy_daily("ac1", "2026-06-01", 100)
        self.store.purge_before("2026-07-01T00:00:00+00:00")
        everything = ("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
        self.assertEqual(len(self.store.measurements(*everything)), 1)
        self.assertEqual(self.store.forecast_rows(*everything), [])
        self.assertEqual(self.store.energy_rows("2000-01-01", "2100-01-01"), [])


class HeatingIntervalTests(unittest.TestCase):
    END = "2026-10-01T12:00:00+00:00"

    def test_simple_interval_with_energy(self) -> None:
        rows = [_m("2026-10-01T10:00:00+00:00", "p1", 0),
                _m("2026-10-01T10:05:00+00:00", "p1", 1),
                _m("2026-10-01T10:10:00+00:00", "p1", 1),
                _m("2026-10-01T10:35:00+00:00", "p1", 0)]
        # Mezera 25 min je pod limitem 30 min, takže jde o souvislý interval.
        result = heating_intervals(rows, self.END, {"poer": 1800}, {"p1": 3.5})
        self.assertEqual(len(result), 1)
        interval = result[0]
        self.assertEqual((interval.start_ts, interval.end_ts), (rows[1].ts, rows[3].ts))
        self.assertEqual(interval.minutes, 30.0)
        self.assertAlmostEqual(interval.est_kwh, 1.75)
        self.assertFalse(interval.open)

    def test_gap_in_data_closes_interval_at_last_sample(self) -> None:
        rows = [_m("2026-10-01T10:00:00+00:00", "p1", 1),
                _m("2026-10-01T10:05:00+00:00", "p1", 1),
                _m("2026-10-01T11:00:00+00:00", "p1", 1),
                _m("2026-10-01T11:05:00+00:00", "p1", 0)]
        result = heating_intervals(rows, self.END, {"poer": 900}, {})
        self.assertEqual([(i.start_ts, i.end_ts) for i in result],
                         [(rows[0].ts, rows[1].ts), (rows[2].ts, rows[3].ts)])
        self.assertIsNone(result[0].est_kwh)

    def test_running_interval_ends_at_export_end(self) -> None:
        rows = [_m("2026-10-01T11:50:00+00:00", "p1", 1)]
        result = heating_intervals(rows, self.END, {"poer": 900}, {})
        self.assertEqual(result[0].end_ts, self.END)
        self.assertTrue(result[0].open)

    def test_event_source_without_gap_limit(self) -> None:
        rows = [_m("2026-10-01T06:00:00+00:00", "ac1", 1, source="lg"),
                _m("2026-10-01T09:00:00+00:00", "ac1", 0, source="lg")]
        result = heating_intervals(rows, self.END, {"poer": 900}, {})
        self.assertEqual(result[0].minutes, 180.0)

    def test_unknown_state_closes_interval(self) -> None:
        rows = [_m("2026-10-01T06:00:00+00:00", "ac1", 1, source="lg"),
                Measurement("2026-10-01T07:00:00+00:00", "lg", "ac1", "heating", None)]
        result = heating_intervals(rows, self.END, {}, {})
        self.assertEqual((result[0].end_ts, result[0].open), (rows[1].ts, False))

    def test_devices_are_separate(self) -> None:
        rows = [_m("2026-10-01T10:00:00+00:00", "p1", 1),
                _m("2026-10-01T10:00:00+00:00", "p2", 0),
                _m("2026-10-01T10:05:00+00:00", "p1", 0)]
        result = heating_intervals(rows, self.END, {"poer": 900}, {})
        self.assertEqual([i.device for i in result], ["p1"])


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = HistoryStore(Path(self._tmp.name) / "history.db")
        self.collector = HistoryCollector(self.store, poer_api_key="eutoken",
                                          clock=lambda: self.NOW)

    def _all(self) -> list[Measurement]:
        return self.store.measurements("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")

    async def test_poll_poer_records_all_thermostats(self) -> None:
        statuses = [
            {"device_id": "p1", "online": True, "current_temperature_c": 21.8,
             "current_humidity_pct": 49.0, "target_temperature_c": 20.0, "mode": "auto",
             "action": "heating", "error_text": None},
            {"device_id": "p2", "online": False, "current_temperature_c": None,
             "current_humidity_pct": None, "target_temperature_c": None, "mode": None,
             "action": None, "error_text": "offline"},
        ]
        with patch.object(collector_mod, "fetch_poer_statuses_cached",
                          AsyncMock(return_value=statuses)):
            await self.collector.poll_poer()
        rows = {(r.device, r.metric): (r.value, r.text) for r in self._all()}
        self.assertEqual(rows[("p1", "temperature_c")], (21.8, None))
        self.assertEqual(rows[("p1", "heating")], (1.0, None))
        self.assertEqual(rows[("p1", "mode")], (None, "auto"))
        self.assertEqual(rows[("p2", "online")], (0.0, None))
        self.assertNotIn(("p2", "temperature_c"), rows)

    async def test_poll_poer_cloud_error_writes_nothing(self) -> None:
        from poer_api import PoerApiError
        with patch.object(collector_mod, "fetch_poer_statuses_cached",
                          AsyncMock(side_effect=PoerApiError("výpadek"))):
            await self.collector.poll_poer()
        self.assertEqual(self._all(), [])

    async def test_lg_partial_push_merges_state_for_heating(self) -> None:
        await self.collector.record_lg_status("ac1", {
            "operation": {"airConOperationMode": "POWER_OFF"},
            "airConJobMode": {"currentJobMode": "HEAT"},
            "temperature": {"currentTemperature": 22.0, "targetTemperature": 23.0},
        })
        await self.collector.record_lg_status("ac1", {
            "operation": {"airConOperationMode": "POWER_ON"},
        })
        heating = [r.value for r in self._all() if r.metric == "heating"]
        self.assertEqual(heating, [0.0, 1.0])

    async def test_lg_push_without_power_or_mode_does_not_record_heating(self) -> None:
        await self.collector.record_lg_status("ac1", {"temperature": {"currentTemperature": 22}})
        self.assertEqual([r.metric for r in self._all()], ["temperature_c"])

    async def test_lg_malformed_push_is_ignored(self) -> None:
        await self.collector.record_lg_status("ac1", {"temperature": "x", "operation": []})
        self.assertEqual(self._all(), [])

    async def test_initial_status_skipped_when_push_already_arrived(self) -> None:
        await self.collector.record_lg_status("ac1", {"operation": {"airConOperationMode":
                                                                    "POWER_ON"}})
        api = AsyncMock()
        api.get_device_status.return_value = {"operation": {"airConOperationMode": "POWER_OFF"}}
        await self.collector.record_initial_lg_status(api, ["ac1"])
        self.assertEqual([r.value for r in self._all() if r.metric == "power_on"], [1.0])

    async def test_initial_status_recorded(self) -> None:
        api = AsyncMock()
        api.get_device_status.return_value = {"operation": {"airConOperationMode": "POWER_OFF"}}
        await self.collector.record_initial_lg_status(api, ["ac1"])
        self.assertEqual([r.value for r in self._all() if r.metric == "heating"], [0.0])

    async def test_mark_stopped_writes_lg_boundary(self) -> None:
        await self.collector.record_lg_status("ac1", {"operation": {"airConOperationMode":
                                                                    "POWER_ON"}})
        await self.collector.mark_stopped()
        heating = [r.value for r in self._all() if r.metric == "heating"]
        self.assertEqual(heating, [0.0, None])

    async def test_weather_records_current_and_forecast(self) -> None:
        weather = {
            "outdoor_current_temperature_c": 12.0,
            "outdoor_current_temperature_source": "forecast",
            "current": {"cloudiness_pct": 90.0, "precip_mm_h": 1.2, "humidity_pct": 80.0,
                        "wind_ms": 4.0},
            "hourly": [{"time_utc": "2026-10-01T11:00:00+00:00", "temp_c": 12.5}],
        }
        await self.collector.record_weather(weather)
        rows = {r.metric: (r.value, r.text) for r in self._all()}
        self.assertEqual(rows["temperature_c"], (12.0, "forecast"))
        self.assertEqual(rows["precip_mm_h"], (1.2, None))
        forecast = self.store.forecast_rows("2000-01-01T00:00:00+00:00",
                                            "2100-01-01T00:00:00+00:00")
        self.assertEqual(forecast[0]["target_time"], "2026-10-01T11:00:00+00:00")

    async def test_daily_maintenance_stores_energy_once_per_day(self) -> None:
        api = AsyncMock()
        api.get_energy_usage.return_value = [{"usedDate": "20260929", "energyUsage": 400},
                                             {"usedDate": "20260930", "energyUsage": 508}]
        await self.collector.daily_maintenance(api, ["ac1"])
        await self.collector.daily_maintenance(api, ["ac1"])
        # Jedno volání na 7 dní zpět – dopočítá i dny, kdy LG součet ještě neměl.
        api.get_energy_usage.assert_awaited_once_with("ac1", "DAILY", "20260924", "20260930")
        self.assertEqual([r["wh"] for r in self.store.energy_rows("2026-09-29", "2026-09-30")],
                         [400.0, 508.0])

    async def test_energy_not_refetched_after_restart(self) -> None:
        self.store.upsert_energy_daily("ac1", "2026-09-30", 508)
        restarted = HistoryCollector(self.store, poer_api_key=None, clock=lambda: self.NOW)
        api = AsyncMock()
        await restarted.daily_maintenance(api, ["ac1"])
        api.get_energy_usage.assert_not_awaited()

    async def test_energy_not_fetched_before_morning(self) -> None:
        early = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
        collector = HistoryCollector(self.store, poer_api_key=None, clock=lambda: early)
        api = AsyncMock()
        await collector.daily_maintenance(api, ["ac1"])
        api.get_energy_usage.assert_not_awaited()

    async def test_energy_error_does_not_break_maintenance(self) -> None:
        api = AsyncMock()
        api.get_energy_usage.side_effect = RuntimeError("LG nedostupné")
        await self.collector.daily_maintenance(api, ["ac1"])
        self.assertEqual(self.store.energy_rows("2000-01-01", "2100-01-01"), [])


class LgPushParsingTests(unittest.TestCase):
    KNOWN = {"ef279add"}

    def test_thinq_connect_report_shape(self) -> None:
        data = {"pushType": "DEVICE_STATUS", "deviceId": "ef279add",
                "report": {"operation": {"airConOperationMode": "POWER_ON"}}}
        self.assertEqual(parse_lg_push("app/clients/x/push", data, self.KNOWN),
                         ("ef279add", {"operation": {"airConOperationMode": "POWER_ON"}}))

    def test_event_push_shape_with_device_in_topic(self) -> None:
        data = {"event": {"push": {"temperature": {"currentTemperature": 22}}}}
        self.assertEqual(parse_lg_push("x/ef279add/push", data, self.KNOWN),
                         ("ef279add", {"temperature": {"currentTemperature": 22}}))

    def test_unknown_device_is_ignored(self) -> None:
        self.assertEqual(parse_lg_push("app/clients/x/push", {"deviceId": "cizi", "report": {}},
                                       self.KNOWN), (None, None))

    def test_non_status_push_is_ignored(self) -> None:
        data = {"pushType": "DEVICE_PUSH", "deviceId": "ef279add", "pushCode": "X"}
        self.assertEqual(parse_lg_push("t", data, self.KNOWN), (None, None))


class ExportTests(_TempStoreCase):
    def test_intervals_csv(self) -> None:
        self.store.add_measurements([_m("2026-10-01T10:00:00+00:00", "p1", 1),
                                     _m("2026-10-01T10:30:00+00:00", "p1", 0)])
        text = build_csv(self.store, "intervals", date(2026, 10, 1), date(2026, 10, 1),
                         power_kw={"p1": 3.5}, poll_seconds=900)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "zdroj;zarizeni;od;do;minuty;odhad_kwh;probiha")
        self.assertEqual(lines[1], "poer;p1;2026-10-01T10:00:00+00:00;"
                                   "2026-10-01T10:30:00+00:00;30.0;1.75;ne")

    def test_lg_interval_started_before_window_is_included(self) -> None:
        self.store.add_measurements([
            _m("2026-09-30T20:00:00+00:00", "ac1", 1, source="lg"),
            _m("2026-10-01T02:00:00+00:00", "ac1", 0, source="lg"),
        ])
        text = build_csv(self.store, "intervals", date(2026, 10, 1), date(2026, 10, 1),
                         power_kw={}, poll_seconds=300)
        start_ts = datetime(2026, 10, 1).astimezone().astimezone(timezone.utc)
        row = text.strip().splitlines()[1].split(";")
        self.assertEqual(row[2], start_ts.isoformat(timespec="seconds"))
        self.assertEqual(row[3], "2026-10-01T02:00:00+00:00")

    def test_interval_cut_by_past_window_is_not_running(self) -> None:
        # Den v minulosti – interval uřízne konec okna, ne současnost.
        self.store.add_measurements([_m("2026-09-01T18:00:00+00:00", "ac1", 1, source="lg")])
        text = build_csv(self.store, "intervals", date(2026, 9, 1), date(2026, 9, 1),
                         power_kw={}, poll_seconds=300)
        self.assertTrue(text.strip().splitlines()[1].endswith(";ne"))

    def test_measurements_csv_header(self) -> None:
        text = build_csv(self.store, "measurements", date(2026, 10, 1), date(2026, 10, 1),
                         power_kw={}, poll_seconds=300)
        self.assertEqual(text.strip(), "cas;zdroj;zarizeni;velicina;hodnota;text")


if __name__ == "__main__":
    unittest.main()
