"""Testy obecného arbitra příkazů (command_arbiter.py)."""

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from command_arbiter import (  # noqa: E402
    ArbiterClosed,
    ArbiterConfig,
    CommandArbiter,
    CommandOutcome,
    CommandRequest,
    CommandSource,
    CommandSuperseded,
    DeviceTiming,
)


def _config(min_interval_s=0.0, max_attempts=1, unavailable_after_failures=3) -> ArbiterConfig:
    """Sestaví konfiguraci s krátkými časy pro testy."""
    timing = DeviceTiming(
        min_interval_s=min_interval_s, max_attempts=max_attempts, backoff_base_s=0.0
    )
    return ArbiterConfig(
        timings={}, default_timing=timing, unavailable_after_failures=unavailable_after_failures
    )


def _job(log: list, name: str, sent: bool = True, gate: asyncio.Event | None = None,
         started: asyncio.Event | None = None):
    """Vrátí úlohu, která zapíše své jméno do logu a volitelně čeká na bránu."""
    async def run() -> CommandOutcome:
        if started is not None:
            started.set()
        log.append(name)
        if gate is not None:
            await gate.wait()
        return CommandOutcome(sent=sent)
    return run


def _tracked_job(events: list, name: str, delay: float):
    """Vrátí úlohu, která zaznamená začátek a konec a mezi nimi čeká."""
    async def run() -> CommandOutcome:
        events.append(f"{name}:start")
        await asyncio.sleep(delay)
        events.append(f"{name}:end")
        return CommandOutcome(sent=True)
    return run


