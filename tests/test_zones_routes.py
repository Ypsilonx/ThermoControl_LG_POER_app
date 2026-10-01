"""Testy API řízení zón (/api/control) a kompatibilního /api/mode."""

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

from web.routes import mode as mode_routes  # noqa: E402
from web.routes import zones as zone_routes  # noqa: E402
from zones.loop import ZoneController  # noqa: E402

ZONES_JSON = {
    "zones": {"koupelna": {"name": "Koupelna", "heaters": [{"device": "poer:pb"}],
                           "roles": {"indoor_temperature": ["poer_b"]}}},
    "sensors": {"poer_b": {"source": "poer", "device_id": "pb"}},
}


class ZoneRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        (base / "zones.json").write_text(json.dumps(ZONES_JSON), encoding="utf-8")
        self.zones = ZoneController(base / "zones.json", base / "control.json",
                                    base / "state.json", "", object(),
                                    local_now=lambda: datetime(2026, 10, 5, 10, 0))
        self.zones.reload()
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(zones=self.zones)))
        broadcast = patch.object(zone_routes.ws_manager, "broadcast", AsyncMock())
        self.broadcast = broadcast.start()
        self.addCleanup(broadcast.stop)

    async def test_get_state(self) -> None:
        state = await zone_routes.get_control(self.request)
        self.assertEqual(state["mode"], "manual")
        self.assertTrue(state["dry_run"])
        self.assertTrue(state["zones_configured"])
        self.assertEqual(state["zones"][0]["id"], "koupelna")
        self.assertIn("program", state["config"])

    async def test_set_mode_broadcasts(self) -> None:
        result = await zone_routes.set_mode(zone_routes.ModeBody(mode="program"), self.request)
        self.assertEqual(result["mode"], "program")
        self.assertEqual(self.zones.control["mode"], "program")
        self.assertEqual(self.broadcast.await_args.args[0]["type"], "mode_change")

    async def test_vacation_without_return_is_422(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await zone_routes.set_mode(zone_routes.ModeBody(mode="vacation"), self.request)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_update_config(self) -> None:
        body = {"dry_run": False, "automation": {"targets": {"koupelna": 22.5}}}
        result = await zone_routes.update_config(body, self.request)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["automation"]["targets"]["koupelna"], 22.5)
        # Noční útlum zůstal zachován (slučuje se o úroveň níž).
        self.assertIn("night_setback", result["automation"])

    async def test_update_config_rejects_unknown_key_and_invalid_value(self) -> None:
        for body in ({"mode": "program"}, {"emergency_min_c": 1}):
            with self.subTest(body=body), self.assertRaises(HTTPException) as ctx:
                await zone_routes.update_config(body, self.request)
            self.assertEqual(ctx.exception.status_code, 422)

    async def test_cancel_override(self) -> None:
        self.zones.set_mode("automation")
        self.zones.add_override("poer", "pb", 24.0)
        await zone_routes.cancel_override("koupelna", self.request)
        self.assertEqual(self.zones.control["overrides"], {})
        with self.assertRaises(HTTPException) as ctx:
            await zone_routes.cancel_override("puda", self.request)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_journal_newest_first(self) -> None:
        self.zones.journal.extend([{"ts": "1"}, {"ts": "2"}, {"ts": "3"}])
        self.assertEqual(await zone_routes.get_journal(self.request, limit=2),
                         [{"ts": "3"}, {"ts": "2"}])

    async def test_legacy_mode_endpoint_maps_modes(self) -> None:
        await mode_routes.set_mode(mode_routes.ModeRequest(mode="AUTO"), self.request)
        self.assertEqual(self.zones.control["mode"], "automation")
        self.assertEqual(await mode_routes.get_mode(self.request), {"mode": "AUTO"})
        await mode_routes.set_mode(mode_routes.ModeRequest(mode="HAND"), self.request)
        self.assertEqual(self.zones.control["mode"], "manual")


if __name__ == "__main__":
    unittest.main()
