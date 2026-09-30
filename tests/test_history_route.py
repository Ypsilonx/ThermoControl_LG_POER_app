"""Testy exportní routy historie a načtení nastavení historie z prostředí."""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi import HTTPException  # noqa: E402

from history.collector import HistoryCollector  # noqa: E402
from history.store import HistoryStore  # noqa: E402
from web.routes import history as history_route  # noqa: E402
from web.settings import Settings, _parse_power_map  # noqa: E402


def _request(collector):
    """Falešný Request s app.state.history."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(history=collector)))


class HistoryRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.collector = HistoryCollector(HistoryStore(Path(self._tmp.name) / "h.db"), None)

    async def test_export_returns_csv(self) -> None:
        response = await history_route.export_history(
            _request(self.collector), data="energy", od=date(2026, 10, 1), do=date(2026, 10, 1)
        )
        self.assertEqual(response.media_type, "text/csv")
        self.assertEqual(response.body.decode("utf-8-sig").strip(), "zarizeni;den;wh")
        self.assertIn("historie_energy_2026-10-01_2026-10-01.csv",
                      response.headers["content-disposition"])

    async def test_disabled_history_returns_503(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await history_route.export_history(_request(None), data="energy",
                                               od=date(2026, 10, 1), do=date(2026, 10, 1))
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_reversed_range_returns_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await history_route.export_history(_request(self.collector), data="energy",
                                               od=date(2026, 10, 2), do=date(2026, 10, 1))
        self.assertEqual(ctx.exception.status_code, 400)


class HistorySettingsTests(unittest.TestCase):
    def test_power_map_parsing(self) -> None:
        self.assertEqual(_parse_power_map("fee89300f2a5:3500, fee89300fac5:560"),
                         {"fee89300f2a5": 3.5, "fee89300fac5": 0.56})
        self.assertEqual(_parse_power_map(""), {})
        self.assertEqual(_parse_power_map("spatne,x:abc"), {})

    def test_history_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            settings = Settings()
        self.assertTrue(settings.history_enabled)
        self.assertEqual(settings.history_poll_s, 300)
        self.assertEqual(settings.history_retention_days, 90)


if __name__ == "__main__":
    unittest.main()
