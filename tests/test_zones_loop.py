"""Testy řídicí smyčky zón (zones/loop.py) s falešným arbitrem a POER cloudem."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from command_arbiter import CommandOutcome, CommandSource  # noqa: E402
from zones import loop as loop_mod  # noqa: E402
from zones.config import default_control, save_control  # noqa: E402
from zones.loop import ZoneController  # noqa: E402

ZONES_JSON = {
    "zones": {
        "kuchoobyvak": {
            "name": "Kuchoobývák",
            "heaters": [{"device": "lg:*"}, {"device": "poer:pk", "offset_c": -1.0}],
            "roles": {"indoor_temperature": ["poer_k"]},
        },
        "koupelna": {
            "name": "Koupelna",
            "heaters": [{"device": "poer:pb"}],
            "roles": {"indoor_temperature": ["poer_b"]},
        },
    },
    "sensors": {
        "poer_k": {"source": "poer", "device_id": "pk"},
        "poer_b": {"source": "poer", "device_id": "pb"},
    },
}
# Pondělí 2026-10-05 10:00 místního času (UTC+2).
NOW_LOCAL = datetime(2026, 10, 5, 10, 0)


def _statuses(kitchen: float | None = 21.0, bathroom: float | None = 20.0) -> list[dict]:
    """Stav obou termostatů; None = termostat offline."""
    return [
        {"device_id": "pk", "online": kitchen is not None, "current_temperature_c": kitchen},
        {"device_id": "pb", "online": bathroom is not None, "current_temperature_c": bathroom},
    ]


class FakeArbiter:
    """Zaznamená požadavky a vrátí úspěch bez spuštění úlohy."""

    def __init__(self) -> None:
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return CommandOutcome(sent=True)


class ZoneControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.zones_path = base / "zones.json"
        self.zones_path.write_text(json.dumps(ZONES_JSON), encoding="utf-8")
        self.control_path = base / "control.json"
        self.state_path = base / "state.json"
        self.arbiter = FakeArbiter()
        self.now = NOW_LOCAL
        fetch = patch.object(loop_mod, "fetch_poer_statuses_cached",
                             AsyncMock(return_value=_statuses()))
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def _controller(self, mode: str = "automation", **changes) -> ZoneController:
        control = default_control(["kuchoobyvak", "koupelna"], mode)
        control.update(changes)
        save_control(self.control_path, control)
        controller = ZoneController(
            self.zones_path, self.control_path, self.state_path, "eu-key", self.arbiter,
            local_now=lambda: self.now,
        )
        controller.reload()
        return controller

    async def _tick(self, controller: ZoneController) -> None:
        await controller.tick()
        await controller.drain()

    async def test_dry_run_sends_nothing_but_writes_journal(self) -> None:
        controller = self._controller(dry_run=True)
        await self._tick(controller)
        self.assertEqual(self.arbiter.requests, [])
        self.assertEqual({e["zone"] for e in controller.journal}, {"kuchoobyvak", "koupelna"})
        self.assertTrue(all(e["dry_run"] for e in controller.journal))
        self.assertIn("pb → 21.0 °C", controller.journal[-1]["actions"][0])

    async def test_dry_run_journals_action_once_and_sends_after_switch(self) -> None:
        controller = self._controller(dry_run=True)
        await self._tick(controller)
        journal_len = len(controller.journal)
        await self._tick(controller)
        self.assertEqual(len(controller.journal), journal_len)
        controller.update_control({"dry_run": False})
        await self._tick(controller)
        self.assertEqual(len(self.arbiter.requests), 2)

    async def test_setpoint_is_target_plus_offset_rounded(self) -> None:
        controller = self._controller(dry_run=False)
        controller.control["automation"]["targets"]["kuchoobyvak"] = 21.3
        await self._tick(controller)
        sent = {r.device_key: r for r in self.arbiter.requests}
        self.assertEqual(set(sent), {"poer:pk", "poer:pb"})
        self.assertEqual(sent["poer:pk"].source, CommandSource.AUTOMATION)
        self.assertEqual(sent["poer:pk"].key, "set_temp")
        self.assertEqual(controller.zone_states["kuchoobyvak"]["setpoints"], {"poer:pk": 20.5})

    async def test_same_setpoint_is_not_sent_again(self) -> None:
        controller = self._controller(dry_run=False)
        await self._tick(controller)
        await self._tick(controller)
        self.assertEqual(len(self.arbiter.requests), 2)
        journal_len = len(controller.journal)
        await self._tick(controller)
        self.assertEqual(len(controller.journal), journal_len)

    async def test_setpoint_never_below_emergency_minimum(self) -> None:
        controller = self._controller(dry_run=False)
        controller.control["automation"]["targets"]["kuchoobyvak"] = 12.5
        await self._tick(controller)
        self.assertEqual(controller.zone_states["kuchoobyvak"]["setpoints"], {"poer:pk": 12.0})

    async def test_emergency_uses_emergency_source(self) -> None:
        self.fetch.return_value = _statuses(bathroom=10.5)
        controller = self._controller("manual", dry_run=False)
        await self._tick(controller)
        self.assertEqual([r.device_key for r in self.arbiter.requests], ["poer:pb"])
        self.assertEqual(self.arbiter.requests[0].source, CommandSource.EMERGENCY)

    async def test_manual_mode_sends_nothing(self) -> None:
        controller = self._controller("manual", dry_run=False)
        await self._tick(controller)
        self.assertEqual(self.arbiter.requests, [])

    async def test_missing_indoor_temperature_holds(self) -> None:
        self.fetch.return_value = _statuses(bathroom=None)
        controller = self._controller(dry_run=False)
        await self._tick(controller)
        self.assertEqual([r.device_key for r in self.arbiter.requests], ["poer:pk"])
        self.assertTrue(controller.zone_states["koupelna"]["hold"])

    async def test_program_uses_schedule_source(self) -> None:
        controller = self._controller("program", dry_run=False)
        await self._tick(controller)
        self.assertEqual({r.source for r in self.arbiter.requests}, {CommandSource.SCHEDULE})

    async def test_ac_target_only_outside_dry_run(self) -> None:
        controller = self._controller(dry_run=True)
        await self._tick(controller)
        self.assertIsNone(controller.ac_target("lg-dev"))
        controller.control["dry_run"] = False
        await self._tick(controller)
        self.assertEqual(controller.ac_target("lg-dev"), (21.0, False))

    async def test_ac_regulation_holds_without_indoor_temperature(self) -> None:
        self.fetch.return_value = _statuses(kitchen=None)
        controller = self._controller(dry_run=False)
        await self._tick(controller)
        self.assertEqual(controller.ac_target("lg-dev"), (21.0, True))

    async def test_expired_vacation_returns_to_previous_mode(self) -> None:
        vacation = default_control(["kuchoobyvak", "koupelna"], "vacation")["vacation"]
        vacation.update(return_at="2026-10-05T09:00", previous_mode="program")
        controller = self._controller("vacation", vacation=vacation)
        await self._tick(controller)
        self.assertEqual(controller.control["mode"], "program")
        saved = json.loads(self.control_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "program")

    async def test_expired_override_is_removed(self) -> None:
        controller = self._controller(
            overrides={"koupelna": {"target_c": 24.0, "until": "2026-10-05T09:00:00"}})
        await self._tick(controller)
        self.assertEqual(controller.control["overrides"], {})

    async def test_add_override_from_manual_poer_command(self) -> None:
        controller = self._controller("automation", override_hours=2.0)
        zone = controller.add_override("poer", "pk", 23.0)
        self.assertEqual(zone, "kuchoobyvak")
        override = controller.control["overrides"]["kuchoobyvak"]
        # Fólie má posun −1 °C → ruční 23 °C na termostatu = cíl zóny 24 °C.
        self.assertEqual(override["target_c"], 24.0)
        self.assertEqual(override["until"], "2026-10-05T12:00:00")

    async def test_lg_override_converts_ac_setpoint_to_room_temperature(self) -> None:
        controller = self._controller("automation")
        controller._proxy_offset_c = lambda: -2.0
        self.assertEqual(controller.add_override("lg", "lg-dev", 23.0), "kuchoobyvak")
        self.assertEqual(controller.control["overrides"]["kuchoobyvak"]["target_c"], 21.0)

    async def test_manual_poer_setpoint_is_not_resent(self) -> None:
        controller = self._controller("automation", dry_run=False)
        controller.add_override("poer", "pb", 23.0)
        await self._tick(controller)
        self.assertEqual([r.device_key for r in self.arbiter.requests], ["poer:pk"])

    async def test_no_override_in_manual_mode(self) -> None:
        controller = self._controller("manual")
        self.assertIsNone(controller.add_override("poer", "pb", 23.0))
        self.assertEqual(controller.control["overrides"], {})

    async def test_set_mode_vacation_remembers_previous_mode(self) -> None:
        controller = self._controller("program")
        controller.update_control({"vacation": {"return_at": "2026-10-12T18:00"}})
        controller.set_mode("vacation")
        self.assertEqual(controller.control["vacation"]["previous_mode"], "program")
        with self.assertRaises(ValueError):
            controller.set_mode("turbo")

    async def test_legacy_mode_maps_manual_to_hand(self) -> None:
        self.assertEqual(self._controller("manual").legacy_mode, "HAND")
        for mode in ("program", "automation"):
            self.assertEqual(self._controller(mode).legacy_mode, "AUTO")

    async def test_without_zones_file_tick_does_nothing(self) -> None:
        self.zones_path.unlink()
        controller = self._controller(dry_run=False)
        await self._tick(controller)
        self.assertEqual(controller.zone_states, {})
        self.assertEqual(self.arbiter.requests, [])
        self.assertEqual(controller.control["mode"], "automation")


if __name__ == "__main__":
    unittest.main()
