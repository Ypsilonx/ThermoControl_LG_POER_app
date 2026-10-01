"""Testy nastavení zón v aplikaci: návrh z nalezených zařízení, uložení a API editoru."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi import HTTPException  # noqa: E402

from poer_api import PoerApiError  # noqa: E402
from web.routes import zones as zone_routes  # noqa: E402
from zones.config import parse_zones, save_zones  # noqa: E402
from zones.discovery import propose_zones, slugify  # noqa: E402
from zones.loop import ZoneController  # noqa: E402

POER = [{"device_id": "p1", "name": "Kuchobývák"}, {"device_id": "p2", "name": "Koupelna"}]
LG = [{"device_id": "ac1", "name": "Obývák AC"}]


class DiscoveryTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Kuchobývák"), "kuchobyvak")
        self.assertEqual(slugify("Pokoj č. 2"), "pokoj_c_2")
        self.assertEqual(slugify("!!!"), "zona")

    def test_one_zone_per_thermostat_and_ac_in_first_zone(self) -> None:
        raw = propose_zones(POER, LG)
        cfg = parse_zones(raw)
        self.assertEqual(list(cfg.zones), ["kuchobyvak", "koupelna"])
        kitchen = cfg.zones["kuchobyvak"]
        self.assertEqual(kitchen.name, "Kuchobývák")
        self.assertEqual([h.kind for h in kitchen.heaters], ["lg", "poer"])
        self.assertEqual(kitchen.roles["indoor_temperature"], ("poer_kuchobyvak", "lg_klima"))
        self.assertEqual(cfg.zones["koupelna"].roles["indoor_humidity"], ("poer_koupelna",))
        self.assertEqual(cfg.zones["koupelna"].roles["outdoor_temperature"], ("chmi",))
        self.assertEqual(cfg.sensors["chmi"].max_age_min, 240)

    def test_ac_only_house(self) -> None:
        cfg = parse_zones(propose_zones([], LG))
        self.assertEqual(list(cfg.zones), ["dum"])
        self.assertEqual(cfg.zones["dum"].roles["indoor_temperature"], ("lg_klima",))

    def test_duplicate_names_get_unique_ids(self) -> None:
        raw = propose_zones([{"device_id": "a", "name": "Pokoj"},
                             {"device_id": "b", "name": "Pokoj"}], [])
        self.assertEqual(list(raw["zones"]), ["pokoj", "pokoj_2"])
        self.assertEqual(set(raw["sensors"]) - {"chmi"}, {"poer_pokoj", "poer_pokoj_2"})

    def test_nothing_found_is_error(self) -> None:
        with self.assertRaises(ValueError):
            propose_zones([], [])


class SaveZonesTests(unittest.TestCase):
    def test_invalid_zone_id_is_rejected(self) -> None:
        raw = propose_zones(POER, [])
        raw["zones"]["Špatné ID"] = raw["zones"].pop("koupelna")
        with self.assertRaisesRegex(ValueError, "Špatné ID"):
            parse_zones(raw)

    def test_save_validates_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "zones.json"
            save_zones(path, propose_zones(POER, LG))
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["zones"]),
                             {"kuchobyvak", "koupelna"})
            with self.assertRaises(ValueError):
                save_zones(path, {"zones": {}, "sensors": {}})
            # Neplatná konfigurace nepřepíše uloženou.
            self.assertIn("kuchobyvak", path.read_text(encoding="utf-8"))
            self.assertFalse(path.with_suffix(".json.tmp").exists())


class ZoneEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.zones_path = base / "zones.json"
        self.zones = ZoneController(self.zones_path, base / "control.json", base / "state.json",
                                    "eu-key", object(),
                                    local_now=lambda: datetime(2026, 10, 5, 10, 0))
        self.zones.reload()
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(zones=self.zones)))
        poer = patch.object(zone_routes, "fetch_poer_devices", AsyncMock(return_value=[
            {"device_id": "p1", "name": "Kuchobývák", "min_temp_c": 5, "max_temp_c": 32},
            {"device_id": "p2", "name": "Koupelna", "min_temp_c": 5, "max_temp_c": 32},
        ]))
        poer.start()
        self.addCleanup(poer.stop)
        lg = patch.object(zone_routes, "_lg_devices", return_value=LG)
        lg.start()
        self.addCleanup(lg.stop)

    async def test_unconfigured_state_lists_devices(self) -> None:
        state = await zone_routes.get_zones_setup(self.request)
        self.assertFalse(state["configured"])
        self.assertIsNone(state["config"])
        self.assertEqual([d["device_id"] for d in state["devices"]["poer"]], ["p1", "p2"])
        self.assertEqual(state["devices"]["lg"], LG)
        self.assertIn("poer_koupelna", state["available_sensors"])

    async def test_proposal_and_save_reload_controller(self) -> None:
        proposal = await zone_routes.get_zones_proposal(self.request)
        result = await zone_routes.save_zones_setup(proposal, self.request)
        self.assertTrue(result["configured"])
        self.assertTrue(self.zones_path.exists())
        self.assertEqual(self.zones.zone_ids, ["kuchobyvak", "koupelna"])
        # Řízení dostalo cíle pro nové zóny.
        self.assertIn("koupelna", self.zones.control["automation"]["targets"])

    async def test_invalid_setup_is_422(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await zone_routes.save_zones_setup({"zones": {}, "sensors": {}}, self.request)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_poer_cloud_error_still_returns_lg(self) -> None:
        zone_routes.fetch_poer_devices.side_effect = PoerApiError("POER SYNC selhal")
        state = await zone_routes.get_zones_setup(self.request)
        self.assertEqual(state["devices"]["poer"], [])
        self.assertIn("POER", state["warning"])

    async def test_removed_zone_drops_its_state(self) -> None:
        await zone_routes.save_zones_setup(propose_zones(POER, LG), self.request)
        self.zones.zone_states["koupelna"] = {"id": "koupelna"}
        await zone_routes.save_zones_setup(propose_zones(POER[:1], LG), self.request)
        self.assertEqual(self.zones.zone_ids, ["kuchobyvak"])
        self.assertNotIn("koupelna", self.zones.zone_states)
        self.assertNotIn("koupelna", self.zones.control["automation"]["targets"])


if __name__ == "__main__":
    unittest.main()