class CommandArbiterTests(unittest.IsolatedAsyncioTestCase):
    """Chování arbitra: sériovost, priority, nahrazování, frekvence, opakování."""

    async def asyncSetUp(self) -> None:
        self.arbiter = CommandArbiter(_config())

    async def asyncTearDown(self) -> None:
        await self.arbiter.close()

    async def _block_lane(self, device_key: str) -> asyncio.Event:
        """Obsadí zařízení úlohou čekající na bránu; vrátí bránu k uvolnění."""
        gate = asyncio.Event()
        started = asyncio.Event()
        self._blocker = asyncio.create_task(self.arbiter.submit(CommandRequest(
            device_key, "blocker", CommandSource.MANUAL,
            _job([], "blocker", gate=gate, started=started),
        )))
        await started.wait()
        return gate

    async def test_same_device_runs_serially(self) -> None:
        events: list[str] = []
        await asyncio.gather(
            self.arbiter.submit(CommandRequest(
                "lg:1", "a", CommandSource.MANUAL, _tracked_job(events, "a", 0.05))),
            self.arbiter.submit(CommandRequest(
                "lg:1", "b", CommandSource.MANUAL, _tracked_job(events, "b", 0.05))),
        )
        self.assertEqual(events, ["a:start", "a:end", "b:start", "b:end"])

    async def test_different_devices_run_in_parallel(self) -> None:
        events: list[str] = []
        await asyncio.gather(
            self.arbiter.submit(CommandRequest(
                "lg:1", "a", CommandSource.MANUAL, _tracked_job(events, "a", 0.05))),
            self.arbiter.submit(CommandRequest(
                "lg:2", "b", CommandSource.MANUAL, _tracked_job(events, "b", 0.05))),
        )
        self.assertEqual(sorted(events[:2]), ["a:start", "b:start"])

    async def test_higher_priority_runs_first(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        sched = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "x", CommandSource.SCHEDULE, _job(log, "sched"))))
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "y", CommandSource.MANUAL, _job(log, "manual"))))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(sched, manual, self._blocker)
        self.assertEqual(log, ["manual", "sched"])

    async def test_newer_same_key_same_source_replaces_pending(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        old = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "temp", CommandSource.AUTOMATION, _job(log, "20"))))
        await asyncio.sleep(0)
        new = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "temp", CommandSource.AUTOMATION, _job(log, "21"))))
        await asyncio.sleep(0)
        gate.set()
        with self.assertRaises(CommandSuperseded):
            await old
        await asyncio.gather(new, self._blocker)
        self.assertEqual(log, ["21"])

    async def test_lower_priority_does_not_replace_higher_pending(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "temp", CommandSource.MANUAL, _job(log, "manual"))))
        await asyncio.sleep(0)
        with self.assertRaises(CommandSuperseded):
            await self.arbiter.submit(CommandRequest(
                "lg:1", "temp", CommandSource.AUTOMATION, _job(log, "auto")))
        gate.set()
        await asyncio.gather(manual, self._blocker)
        self.assertEqual(log, ["manual"])

    async def test_higher_priority_replaces_lower_pending(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        auto = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "temp", CommandSource.AUTOMATION, _job(log, "auto"))))
        await asyncio.sleep(0)
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "temp", CommandSource.MANUAL, _job(log, "manual"))))
        await asyncio.sleep(0)
        gate.set()
        with self.assertRaises(CommandSuperseded):
            await auto
        await asyncio.gather(manual, self._blocker)
        self.assertEqual(log, ["manual"])

    async def test_manual_request_drops_pending_automation(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        auto = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "state", CommandSource.AUTOMATION, _job(log, "auto_on"))))
        await asyncio.sleep(0)
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "power_off", CommandSource.MANUAL, _job(log, "manual_off"))))
        await asyncio.sleep(0)
        gate.set()
        with self.assertRaises(CommandSuperseded):
            await auto
        await asyncio.gather(manual, self._blocker)
        self.assertEqual(log, ["manual_off"])

    async def test_manual_request_keeps_pending_schedule(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        sched = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "state", CommandSource.SCHEDULE, _job(log, "sched_off"))))
        await asyncio.sleep(0)
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "set_temperature", CommandSource.MANUAL, _job(log, "manual"))))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(sched, manual, self._blocker)
        self.assertEqual(log, ["manual", "sched_off"])

    async def test_cancelled_caller_is_skipped(self) -> None:
        gate = await self._block_lane("lg:1")
        log: list[str] = []
        doomed = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "x", CommandSource.AUTOMATION, _job(log, "doomed"))))
        await asyncio.sleep(0)
        doomed.cancel()
        await asyncio.sleep(0)
        after = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "y", CommandSource.AUTOMATION, _job(log, "after"))))
        gate.set()
        await asyncio.gather(after, self._blocker)
        self.assertEqual(log, ["after"])

    async def test_rate_limit_delays_automation_but_not_manual(self) -> None:
        await self.arbiter.close()
        self.arbiter = CommandArbiter(_config(min_interval_s=0.3))
        log: list[str] = []
        await self.arbiter.submit(CommandRequest(
            "lg:1", "a", CommandSource.AUTOMATION, _job(log, "a")))
        start = time.monotonic()
        await self.arbiter.submit(CommandRequest(
            "lg:1", "b", CommandSource.MANUAL, _job(log, "b")))
        self.assertLess(time.monotonic() - start, 0.15)
        start = time.monotonic()
        await self.arbiter.submit(CommandRequest(
            "lg:1", "c", CommandSource.AUTOMATION, _job(log, "c")))
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    async def test_skipped_outcome_does_not_consume_rate_limit(self) -> None:
        await self.arbiter.close()
        self.arbiter = CommandArbiter(_config(min_interval_s=0.3))
        log: list[str] = []
        await self.arbiter.submit(CommandRequest(
            "lg:1", "a", CommandSource.AUTOMATION, _job(log, "a", sent=False)))
        start = time.monotonic()
        await self.arbiter.submit(CommandRequest(
            "lg:1", "b", CommandSource.AUTOMATION, _job(log, "b")))
        self.assertLess(time.monotonic() - start, 0.15)

    async def test_retries_until_success(self) -> None:
        await self.arbiter.close()
        self.arbiter = CommandArbiter(_config(max_attempts=3))
        calls = {"n": 0}

        async def flaky() -> CommandOutcome:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("výpadek")
            return CommandOutcome(sent=True)

        outcome = await self.arbiter.submit(CommandRequest(
            "poer:1", "set_temp", CommandSource.AUTOMATION, flaky))
        self.assertTrue(outcome.sent)
        self.assertEqual(calls["n"], 3)

    async def test_health_marks_unavailable_and_recovers(self) -> None:
        await self.arbiter.close()
        self.arbiter = CommandArbiter(_config(unavailable_after_failures=2))

        async def failing() -> CommandOutcome:
            raise RuntimeError("cloud nedostupný")

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.arbiter.submit(CommandRequest(
                    "lg:1", "x", CommandSource.AUTOMATION, failing))
        health = self.arbiter.health("lg:1")
        self.assertFalse(health.available)
        self.assertEqual(health.consecutive_failures, 2)
        self.assertEqual(health.last_error, "cloud nedostupný")

        await self.arbiter.submit(CommandRequest(
            "lg:1", "x", CommandSource.AUTOMATION, _job([], "ok")))
        self.assertTrue(self.arbiter.health("lg:1").available)
        self.assertEqual(self.arbiter.health("lg:1").consecutive_failures, 0)

    async def test_unknown_device_is_healthy(self) -> None:
        self.assertTrue(self.arbiter.health("lg:neznámé").available)

    async def test_close_fails_pending_and_rejects_new(self) -> None:
        await self._block_lane("lg:1")
        pending = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "x", CommandSource.AUTOMATION, _job([], "x"))))
        await asyncio.sleep(0)
        await self.arbiter.close()
        with self.assertRaises(ArbiterClosed):
            await pending
        with self.assertRaises(ArbiterClosed):
            await self._blocker
        with self.assertRaises(ArbiterClosed):
            await self.arbiter.submit(CommandRequest(
                "lg:1", "y", CommandSource.MANUAL, _job([], "y")))

    async def test_invalid_command_is_not_retried_nor_counted_as_failure(self) -> None:
        await self.arbiter.close()
        self.arbiter = CommandArbiter(_config(max_attempts=3, unavailable_after_failures=1))
        calls = {"n": 0}

        async def invalid() -> CommandOutcome:
            calls["n"] += 1
            raise ValueError("Neznámý příkaz")

        with self.assertRaises(ValueError):
            await self.arbiter.submit(CommandRequest(
                "lg:1", "x", CommandSource.MANUAL, invalid))
        self.assertEqual(calls["n"], 1)
        self.assertTrue(self.arbiter.health("lg:1").available)
        self.assertEqual(self.arbiter.health("lg:1").consecutive_failures, 0)

    async def test_crashed_worker_fails_pending_and_lane_recovers(self) -> None:
        original = self.arbiter._execute

        async def broken(*_args):
            raise AssertionError("chyba v arbitrovi")

        self.arbiter._execute = broken
        with self.assertRaises(CommandSuperseded):
            await asyncio.wait_for(self.arbiter.submit(CommandRequest(
                "lg:1", "x", CommandSource.MANUAL, _job([], "x"))), timeout=1)
        self.arbiter._execute = original
        outcome = await asyncio.wait_for(self.arbiter.submit(CommandRequest(
            "lg:1", "y", CommandSource.MANUAL, _job([], "y"))), timeout=1)
        self.assertTrue(outcome.sent)


if __name__ == "__main__":
    unittest.main()
