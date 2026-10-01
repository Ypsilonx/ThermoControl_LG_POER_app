"""Scénářové testy volby zdroje tepla a úprav cíle (zones/sources.py)."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zones.config import DEFAULT_DRYING, DEFAULT_SOURCES, DEFAULT_SUN, Heater  # noqa: E402
from zones.decide import ZoneDecision  # noqa: E402
from zones.sources import (  # noqa: E402
    DryingState,
    SourceContext,
    ac_insufficient,
    drying_adjustment,
    frost_expected,
    heater_setpoint,
    sun_adjustment,
)

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=CEST)
FOIL = Heater("poer", "pk", -1.0)
CABLE = Heater("poer", "pb", 0.0)


def _ctx(**changes) -> SourceContext:
    """Kontext: NT, AC v pořádku, venku 5 °C, bez mrazu."""
    values = dict(low_tariff=True, outdoor_c=5.0, frost_expected=False, ac_available=True,
                  ac_insufficient=False)
    values.update(changes)
    return SourceContext(**values)


def _decision(target: float = 21.0, **flags) -> ZoneDecision:
    """Rozhodnutí zóny s cílem ``target``."""
    return ZoneDecision("zona", target, "Automatika", **flags)


class HeaterSetpointTests(unittest.TestCase):
    def _foil(self, ctx: SourceContext, decision: ZoneDecision | None = None):
        return heater_setpoint(FOIL, True, decision or _decision(), ctx, 12.0, DEFAULT_SOURCES)

    def test_low_tariff_keeps_base(self) -> None:
        setpoint, reason = self._foil(_ctx())
        self.assertEqual(setpoint, 20.0)
        self.assertIn("NT", reason)

    def test_low_tariff_with_frost_heats_to_full_target(self) -> None:
        setpoint, reason = self._foil(_ctx(frost_expected=True))
        self.assertEqual(setpoint, 21.0)
        self.assertIn("mrazu", reason)

    def test_high_tariff_switches_foil_off(self) -> None:
        setpoint, reason = self._foil(_ctx(low_tariff=False))
        self.assertEqual(setpoint, 12.0)
        self.assertIn("VT", reason)

    def test_high_tariff_backup_reasons(self) -> None:
        cases = [
            (_ctx(low_tariff=False, ac_available=False), "nedostupná"),
            (_ctx(low_tariff=False, ac_insufficient=True), "nestačí"),
            (_ctx(low_tariff=False, outdoor_c=-8.0), "-8.0"),
        ]
        for ctx, text in cases:
            with self.subTest(text=text):
                setpoint, reason = self._foil(ctx)
                self.assertEqual(setpoint, 21.0)
                self.assertIn(text, reason)

    def test_unknown_outdoor_temperature_is_no_backup(self) -> None:
        setpoint, _ = self._foil(_ctx(low_tariff=False, outdoor_c=None))
        self.assertEqual(setpoint, 12.0)

    def test_emergency_heats_with_foil_even_in_high_tariff(self) -> None:
        setpoint, _ = self._foil(_ctx(low_tariff=False), _decision(12.0, emergency=True))
        self.assertEqual(setpoint, 12.0)
        setpoint, _ = self._foil(_ctx(low_tariff=False), _decision(15.0, emergency=True))
        self.assertEqual(setpoint, 15.0)

    def test_paused_zone_keeps_minimum(self) -> None:
        setpoint, _ = self._foil(_ctx(), _decision(12.0, paused=True))
        self.assertEqual(setpoint, 12.0)

    def test_zone_without_ac_heats_with_cable_always(self) -> None:
        setpoint, _ = heater_setpoint(CABLE, False, _decision(22.0), _ctx(low_tariff=False), 12.0,
                                      DEFAULT_SOURCES)
        self.assertEqual(setpoint, 22.0)


class FrostTests(unittest.TestCase):
    def _hourly(self, temps: list[float]) -> list[dict]:
        base = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
        return [{"time_utc": (base + timedelta(hours=i)).isoformat(), "temp_c": t}
                for i, t in enumerate(temps)]

    def test_frost_within_horizon(self) -> None:
        now = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(frost_expected(self._hourly([3, 1, -1]), now, 12, 0.0))
        self.assertFalse(frost_expected(self._hourly([3, 1, 0.5]), now, 12, 0.0))
        self.assertFalse(frost_expected(self._hourly([3] * 13 + [-5]), now, 12, 0.0))
        self.assertFalse(frost_expected([], now, 12, 0.0))


class SunTests(unittest.TestCase):
    SUNRISE = datetime(2026, 10, 5, 6, 50, tzinfo=CEST)
    SUNSET = datetime(2026, 10, 5, 18, 25, tzinfo=CEST)

    def _hourly(self, cloud: float) -> list[dict]:
        base = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        return [{"time_utc": (base + timedelta(hours=i)).isoformat(), "cloudiness_pct": cloud}
                for i in range(24)]

    def _adjust(self, side: str, hh: int, cloud: float):
        now = datetime(2026, 10, 5, hh, 0, tzinfo=CEST)
        return sun_adjustment(side, DEFAULT_SUN, now, self.SUNRISE, self.SUNSET,
                              self._hourly(cloud))

    def test_east_side_morning_clear_sky(self) -> None:
        delta, reason = self._adjust("east", 5, cloud=10)
        self.assertEqual(delta, -0.5)
        self.assertIn("slunce", reason)

    def test_east_side_outside_window_or_cloudy(self) -> None:
        self.assertEqual(self._adjust("east", 3, cloud=10)[0], 0.0)
        self.assertEqual(self._adjust("east", 13, cloud=10)[0], 0.0)
        self.assertEqual(self._adjust("east", 5, cloud=80)[0], 0.0)

    def test_west_side_afternoon(self) -> None:
        self.assertEqual(self._adjust("west", 14, cloud=20)[0], -0.5)
        self.assertEqual(self._adjust("west", 19, cloud=20)[0], 0.0)

    def test_no_side_or_disabled(self) -> None:
        self.assertEqual(self._adjust(None, 5, cloud=0)[0], 0.0)
        now = datetime(2026, 10, 5, 5, 0, tzinfo=CEST)
        disabled = {**DEFAULT_SUN, "enabled": False}
        self.assertEqual(sun_adjustment("east", disabled, now, self.SUNRISE, self.SUNSET,
                                        self._hourly(0))[0], 0.0)

    def test_no_forecast_means_no_adjustment(self) -> None:
        now = datetime(2026, 10, 5, 5, 0, tzinfo=CEST)
        self.assertEqual(sun_adjustment("east", DEFAULT_SUN, now, self.SUNRISE, self.SUNSET,
                                        [])[0], 0.0)


class DryingTests(unittest.TestCase):
    def test_boost_limited_in_time_and_rearmed_below_threshold(self) -> None:
        t0 = datetime(2026, 10, 5, 7, 0)
        state = DryingState()
        delta, reason, state = drying_adjustment(75.0, DEFAULT_DRYING, state, t0)
        self.assertEqual(delta, 1.0)
        self.assertIn("75", reason)
        delta, _, state = drying_adjustment(74.0, DEFAULT_DRYING, state, t0 + timedelta(minutes=30))
        self.assertEqual(delta, 1.0)
        # Po 60 min konec, i když vlhkost zůstává vysoko – a znovu se nezapne.
        delta, _, state = drying_adjustment(73.0, DEFAULT_DRYING, state, t0 + timedelta(minutes=61))
        self.assertEqual(delta, 0.0)
        delta, _, state = drying_adjustment(73.0, DEFAULT_DRYING, state, t0 + timedelta(minutes=90))
        self.assertEqual(delta, 0.0)
        # Pokles pod 65 % opět povolí vysoušení.
        _, _, state = drying_adjustment(60.0, DEFAULT_DRYING, state, t0 + timedelta(minutes=120))
        delta, _, state = drying_adjustment(72.0, DEFAULT_DRYING, state,
                                            t0 + timedelta(minutes=130))
        self.assertEqual(delta, 1.0)

    def test_drying_ends_when_humidity_drops(self) -> None:
        t0 = datetime(2026, 10, 5, 7, 0)
        _, _, state = drying_adjustment(75.0, DEFAULT_DRYING, DryingState(), t0)
        delta, _, state = drying_adjustment(64.0, DEFAULT_DRYING, state, t0 + timedelta(minutes=10))
        self.assertEqual(delta, 0.0)
        self.assertIsNone(state.active_since)

    def test_disabled_or_unknown_humidity(self) -> None:
        t0 = datetime(2026, 10, 5, 7, 0)
        disabled = {**DEFAULT_DRYING, "enabled": False}
        self.assertEqual(drying_adjustment(90.0, disabled, DryingState(), t0)[0], 0.0)
        self.assertEqual(drying_adjustment(None, DEFAULT_DRYING, DryingState(), t0)[0], 0.0)


class AcInsufficientTests(unittest.TestCase):
    CFG = DEFAULT_SOURCES["ac_insufficient"]
    NOW = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)

    def _history(self, start: float, end: float) -> list[tuple[datetime, float]]:
        return [(self.NOW - timedelta(minutes=40), start), (self.NOW - timedelta(minutes=20),
                                                            (start + end) / 2), (self.NOW, end)]

    def test_falling_temperature_while_heating(self) -> None:
        since = self.NOW - timedelta(minutes=45)
        self.assertTrue(ac_insufficient(self._history(20.0, 19.6), since, 21.0, self.NOW, self.CFG))

    def test_not_heating_long_enough_or_rising(self) -> None:
        self.assertFalse(ac_insufficient(self._history(20.0, 19.6),
                                         self.NOW - timedelta(minutes=10),
                                         21.0, self.NOW, self.CFG))
        self.assertFalse(ac_insufficient(self._history(20.0, 19.6), None, 21.0, self.NOW, self.CFG))
        since = self.NOW - timedelta(minutes=45)
        history = self._history(19.6, 20.0)
        self.assertFalse(ac_insufficient(history, since, 21.0, self.NOW, self.CFG))

    def test_short_history_after_restart(self) -> None:
        since = self.NOW - timedelta(minutes=45)
        history = [(self.NOW - timedelta(minutes=5), 20.0), (self.NOW, 19.5)]
        self.assertFalse(ac_insufficient(history, since, 21.0, self.NOW, self.CFG))

    def test_above_target_is_fine(self) -> None:
        since = self.NOW - timedelta(minutes=45)
        history = self._history(22.0, 21.5)
        self.assertFalse(ac_insufficient(history, since, 21.0, self.NOW, self.CFG))


if __name__ == "__main__":
    unittest.main()
