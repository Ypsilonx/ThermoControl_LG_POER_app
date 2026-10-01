"""Testy tarifu D25d (okna NT) a výpočtu východu/západu slunce."""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zones.sun import sun_times  # noqa: E402
from zones.tariff import is_low_tariff  # noqa: E402

TARIFF = {
    "workday": [{"from": "22:00", "to": "06:00"}, {"from": "13:00", "to": "15:00"}],
    "weekend": [{"from": "00:00", "to": "08:00"}],
}
# 2026-10-09 je pátek, 2026-10-10 sobota.
FRIDAY = datetime(2026, 10, 9)
SATURDAY = datetime(2026, 10, 10)
# Pevné posuny místo zoneinfo – Windows nemá databázi časových zón (tzdata).
CEST = timezone(timedelta(hours=2))
CET = timezone(timedelta(hours=1))


def _at(day: datetime, h: int, m: int = 0) -> datetime:
    """Datum ``day`` v čase h:m."""
    return day.replace(hour=h, minute=m)


class TariffTests(unittest.TestCase):
    def test_window_over_midnight(self) -> None:
        self.assertTrue(is_low_tariff(TARIFF, _at(FRIDAY, 23, 30)))
        self.assertTrue(is_low_tariff(TARIFF, _at(FRIDAY, 5, 59)))
        self.assertFalse(is_low_tariff(TARIFF, _at(FRIDAY, 6, 0)))

    def test_several_windows(self) -> None:
        self.assertTrue(is_low_tariff(TARIFF, _at(FRIDAY, 14)))
        self.assertFalse(is_low_tariff(TARIFF, _at(FRIDAY, 15)))

    def test_weekend_uses_weekend_windows(self) -> None:
        self.assertTrue(is_low_tariff(TARIFF, _at(SATURDAY, 7)))
        self.assertFalse(is_low_tariff(TARIFF, _at(SATURDAY, 14)))
        self.assertFalse(is_low_tariff(TARIFF, _at(SATURDAY, 23)))

    def test_no_windows_means_high_tariff(self) -> None:
        self.assertFalse(is_low_tariff({"workday": [], "weekend": []}, _at(FRIDAY, 3)))


class SunTests(unittest.TestCase):
    def _close(self, actual: datetime, hh: int, mm: int) -> None:
        """Ověří čas s tolerancí 10 min (tabulkové hodnoty se zaokrouhlují, refrakce)."""
        expected = actual.replace(hour=hh, minute=mm, second=0, microsecond=0)
        self.assertLess(abs((actual - expected).total_seconds()), 600, actual)

    def test_valasske_mezirici_autumn(self) -> None:
        sunrise, sunset = sun_times(date(2026, 10, 1), 49.47, 17.97, CEST)
        self._close(sunrise, 6, 50)
        self._close(sunset, 18, 33)
        self.assertEqual(sunrise.utcoffset().total_seconds(), 7200)

    def test_valasske_mezirici_winter(self) -> None:
        sunrise, sunset = sun_times(date(2026, 12, 21), 49.47, 17.97, CET)
        self._close(sunrise, 7, 44)
        self._close(sunset, 15, 50)


if __name__ == "__main__":
    unittest.main()
