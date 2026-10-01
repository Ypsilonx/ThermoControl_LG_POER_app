"""Test, že plánovač nečeká na arbitra (rate limit nesmí zdržet další minutu)."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from command_arbiter import CommandOutcome  # noqa: E402
from web import app as web_app  # noqa: E402


class GatedArbiter:
    """Falešný arbitr, který drží každý požadavek, dokud se neotevře brána."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        await self.gate.wait()
        return CommandOutcome(sent=True)


class SchedulerDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_before_commands_finish(self) -> None:
        arbiter = GatedArbiter()
        app = SimpleNamespace(state=SimpleNamespace(arbiter=arbiter, background_tasks=set()))
        web_app._dispatch_schedule_action(app, object(), ["dev1"], None)
        self.assertEqual(len(app.state.background_tasks), 1)
        for _ in range(5):  # task → gather → podúloha potřebují několik kroků smyčky
            await asyncio.sleep(0)
        self.assertEqual(len(arbiter.requests), 1)
        arbiter.gate.set()
        await asyncio.gather(*app.state.background_tasks)
        await asyncio.sleep(0)
        self.assertEqual(app.state.background_tasks, set())


class SchedulerGateTests(unittest.TestCase):
    def test_runs_only_in_hand_mode_with_enabled_settings(self) -> None:
        on = {"enable_scheduler": True, "auto_execute": True}
        self.assertTrue(web_app._scheduler_enabled("HAND", on))
        self.assertFalse(web_app._scheduler_enabled("AUTO", on))
        self.assertFalse(web_app._scheduler_enabled("HAND", {**on, "auto_execute": False}))


if __name__ == "__main__":
    unittest.main()
