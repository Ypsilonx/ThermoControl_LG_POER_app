"""Scénářové testy rozhodování zóny (čisté funkce v zones/decide.py)."""

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zones.config import default_control  # noqa: E402
from zones.decide import (  # noqa: E402
    decide_zone,
    next_block_start,
    override_until,
    program_block,
)

ZONES = ["kuchoobyvak", "koupelna"]
# 2026-10-05 je pondělí.
MON = datetime(2026, 10, 5)


def _control(mode: str, **changes) -> dict:
    """Výchozí konfigurace řízení s úpravami."""
    control = default_control(ZONES, mode)
    control.update(changes)
    return control


def _at(day: datetime, hhmm: str) -> datetime:
    """Datum ``day`` v čase ``hhmm``."""
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m)


class ProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program = _control("program")["program"]

    def test_current_block_of_the_day(self) -> None:
        block, start = program_block(self.program, _at(MON, "09:15"))
        self.assertEqual(block["from"], "08:00")
        self.assertEqual(start, _at(MON, "08:00"))

    def test_before_first_block_uses_previous_day(self) -> None:
        # Pondělí 03:00 → poslední blok neděle (22:30).
        block, start = program_block(self.program, _at(MON, "03:00"))
        self.assertEqual(block["from"], "22:30")
        self.assertEqual(start, datetime(2026, 10, 4, 22, 30))

    def test_next_block_start_wraps_to_next_day(self) -> None:
        self.assertEqual(next_block_start(self.program, _at(MON, "09:00")), _at(MON, "15:00"))
        # Pátek 23:00 → sobota 07:00.
        friday = datetime(2026, 10, 9)
        self.assertEqual(next_block_start(self.program, _at(friday, "23:00")),
                         datetime(2026, 10, 10, 7, 0))
        # Neděle 23:00 → pondělí 05:30.
        sunday = datetime(2026, 10, 11)
        self.assertEqual(next_block_start(self.program, _at(sunday, "23:00")),
                         datetime(2026, 10, 12, 5, 30))


class DecideZoneTests(unittest.TestCase):
    def test_manual_does_not_control(self) -> None:
        decision = decide_zone("koupelna", _control("manual"), 20.0, None, _at(MON, "10:00"))
        self.assertIsNone(decision.target_c)
        self.assertFalse(decision.emergency)

    def test_manual_below_emergency_minimum_heats(self) -> None:
        decision = decide_zone("koupelna", _control("manual"), 11.0, None, _at(MON, "10:00"))
        self.assertEqual(decision.target_c, 12.0)
        self.assertTrue(decision.emergency)

    def test_program_target(self) -> None:
        control = _control("program")
        control["program"]["mon"][2]["targets"]["koupelna"] = 22.5
        decision = decide_zone("koupelna", control, 20.0, None, _at(MON, "16:00"))
        self.assertEqual(decision.target_c, 22.5)
        self.assertIn("15:00", decision.reason)

    def test_automation_target_and_night_setback_over_midnight(self) -> None:
        control = _control("automation")
        control["automation"]["targets"]["kuchoobyvak"] = 21.5
        control["automation"]["night_setback"] = {
            "enabled": True, "from": "22:00", "to": "05:30", "delta_c": 2.0}
        day = decide_zone("kuchoobyvak", control, 20.0, None, _at(MON, "12:00"))
        late = decide_zone("kuchoobyvak", control, 20.0, None, _at(MON, "23:30"))
        early = decide_zone("kuchoobyvak", control, 20.0, None, _at(MON, "04:00"))
        self.assertEqual(day.target_c, 21.5)
        self.assertEqual(late.target_c, 19.5)
        self.assertEqual(early.target_c, 19.5)

    def test_vacation_away_preheat_and_after_return(self) -> None:
        control = _control("vacation")
        control["vacation"].update(return_at="2026-10-05T18:00", preheat_hours=6.0,
                                   previous_mode="automation")
        control["automation"]["targets"]["koupelna"] = 22.0
        away = decide_zone("koupelna", control, 18.0, None, _at(MON, "11:00"))
        preheat = decide_zone("koupelna", control, 18.0, None, _at(MON, "12:30"))
        back = decide_zone("koupelna", control, 18.0, None, _at(MON, "19:00"))
        self.assertEqual(away.target_c, 15.0)
        self.assertEqual(preheat.target_c, 22.0)
        self.assertIn("předtopení", preheat.reason)
        self.assertEqual(back.target_c, 22.0)

    def test_override_wins_until_it_expires(self) -> None:
        control = _control("program")
        control["overrides"] = {"koupelna": {"target_c": 24.0, "until": "2026-10-05T15:00:00"}}
        active = decide_zone("koupelna", control, 20.0, None, _at(MON, "10:00"))
        expired = decide_zone("koupelna", control, 20.0, None, _at(MON, "15:30"))
        self.assertEqual(active.target_c, 24.0)
        self.assertIn("Přebití", active.reason)
        self.assertEqual(expired.target_c, 21.0)

    def test_fireplace_pauses_zone_but_not_in_manual(self) -> None:
        paused = decide_zone("kuchoobyvak", _control("automation"), 22.0, True, _at(MON, "18:00"))
        self.assertTrue(paused.paused)
        self.assertEqual(paused.target_c, 12.0)
        manual = decide_zone("kuchoobyvak", _control("manual"), 22.0, True, _at(MON, "18:00"))
        self.assertFalse(manual.paused)

    def test_emergency_raises_target_and_flags(self) -> None:
        control = _control("automation")
        control["automation"]["targets"]["koupelna"] = 10.0
        decision = decide_zone("koupelna", control, 11.5, None, _at(MON, "12:00"))
        self.assertTrue(decision.emergency)
        self.assertEqual(decision.target_c, 12.0)

    def test_missing_indoor_temperature_holds(self) -> None:
        decision = decide_zone("koupelna", _control("automation"), None, None, _at(MON, "12:00"))
        self.assertTrue(decision.hold)
        self.assertEqual(decision.target_c, 21.0)


class OverrideUntilTests(unittest.TestCase):
    def test_program_override_lasts_until_next_block(self) -> None:
        self.assertEqual(override_until(_control("program"), _at(MON, "09:00")), _at(MON, "15:00"))

    def test_other_modes_use_override_hours(self) -> None:
        control = _control("automation", override_hours=2.0)
        self.assertEqual(override_until(control, _at(MON, "09:00")), _at(MON, "11:00"))


if __name__ == "__main__":
    unittest.main()
