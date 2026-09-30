# Arbitr příkazů – implementační plán

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Všechny příkazy pro LG klimatizaci a POER termostat posílat přes jednoho arbitra, který je pro každé zařízení vykonává sériově, podle priority zdroje, s nahrazováním, deduplikací a omezením frekvence – a tím odstranit dnešní kolize mezi webem, HAND schedulerem a AUTO smyčkou.

**Architecture:** Nový modul `src/command_arbiter.py` (obecný arbitr: fronta + worker na zařízení, nic neví o LG/POER). Nový modul `src/device_jobs.py` (úlohy = async funkce, které si stav zařízení čtou až uvnitř, tedy když mají zařízení výhradně pro sebe; stávající pipeline `build_command_plan` → `execute_plan` zůstává). Webové routy, scheduler a AUTO smyčka jen sestaví úlohu a předají ji arbitrovi; instance arbitra žije v `app.state.arbiter`.

**Tech Stack:** Python 3.12, asyncio, FastAPI, stdlib `unittest` (`IsolatedAsyncioTestCase`) spouštěný přes pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-zonove-rizeni-topeni-design.md` (kapitola 4.2, podprojekt 2)

## Global Constraints

- Kód, komentáře a docstringy česky; docstring u každé funkce/třídy (účel, parametry, návratová hodnota).
- Importy uvnitř projektu bez prefixu (`from command_arbiter import ...`, ne `from src.command_arbiter`).
- flake8: `max-line-length = 100`, `max-complexity = 10`.
- Nikdy neblokovat event loop synchronním voláním.
- Priorita zdrojů: `nouze` (3) > `ručně` / `přebití` (2) > `program` / `automatika` (1).
- Výchozí omezení frekvence: LG 30 s, POER 120 s; zdroje `emergency`, `manual`, `override` limit obcházejí.
- Žádné nové runtime závislosti; `pytest` jen jako dev závislost.
- `requirements.txt` se needituje ručně – generuje se `uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt`.
- Odchylka od specifikace: CLI běží jako samostatný proces, arbitr (v paměti web serveru) ho koordinovat nemůže. CLI proto použije stejnou úlohu `lg_command_job` (čerstvý stav, stejná pipeline), ale bez arbitra. Legacy Tkinter GUI se nemění.

## Review Focus

- Volající to vzdá (HTTP klient zavře spojení → future zrušena) → worker takovou položku přeskočí a nespadne na `InvalidStateError`. Test: `test_cancelled_caller_is_skipped` (Task 1).
- Uživatel rychle klikne 3× na teplotu → starší ruční příkazy se nahradí a web dostane `skipped=True`, ne chybu 5xx. Test: `test_superseded_manual_command_returns_skipped` (Task 4).
- Úloha, která nic neodeslala (stav už odpovídá), nesmí spotřebovat okno omezení frekvence. Test: `test_skipped_outcome_does_not_consume_rate_limit` (Task 1).
- Opakovaně selhávající zařízení se označí jako nedostupné a po prvním úspěchu se vrátí. Test: `test_health_marks_unavailable_and_recovers` (Task 1).
- Vypnutí serveru s čekajícími příkazy nesmí viset – čekající dostanou `CommandSuperseded`. Test: `test_close_fails_pending_and_rejects_new` (Task 1).

---

## File Structure

| Soubor | Akce | Odpovědnost |
|---|---|---|
| `pyproject.toml`, `uv.lock` | Modify | dev závislost `pytest` |
| `src/command_arbiter.py` | Create | obecný arbitr: fronty, priority, nahrazování, frekvence, opakování, zdraví zařízení |
| `src/device_jobs.py` | Create | úlohy pro LG a POER + klíče zařízení |
| `src/web/routes/devices.py` | Modify | helper `_get_arbiter` |
| `src/web/routes/control.py` | Modify | ruční LG příkazy přes arbitra |
| `src/web/routes/poer.py` | Modify | ruční POER příkazy přes arbitra |
| `src/web/app.py` | Modify | vytvoření/ukončení arbitra; scheduler a AUTO smyčka přes arbitra; odstranění `_run_schedule_on/_off` |
| `src/main.py` | Modify | CLI přes `lg_command_job` |
| `tests/test_command_arbiter.py` | Create | testy arbitra |
| `tests/test_device_jobs.py` | Create | testy úloh |
| `tests/test_web_command_routes.py` | Create | testy rout control/poer |
| `CLAUDE.md` | Modify | popis pipeline s arbitrem |

---

### Task 1: Obecný arbitr příkazů

**Files:**
- Modify: `pyproject.toml` (přes `uv add --dev pytest`)
- Create: `src/command_arbiter.py`
- Test: `tests/test_command_arbiter.py`

**Interfaces:**
- Consumes: nic.
- Produces:
  - `class CommandSource(StrEnum)`: `EMERGENCY`, `MANUAL`, `OVERRIDE`, `SCHEDULE`, `AUTOMATION`
  - `@dataclass(frozen=True) class CommandOutcome(sent: bool, skip_reason: str | None = None, steps: list[dict] = [])`
  - `class CommandSuperseded(Exception)`
  - `@dataclass(frozen=True) class DeviceTiming(min_interval_s: float, max_attempts: int, backoff_base_s: float)`
  - `@dataclass(frozen=True) class ArbiterConfig(timings: dict[str, DeviceTiming], default_timing: DeviceTiming, unavailable_after_failures: int = 3)` s metodou `timing_for(device_key: str) -> DeviceTiming`
  - `DEFAULT_CONFIG: ArbiterConfig`
  - `JobFactory = Callable[[], Awaitable[CommandOutcome]]`
  - `@dataclass(frozen=True) class CommandRequest(device_key: str, key: str, source: CommandSource, run: JobFactory)`
  - `@dataclass class DeviceHealth(available: bool = True, consecutive_failures: int = 0, last_error: str | None = None)`
  - `class CommandArbiter(config: ArbiterConfig = DEFAULT_CONFIG)`: `async submit(request) -> CommandOutcome`, `health(device_key) -> DeviceHealth`, `async close() -> None`

- [ ] **Step 1: Přidej pytest jako dev závislost**

Run: `uv add --dev pytest`
Expected: `pyproject.toml` obsahuje sekci `[dependency-groups]` s `dev = ["pytest>=..."]`, `uv.lock` aktualizován. `requirements.txt` se nemění (generuje se s `--no-dev`).

Ověř: `uv run pytest tests/test_weather_provider.py -q` → všechny testy PASS.

- [ ] **Step 2: Napiš padající testy**

Vytvoř `tests/test_command_arbiter.py`:

```python
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
        auto = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "x", CommandSource.AUTOMATION, _job(log, "auto"))))
        manual = asyncio.create_task(self.arbiter.submit(CommandRequest(
            "lg:1", "y", CommandSource.MANUAL, _job(log, "manual"))))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(auto, manual, self._blocker)
        self.assertEqual(log, ["manual", "auto"])

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
        with self.assertRaises(CommandSuperseded):
            await pending
        with self.assertRaises(CommandSuperseded):
            await self._blocker
        with self.assertRaises(RuntimeError):
            await self.arbiter.submit(CommandRequest(
                "lg:1", "y", CommandSource.MANUAL, _job([], "y")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_command_arbiter.py -q`
Expected: FAIL – `ModuleNotFoundError: No module named 'command_arbiter'`

- [ ] **Step 4: Implementuj arbitra**

Vytvoř `src/command_arbiter.py`:

```python
# -*- coding: utf-8 -*-
"""
Arbitr příkazů – jediná brána pro odesílání příkazů zařízením.

Každé zařízení má vlastní frontu a vlastního workera, takže vícekrokové
sekvence (zapni → mód → teplota) se nikdy neproloží jiným příkazem.
Čekající příkazy se řadí podle priority zdroje, novější příkaz se stejným
klíčem nahradí starší čekající a frekvence odesílání je omezena.

Arbitr nic neví o LG ani POER – vykonává úlohy (``JobFactory``), které
sestavuje modul ``device_jobs``.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class CommandSource(StrEnum):
    """Původce příkazu – určuje prioritu a zda obchází omezení frekvence."""

    EMERGENCY = "emergency"
    MANUAL = "manual"
    OVERRIDE = "override"
    SCHEDULE = "schedule"
    AUTOMATION = "automation"


SOURCE_PRIORITY: dict[CommandSource, int] = {
    CommandSource.EMERGENCY: 3,
    CommandSource.MANUAL: 2,
    CommandSource.OVERRIDE: 2,
    CommandSource.SCHEDULE: 1,
    CommandSource.AUTOMATION: 1,
}

# Na tyto zdroje čeká člověk nebo bezpečnost domu – nesmí čekat na limit frekvence.
RATE_LIMIT_BYPASS = {CommandSource.EMERGENCY, CommandSource.MANUAL, CommandSource.OVERRIDE}


@dataclass(frozen=True)
class CommandOutcome:
    """
    Výsledek jedné úlohy.

    Args:
        sent:        True pokud úloha skutečně odeslala příkaz zařízení
        skip_reason: Důvod, proč úloha nic neodeslala (stav už odpovídá apod.)
        steps:       Výsledky provedených kroků
    """

    sent: bool
    skip_reason: str | None = None
    steps: list[dict] = field(default_factory=list)


class CommandSuperseded(Exception):
    """Příkaz nebyl proveden – nahradil ho novější/důležitější příkaz nebo byl arbitr ukončen."""


@dataclass(frozen=True)
class DeviceTiming:
    """
    Časování pro jeden druh zařízení.

    Args:
        min_interval_s: Minimální odstup mezi odeslanými příkazy (s)
        max_attempts:   Počet pokusů o úlohu (1 = bez opakování)
        backoff_base_s: Prodleva před 2. pokusem; každý další pokus ji zdvojnásobí
    """

    min_interval_s: float
    max_attempts: int
    backoff_base_s: float


@dataclass(frozen=True)
class ArbiterConfig:
    """
    Konfigurace arbitra.

    Args:
        timings:                    Časování podle druhu zařízení (prefix klíče před ``:``)
        default_timing:             Časování pro neznámý druh zařízení
        unavailable_after_failures: Po kolika po sobě jdoucích selháních je zařízení nedostupné
    """

    timings: dict[str, DeviceTiming]
    default_timing: DeviceTiming
    unavailable_after_failures: int = 3

    def timing_for(self, device_key: str) -> DeviceTiming:
        """
        Vrátí časování pro zařízení podle prefixu jeho klíče.

        Args:
            device_key: Klíč zařízení, např. ``"lg:<id>"``

        Returns:
            DeviceTiming: Časování druhu zařízení nebo výchozí
        """
        kind = device_key.split(":", 1)[0]
        return self.timings.get(kind, self.default_timing)


# Výchozí časování – zde se ladí odstupy příkazů a počty pokusů.
DEFAULT_CONFIG = ArbiterConfig(
    timings={
        # ThinQAPI opakuje požadavky samo uvnitř – arbitr už další pokusy nepřidává.
        "lg": DeviceTiming(min_interval_s=30.0, max_attempts=1, backoff_base_s=0.0),
        "poer": DeviceTiming(min_interval_s=120.0, max_attempts=3, backoff_base_s=2.0),
    },
    default_timing=DeviceTiming(min_interval_s=30.0, max_attempts=1, backoff_base_s=0.0),
)

JobFactory = Callable[[], Awaitable[CommandOutcome]]


@dataclass(frozen=True)
class CommandRequest:
    """
    Požadavek na vykonání úlohy na zařízení.

    Args:
        device_key: Klíč zařízení (``"lg:<id>"``, ``"poer:<id>"``)
        key:        Klíč pro nahrazování – čekající příkaz se stejným klíčem se nahradí
        source:     Původce příkazu (priorita, limit frekvence)
        run:        Úloha; stav zařízení čte až při spuštění
    """

    device_key: str
    key: str
    source: CommandSource
    run: JobFactory


@dataclass
class DeviceHealth:
    """
    Zdraví zařízení z pohledu arbitra.

    Args:
        available:            False po ``unavailable_after_failures`` selháních za sebou
        consecutive_failures: Počet po sobě jdoucích selhaných úloh
        last_error:           Text poslední chyby
    """

    available: bool = True
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass
class _Pending:
    """Čekající požadavek s budoucím výsledkem a pořadovým číslem (FIFO v rámci priority)."""

    request: CommandRequest
    future: asyncio.Future
    seq: int


@dataclass
class _DeviceLane:
    """Fronta, worker a stav jednoho zařízení."""

    pending: list[_Pending] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    last_sent_at: float | None = None
    health: DeviceHealth = field(default_factory=DeviceHealth)
    worker: asyncio.Task | None = None


def _fail(future: asyncio.Future, exc: BaseException) -> None:
    """Nastaví výjimku do future, pokud ji volající mezitím nezrušil."""
    if not future.done():
        future.set_exception(exc)


class CommandArbiter:
    """Sériové, prioritní vykonávání příkazů pro každé zařízení zvlášť."""

    def __init__(self, config: ArbiterConfig = DEFAULT_CONFIG) -> None:
        """
        Args:
            config: Konfigurace časování a prahů
        """
        self._config = config
        self._lanes: dict[str, _DeviceLane] = {}
        self._seq = 0
        self._closed = False

    async def submit(self, request: CommandRequest) -> CommandOutcome:
        """
        Zařadí požadavek a počká na jeho výsledek.

        Args:
            request: Požadavek s úlohou

        Returns:
            CommandOutcome: Výsledek úlohy

        Raises:
            CommandSuperseded: Požadavek nahradil jiný nebo byl arbitr ukončen
            RuntimeError:      Arbitr je už ukončen
            Exception:         Poslední chyba úlohy po vyčerpání pokusů
        """
        if self._closed:
            raise RuntimeError("Arbitr příkazů je ukončen.")
        lane = self._lane(request.device_key)
        future = asyncio.get_running_loop().create_future()
        self._enqueue(lane, request, future)
        return await future

    def health(self, device_key: str) -> DeviceHealth:
        """
        Vrátí zdraví zařízení.

        Args:
            device_key: Klíč zařízení

        Returns:
            DeviceHealth: Zdraví; zařízení bez historie je dostupné
        """
        lane = self._lanes.get(device_key)
        return lane.health if lane else DeviceHealth()

    async def close(self) -> None:
        """Ukončí workery a všem čekajícím požadavkům vrátí ``CommandSuperseded``."""
        self._closed = True
        workers = []
        for lane in self._lanes.values():
            for item in lane.pending:
                _fail(item.future, CommandSuperseded("Arbitr příkazů byl ukončen."))
            lane.pending.clear()
            if lane.worker is not None:
                lane.worker.cancel()
                workers.append(lane.worker)
        await asyncio.gather(*workers, return_exceptions=True)

    def _lane(self, device_key: str) -> _DeviceLane:
        """Vrátí frontu zařízení, při prvním použití ji vytvoří i s workerem."""
        lane = self._lanes.get(device_key)
        if lane is None:
            lane = _DeviceLane()
            lane.worker = asyncio.create_task(
                self._worker(device_key, lane), name=f"arbiter:{device_key}"
            )
            self._lanes[device_key] = lane
        return lane

    def _enqueue(self, lane: _DeviceLane, request: CommandRequest, future: asyncio.Future) -> None:
        """
        Zařadí požadavek; čekající se stejným klíčem nahradí, pokud nemá vyšší prioritu.

        Args:
            lane:    Fronta zařízení
            request: Nový požadavek
            future:  Future, do které se zapíše výsledek
        """
        new_priority = SOURCE_PRIORITY[request.source]
        for existing in list(lane.pending):
            if existing.request.key != request.key or existing.future.done():
                continue
            if SOURCE_PRIORITY[existing.request.source] > new_priority:
                _fail(future, CommandSuperseded(
                    f"Čeká důležitější příkaz '{request.key}' ({existing.request.source})."
                ))
                return
            lane.pending.remove(existing)
            _fail(existing.future, CommandSuperseded(
                f"Nahrazen novějším příkazem '{request.key}' ({request.source})."
            ))
        self._seq += 1
        lane.pending.append(_Pending(request, future, self._seq))
        lane.wakeup.set()

    async def _worker(self, device_key: str, lane: _DeviceLane) -> None:
        """
        Smyčka zařízení: vybírá nejdůležitější čekající požadavek a vykonává ho.

        Při čekání na limit frekvence se po každém novém požadavku vybírá znovu –
        mezitím mohl přijít důležitější nebo novější příkaz.
        """
        timing = self._config.timing_for(device_key)
        while True:
            lane.pending = [p for p in lane.pending if not p.future.done()]
            if not lane.pending:
                lane.wakeup.clear()
                await lane.wakeup.wait()
                continue
            item = max(lane.pending, key=lambda p: (SOURCE_PRIORITY[p.request.source], -p.seq))
            wait_s = self._rate_limit_wait(lane, timing, item.request.source)
            if wait_s > 0:
                lane.wakeup.clear()
                try:
                    await asyncio.wait_for(lane.wakeup.wait(), timeout=wait_s)
                except TimeoutError:
                    pass
                continue
            lane.pending.remove(item)
            await self._execute(lane, timing, item)

    @staticmethod
    def _rate_limit_wait(lane: _DeviceLane, timing: DeviceTiming, source: CommandSource) -> float:
        """Vrátí, kolik sekund ještě musí požadavek čekat kvůli limitu frekvence."""
        if source in RATE_LIMIT_BYPASS or lane.last_sent_at is None:
            return 0.0
        return max(0.0, lane.last_sent_at + timing.min_interval_s - time.monotonic())

    async def _execute(self, lane: _DeviceLane, timing: DeviceTiming, item: _Pending) -> None:
        """
        Vykoná úlohu s opakováním a zapíše výsledek do future.

        Args:
            lane:   Fronta zařízení (zdraví, čas posledního odeslání)
            timing: Časování druhu zařízení
            item:   Vykonávaný požadavek
        """
        try:
            outcome = await self._run_with_retries(item.request, timing)
        except asyncio.CancelledError:
            # Ukončení arbitra během úlohy nebo prodlevy – volající nesmí zůstat viset.
            _fail(item.future, CommandSuperseded("Arbitr příkazů byl ukončen."))
            raise
        except Exception as exc:
            # Neúspěšná úloha mohla část kroků odeslat – počítá se do limitu frekvence.
            lane.last_sent_at = time.monotonic()
            self._record_failure(item.request.device_key, lane, exc)
            _fail(item.future, exc)
            return
        if outcome.sent:
            lane.last_sent_at = time.monotonic()
        self._record_success(item.request.device_key, lane)
        if not item.future.done():
            item.future.set_result(outcome)

    @staticmethod
    async def _run_with_retries(request: CommandRequest, timing: DeviceTiming) -> CommandOutcome:
        """
        Spustí úlohu; při výjimce ji zopakuje s rostoucí prodlevou.

        Args:
            request: Požadavek s úlohou
            timing:  Počet pokusů a základní prodleva

        Returns:
            CommandOutcome: Výsledek prvního úspěšného pokusu

        Raises:
            Exception: Chyba posledního pokusu
        """
        attempts = max(1, timing.max_attempts)
        for attempt in range(1, attempts):
            try:
                return await request.run()
            except Exception:
                await asyncio.sleep(timing.backoff_base_s * 2 ** (attempt - 1))
        return await request.run()

    def _record_failure(self, device_key: str, lane: _DeviceLane, exc: Exception) -> None:
        """Započítá selhání; po překročení prahu označí zařízení jako nedostupné."""
        health = lane.health
        health.consecutive_failures += 1
        health.last_error = str(exc)
        if health.available and health.consecutive_failures >= self._config.unavailable_after_failures:
            health.available = False
            logger.warning("⚠️ Arbitr: zařízení %s je nedostupné (%s)", device_key, exc)

    @staticmethod
    def _record_success(device_key: str, lane: _DeviceLane) -> None:
        """Vynuluje počítadlo selhání a případně ohlásí obnovení zařízení."""
        if not lane.health.available:
            logger.info("✅ Arbitr: zařízení %s je opět dostupné", device_key)
        lane.health = DeviceHealth()
```

- [ ] **Step 5: Spusť testy a ověř, že prochází**

Run: `uv run pytest tests/test_command_arbiter.py -q`
Expected: 13 passed

Run: `uv run flake8 src/command_arbiter.py` (případně `flake8 src/command_arbiter.py`)
Expected: žádné chyby.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/command_arbiter.py tests/test_command_arbiter.py
git commit -m "Přidej obecného arbitra příkazů s frontou a prioritami"
```

---

### Task 2: Úlohy pro LG a POER

**Files:**
- Create: `src/device_jobs.py`
- Test: `tests/test_device_jobs.py`

**Interfaces:**
- Consumes (Task 1): `CommandOutcome`, `JobFactory` z `command_arbiter`.
- Consumes (stávající): `build_command_plan(command, args, device_status) -> CommandPlan` (`command_policy`), `execute_plan(api, device_id, plan, status) -> list[dict]` (`command_executor`), `fetch_poer_status_cached(api_key, preferred_device_id) -> dict`, `send_poer_command(api_key, endpoint, data, preferred_device_id) -> dict` (`poer_api`).
- Produces:
  - `lg_device_key(device_id: str) -> str` → `"lg:<id>"`
  - `poer_device_key(device_id: str | None) -> str` → `"poer:<id>"` nebo `"poer:default"`
  - `LG_STATE_KEY = "state"` – klíč pro úlohy, které nastavují celý stav klimatizace (scheduler, automatika)
  - `lg_command_job(api, device_id: str, command: str, args: tuple) -> JobFactory`
  - `lg_apply_action_job(api, device_id: str, action: dict) -> JobFactory` – `action = {"mode"?, "temperature"?, "wind_strength"?}`
  - `poer_command_job(api_key: str, device_id: str | None, endpoint: str, data: dict) -> JobFactory` – `endpoint` je `"set_temp"` (`data={"temperature": float}`) nebo `"set_mode"` (`data={"mode": str, "preset": str}`)

- [ ] **Step 1: Napiš padající testy**

Vytvoř `tests/test_device_jobs.py`:

```python
"""Testy úloh pro arbitra (device_jobs.py) s falešným LG API a POER klientem."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import device_jobs  # noqa: E402
from device_jobs import (  # noqa: E402
    lg_apply_action_job,
    lg_command_job,
    lg_device_key,
    poer_command_job,
    poer_device_key,
)


def _lg_status(power: str = "POWER_ON", mode: str = "COOL") -> dict:
    """Minimální stav LG klimatizace ve formátu ThinQ API."""
    return {
        "operation": {"airConOperationMode": power},
        "airConJobMode": {"currentJobMode": mode},
        "temperature": {"targetTemperature": 24.0},
        "airFlow": {"windStrength": "AUTO"},
    }


class FakeThinQAPI:
    """Falešné ThinQ API: vrací pevný stav a zaznamenává odeslané payloady."""

    def __init__(self, status: dict) -> None:
        self.status = status
        self.sent: list[dict] = []
        self.status_reads = 0

    async def get_device_status(self, device_id: str) -> dict:
        self.status_reads += 1
        return copy.deepcopy(self.status)

    async def send_device_command(self, device_id: str, payload: dict) -> dict:
        self.sent.append(payload)
        return {"ok": True}


class DeviceKeyTests(unittest.TestCase):
    def test_keys(self) -> None:
        self.assertEqual(lg_device_key("abc"), "lg:abc")
        self.assertEqual(poer_device_key("p1"), "poer:p1")
        self.assertEqual(poer_device_key(None), "poer:default")


class LgJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_job_reads_status_at_run_time(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_OFF"))
        job = lg_command_job(api, "dev", "power_on", ())
        self.assertEqual(api.status_reads, 0)
        outcome = await job()
        self.assertEqual(api.status_reads, 1)
        self.assertTrue(outcome.sent)
        self.assertEqual([s["step"] for s in outcome.steps], ["power_on"])
        self.assertEqual(len(api.sent), 1)

    async def test_command_job_skips_noop(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_OFF"))
        outcome = await lg_command_job(api, "dev", "power_off", ())()
        self.assertFalse(outcome.sent)
        self.assertEqual(outcome.skip_reason, "Zařízení je již vypnuté.")
        self.assertEqual(api.sent, [])

    @patch("command_policy.get_temp_limits", return_value=None)
    @patch("command_policy._apply_setpoint_correction", side_effect=lambda t: t)
    async def test_apply_action_runs_mode_then_temperature(self, *_mocks) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_ON", mode="COOL"))
        with patch.object(device_jobs, "_SETTLE_SECONDS", 0):
            outcome = await lg_apply_action_job(
                api, "dev", {"mode": "HEAT", "temperature": 22.0}
            )()
        self.assertTrue(outcome.sent)
        self.assertEqual(
            [s["step"] for s in outcome.steps], ["change_mode", "set_temperature"]
        )

    async def test_apply_action_skips_when_already_on(self) -> None:
        api = FakeThinQAPI(_lg_status(power="POWER_ON"))
        with patch.object(device_jobs, "_SETTLE_SECONDS", 0):
            outcome = await lg_apply_action_job(api, "dev", {})()
        self.assertFalse(outcome.sent)
        self.assertEqual(api.sent, [])


class PoerJobTests(unittest.IsolatedAsyncioTestCase):
    def _status(self, **overrides) -> dict:
        status = {
            "target_temperature_c": 21.0,
            "mode": "heat",
            "preset": "home",
            "device_id": "p1",
            "error_text": None,
        }
        status.update(overrides)
        return status

    async def test_set_temp_skips_when_equal(self) -> None:
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command", AsyncMock()) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 21.0})()
        self.assertFalse(outcome.sent)
        send.assert_not_awaited()

    async def test_set_temp_sends_when_different(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 22.5})()
        self.assertTrue(outcome.sent)
        send.assert_awaited_once_with(
            api_key="key", endpoint="set_temp", data={"temperature": 22.5},
            preferred_device_id="p1",
        )

    async def test_set_mode_away_matches_eco_status(self) -> None:
        status = self._status(mode="heat", preset="away")
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=status)), \
             patch.object(device_jobs, "send_poer_command", AsyncMock()) as send:
            outcome = await poer_command_job(
                "key", "p1", "set_mode", {"mode": "auto", "preset": "away"}
            )()
        self.assertFalse(outcome.sent)
        send.assert_not_awaited()

    async def test_status_error_does_not_skip(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        status = self._status(target_temperature_c=None, error_text="POER sitova chyba")
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=status)), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)):
            outcome = await poer_command_job("key", None, "set_temp", {"temperature": 21.0})()
        self.assertTrue(outcome.sent)

    async def test_failed_send_raises(self) -> None:
        result = {"success": False, "device_id": "p1", "error_text": "POER command selhal: 500"}
        with patch.object(device_jobs, "fetch_poer_status_cached",
                          AsyncMock(return_value=self._status())), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)):
            with self.assertRaisesRegex(RuntimeError, "500"):
                await poer_command_job("key", "p1", "set_temp", {"temperature": 25.0})()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_device_jobs.py -q`
Expected: FAIL – `ModuleNotFoundError: No module named 'device_jobs'`

- [ ] **Step 3: Implementuj úlohy**

Vytvoř `src/device_jobs.py`:

```python
# -*- coding: utf-8 -*-
"""
Úlohy pro arbitra příkazů – co přesně se má se zařízením udělat.

Úloha si stav zařízení čte až ve chvíli spuštění, tedy když má zařízení
od arbitra výhradně pro sebe. Rozhodnutí „příkaz nic nemění“ tak vždy
vychází z čerstvého stavu, ne ze stavu přečteného před čekáním ve frontě.
"""

import asyncio
from typing import Any

from command_arbiter import CommandOutcome, JobFactory
from command_executor import execute_plan
from command_policy import build_command_plan
from poer_api import fetch_poer_status_cached, send_poer_command

# Klimatizace propisuje změnu do stavu se zpožděním – pauza před čtením nového stavu.
_SETTLE_SECONDS = 1.5

# Klíč úloh, které nastavují celý stav klimatizace (scheduler, automatika).
LG_STATE_KEY = "state"


def lg_device_key(device_id: str) -> str:
    """
    Vrátí klíč LG zařízení pro arbitra.

    Args:
        device_id: ThinQ Device ID

    Returns:
        str: ``"lg:<device_id>"``
    """
    return f"lg:{device_id}"


def poer_device_key(device_id: str | None) -> str:
    """
    Vrátí klíč POER termostatu pro arbitra.

    Args:
        device_id: POER device ID, nebo None pokud není nakonfigurováno

    Returns:
        str: ``"poer:<device_id>"`` nebo ``"poer:default"``
    """
    return f"poer:{device_id or 'default'}"


def lg_command_job(api: Any, device_id: str, command: str, args: tuple[Any, ...]) -> JobFactory:
    """
    Úloha pro jeden příkaz klimatizace přes ``build_command_plan`` → ``execute_plan``.

    Args:
        api:       Instance ``ThinQAPI``
        device_id: ThinQ Device ID
        command:   Interní název příkazu (např. ``"set_temperature"``)
        args:      Argumenty příkazu

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``
    """
    async def run() -> CommandOutcome:
        status = await api.get_device_status(device_id)
        plan = build_command_plan(command, args, status)
        if plan.should_skip:
            return CommandOutcome(sent=False, skip_reason=plan.skip_reason)
        steps = await execute_plan(api, device_id, plan, status)
        return CommandOutcome(sent=True, steps=steps)
    return run


def _action_sequence(action: dict) -> list[tuple[str, tuple[Any, ...]]]:
    """
    Převede akci plánovače/automatiky na posloupnost příkazů.

    Args:
        action: ``{"mode"?, "temperature"?, "wind_strength"?}``

    Returns:
        list: Dvojice (příkaz, argumenty); první je vždy zapnutí nebo změna módu
    """
    sequence: list[tuple[str, tuple[Any, ...]]] = []
    if action.get("mode"):
        sequence.append(("change_mode", (action["mode"],)))
    else:
        sequence.append(("power_on", ()))
    if action.get("temperature") is not None:
        sequence.append(("set_temperature", (action["temperature"],)))
    if action.get("wind_strength"):
        sequence.append(("set_wind_strength", (action["wind_strength"],)))
    return sequence


def lg_apply_action_job(api: Any, device_id: str, action: dict) -> JobFactory:
    """
    Úloha, která klimatizaci zapne a nastaví mód, teplotu a ventilátor.

    Nahrazuje dřívější ``_run_schedule_on`` v ``web/app.py``; kroky, které
    nic nemění, se přeskočí.

    Args:
        api:       Instance ``ThinQAPI``
        device_id: ThinQ Device ID
        action:    ``{"mode"?, "temperature"?, "wind_strength"?}``

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``
    """
    async def run() -> CommandOutcome:
        status = await api.get_device_status(device_id)
        steps: list[dict] = []
        for command, args in _action_sequence(action):
            plan = build_command_plan(command, args, status)
            if plan.should_skip:
                continue
            steps.extend(await execute_plan(api, device_id, plan, status))
            await asyncio.sleep(_SETTLE_SECONDS)
            status = await api.get_device_status(device_id)
        if not steps:
            return CommandOutcome(sent=False, skip_reason="Zařízení už je v požadovaném stavu.")
        return CommandOutcome(sent=True, steps=steps)
    return run


def _poer_requested_state(data: dict) -> tuple[str, str]:
    """
    Normalizuje požadovaný režim POER stejně, jako ho vrací ``fetch_poer_status``.

    Předvolba ``away`` se posílá jako ``eco`` a stav ji hlásí jako (``heat``, ``away``).

    Args:
        data: ``{"mode": str, "preset": str}``

    Returns:
        tuple[str, str]: (mode, preset)
    """
    preset = str(data.get("preset") or "home").lower()
    if preset == "away":
        return "heat", "away"
    return str(data.get("mode") or "auto").lower(), "home"


def _poer_skip_reason(status: dict, endpoint: str, data: dict) -> str | None:
    """
    Zjistí, zda POER už je v požadovaném stavu.

    Args:
        status:   Výsledek ``fetch_poer_status_cached``
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu

    Returns:
        str | None: Důvod přeskočení, nebo None pokud je třeba příkaz odeslat
    """
    if status.get("error_text"):
        return None
    if endpoint == "set_temp":
        current = status.get("target_temperature_c")
        if current is not None and abs(float(current) - float(data["temperature"])) < 0.05:
            return f"Cílová teplota POER už je {current} °C."
        return None
    if endpoint == "set_mode":
        if (status.get("mode"), status.get("preset")) == _poer_requested_state(data):
            return "Režim POER je již nastaven."
    return None


def poer_command_job(api_key: str, device_id: str | None, endpoint: str, data: dict) -> JobFactory:
    """
    Úloha pro příkaz POER termostatu.

    Args:
        api_key:   POER API klíč
        device_id: POER device ID (None = první termostat na účtu)
        endpoint:  ``"set_temp"`` nebo ``"set_mode"``
        data:      ``{"temperature": float}`` nebo ``{"mode": str, "preset": str}``

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``; při selhání cloudu vyhodí
                    ``RuntimeError`` (arbitr ji zopakuje)
    """
    async def run() -> CommandOutcome:
        status = await fetch_poer_status_cached(api_key=api_key, preferred_device_id=device_id)
        skip_reason = _poer_skip_reason(status, endpoint, data)
        if skip_reason:
            return CommandOutcome(sent=False, skip_reason=skip_reason)
        result = await send_poer_command(
            api_key=api_key, endpoint=endpoint, data=data, preferred_device_id=device_id
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error_text") or "POER příkaz selhal.")
        return CommandOutcome(sent=True, steps=[{"step": endpoint, "result": result}])
    return run
```

- [ ] **Step 4: Spusť testy a ověř, že prochází**

Run: `uv run pytest tests/test_device_jobs.py -q`
Expected: 10 passed

Run: `flake8 src/device_jobs.py`
Expected: žádné chyby.

- [ ] **Step 5: Commit**

```bash
git add src/device_jobs.py tests/test_device_jobs.py
git commit -m "Přidej úlohy pro LG a POER spouštěné arbitrem"
```

---

### Task 3: Arbitr v lifespanu, scheduleru a AUTO smyčce

**Files:**
- Modify: `src/web/app.py` (importy ř. 29–30; `_run_schedule_on`/`_run_schedule_off` ř. 77–141; docstring ř. 160–163; try blok ř. 273–292; `_scheduler_loop` ř. 527–541; lifespan ř. 635 a shutdown)

**Interfaces:**
- Consumes (Task 1): `CommandArbiter`, `CommandRequest`, `CommandSource`, `CommandSuperseded`.
- Consumes (Task 2): `lg_command_job`, `lg_apply_action_job`, `lg_device_key`, `LG_STATE_KEY`.
- Produces: `app.state.arbiter: CommandArbiter` – dostupné pro routy (Task 4); `_submit_schedule_action(arbiter, api, device_ids, action: dict | None) -> None`.

- [ ] **Step 1: Uprav importy**

V `src/web/app.py` nahraď:

```python
from command_executor import execute_plan
from command_policy import build_command_plan
```

za:

```python
from command_arbiter import CommandArbiter, CommandRequest, CommandSource, CommandSuperseded
from device_jobs import LG_STATE_KEY, lg_apply_action_job, lg_command_job, lg_device_key
```

- [ ] **Step 2: Nahraď `_run_schedule_on` a `_run_schedule_off` jedním helperem**

Smaž celé funkce `_run_schedule_on` a `_run_schedule_off` (od `async def _run_schedule_on` po konec `_run_schedule_off`, těsně před `AUTOMATION_TICK_SECONDS = 60`) a vlož místo nich:

```python
async def _submit_schedule_action(
    arbiter: CommandArbiter,
    api: ThinQAPI,
    device_ids: list[str],
    action: dict | None,
) -> None:
    """
    Odešle akci plánovače všem klimatizacím souběžně přes arbitra.

    Args:
        arbiter:    Sdílený arbitr příkazů.
        api:        Inicializovaná ThinQAPI instance.
        device_ids: ThinQ ID cílových zařízení.
        action:     ``{mode, temperature, wind_strength}`` pro time_on, ``None`` pro time_off.
    """

    async def _one(device_id: str) -> None:
        """Odešle akci jednomu zařízení a zaloguje výsledek."""
        if action is None:
            job = lg_command_job(api, device_id, "power_off", ())
        else:
            job = lg_apply_action_job(api, device_id, action)
        try:
            outcome = await arbiter.submit(CommandRequest(
                lg_device_key(device_id), LG_STATE_KEY, CommandSource.SCHEDULE, job
            ))
            logger.info(
                "✅ Plánovač: akce dokončena (%s...) %s", device_id[:8], outcome.skip_reason or ""
            )
        except CommandSuperseded as exc:
            logger.info("⏰ Plánovač: akce nahrazena (%s...): %s", device_id[:8], exc)
        except Exception as exc:
            logger.error("❌ Plánovač: chyba akce (%s...): %s", device_id[:8], exc)

    await asyncio.gather(*(_one(device_id) for device_id in device_ids))
```

- [ ] **Step 3: AUTO smyčka přes arbitra**

V docstringu `_run_thermal_regulation_for_device` nahraď:

```
    ale příkazy provádí přes stejnou webovou pipeline jako HAND scheduler
    (``_run_schedule_on`` / ``_run_schedule_off``), takže respektuje
    preconditions (power_on před change_mode) a retry v ``ThinQAPI``.
```

za:

```
    ale příkazy posílá přes arbitra (``app.state.arbiter``) stejnými úlohami
    jako HAND scheduler, takže respektuje preconditions (power_on před
    change_mode), retry v ``ThinQAPI`` a nekoliduje s ručními příkazy.
```

A nahraď blok:

```python
    try:
        if decision.action == "power_off":
            await _run_schedule_off(api, device_id)
        elif decision.action == "run" and decision.mode:
            target_temp = decision.target_temperature_c
            if target_temp is None:
                target_temp = effective_policy.target_temperature_c
            await _run_schedule_on(
                api,
                device_id,
                {
                    "mode": decision.mode,
                    "temperature": round(float(target_temp), 1),
                    "wind_strength": decision.wind_strength,
                },
            )
        else:
            return
    except Exception as exc:
        logger.error("❌ Automation: provedení PID rozhodnutí selhalo pro %s...: %s", device_id[:8], exc)
        return
```

za:

```python
    job = _job_for_decision(api, device_id, decision, effective_policy)
    if job is None:
        return

    try:
        await app.state.arbiter.submit(CommandRequest(
            lg_device_key(device_id), LG_STATE_KEY, CommandSource.AUTOMATION, job
        ))
    except CommandSuperseded as exc:
        logger.info("🤖 Automation: příkaz nahrazen (%s...): %s", device_id[:8], exc)
        return
    except Exception as exc:
        # Podpis se uloží i po selhání (stejně jako dřív) – cooldown pak brání
        # opakování každou minutu při výpadku LG cloudu a šetří limit volání API.
        logger.error("❌ Automation: provedení PID rozhodnutí selhalo pro %s...: %s", device_id[:8], exc)
        app.state.thermal_last_signature[device_id] = signature
        app.state.thermal_last_action_at[device_id] = now_local
        return
```

Těsně před `async def _run_thermal_regulation_for_device` vlož funkci (drží složitost regulační funkce pod limitem flake8):

```python
def _job_for_decision(api: ThinQAPI, device_id: str, decision, policy: ThermalControlPolicy):
    """
    Sestaví úlohu pro rozhodnutí PID regulace.

    Args:
        api:       Inicializovaná ThinQAPI instance.
        device_id: ThinQ ID cílové klimatizace.
        decision:  ``ThermalControlDecision`` z ``decide_thermal_control``.
        policy:    Efektivní politika (výchozí cílová teplota).

    Returns:
        JobFactory | None: Úloha, nebo None pokud rozhodnutí nic neodesílá.
    """
    if decision.action == "power_off":
        return lg_command_job(api, device_id, "power_off", ())
    if decision.action == "run" and decision.mode:
        target_temp = decision.target_temperature_c
        if target_temp is None:
            target_temp = policy.target_temperature_c
        return lg_apply_action_job(api, device_id, {
            "mode": decision.mode,
            "temperature": round(float(target_temp), 1),
            "wind_strength": decision.wind_strength,
        })
    return None
```

- [ ] **Step 4: Scheduler přes arbitra**

V `_scheduler_loop` nahraď:

```python
                    for device_id in device_ids:
                        await _run_schedule_on(api, device_id, action)
```

za:

```python
                    await _submit_schedule_action(app.state.arbiter, api, device_ids, action)
```

a:

```python
                    for device_id in device_ids:
                        await _run_schedule_off(api, device_id)
```

za:

```python
                    await _submit_schedule_action(app.state.arbiter, api, device_ids, None)
```

- [ ] **Step 5: Vytvoření a ukončení arbitra v lifespanu**

V lifespanu hned za řádek `app.state.control_mode = _load_control_mode()` přidej:

```python

    # Jediná brána pro příkazy zařízením – web, scheduler i automatika.
    app.state.arbiter = CommandArbiter()
```

Ve shutdown části, za posledním blokem `try: await mqtt_watchdog_task ... except asyncio.CancelledError: ...`, přidej:

```python
    await app.state.arbiter.close()
    logger.info("🔧 Arbitr příkazů ukončen")
```

- [ ] **Step 6: Ověř**

Run: `grep -n "_run_schedule_on\|_run_schedule_off\|execute_plan\|build_command_plan" src/web/app.py`
Expected: žádný výskyt.

Run: `flake8 src/web/app.py --select=E9,F`
Expected: žádné chyby (nepoužité ani nedefinované názvy).

Run: `uv run python -c "import sys; sys.path.insert(0, 'src'); import web.app"`
Expected: bez výjimky.

Run: `uv run pytest tests -q`
Expected: všechny testy PASS.

- [ ] **Step 7: Commit**

```bash
git add src/web/app.py
git commit -m "Posílej příkazy scheduleru a automatiky přes arbitra"
```

---

### Task 4: Ruční příkazy z webu přes arbitra

**Files:**
- Modify: `src/web/routes/devices.py` (za `_get_api`, ř. 21–41)
- Modify: `src/web/routes/control.py` (importy ř. 18–21, tělo `send_command` ř. 119–142)
- Modify: `src/web/routes/poer.py` (importy, oba POST endpointy)
- Test: `tests/test_web_command_routes.py`

**Interfaces:**
- Consumes (Task 1): `CommandRequest`, `CommandSource`, `CommandSuperseded`, `CommandOutcome`.
- Consumes (Task 2): `lg_command_job`, `lg_device_key`, `poer_command_job`, `poer_device_key`.
- Consumes (Task 3): `request.app.state.arbiter`.
- Produces: `_get_arbiter(request: Request) -> CommandArbiter` v `web/routes/devices.py`. Odpověď POER endpointů: `{"success": True, "skipped": bool, "skip_reason": str | None}` (frontend čte jen `r.ok` a `detail`).

- [ ] **Step 1: Napiš padající testy**

Vytvoř `tests/test_web_command_routes.py`. Testy volají funkce rout přímo s falešným `Request` (bez spouštění celé aplikace, jejíž lifespan by se připojoval k LG cloudu):

```python
"""Testy webových rout pro ruční příkazy (control.py, poer.py) přes arbitra."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi import HTTPException  # noqa: E402

from command_arbiter import CommandOutcome, CommandSource, CommandSuperseded  # noqa: E402
from web.routes import control, poer  # noqa: E402


class FakeArbiter:
    """Falešný arbitr: zaznamená požadavek a vrátí/vyhodí předem danou hodnotu."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def _request(arbiter: FakeArbiter):
    """Falešný FastAPI Request s app.state.api a app.state.arbiter."""
    state = SimpleNamespace(api=object(), api_error=None, arbiter=arbiter)
    return SimpleNamespace(app=SimpleNamespace(state=state))


class ControlRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_command_goes_through_arbiter(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True, steps=[{"step": "power_on"}]))
        body = control.CommandRequest(command="power_on", args=[])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertFalse(response.skipped)
        self.assertEqual(response.steps, [{"step": "power_on"}])
        submitted = arbiter.requests[0]
        self.assertEqual(submitted.device_key, "lg:dev1")
        self.assertEqual(submitted.key, "power_on")
        self.assertEqual(submitted.source, CommandSource.MANUAL)

    async def test_noop_returns_skipped(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=False, skip_reason="Zařízení je již zapnuté."))
        body = control.CommandRequest(command="power_on", args=[])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertTrue(response.skipped)
        self.assertEqual(response.skip_reason, "Zařízení je již zapnuté.")

    async def test_superseded_manual_command_returns_skipped(self) -> None:
        arbiter = FakeArbiter(error=CommandSuperseded("Nahrazen novějším příkazem."))
        body = control.CommandRequest(command="set_temperature", args=[22])
        response = await control.send_command("dev1", body, _request(arbiter))
        self.assertTrue(response.skipped)
        self.assertEqual(response.skip_reason, "Nahrazen novějším příkazem.")

    async def test_failure_returns_503(self) -> None:
        arbiter = FakeArbiter(error=RuntimeError("cloud nedostupný"))
        body = control.CommandRequest(command="power_on", args=[])
        with self.assertRaises(HTTPException) as ctx:
            await control.send_command("dev1", body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 503)


class PoerRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = patch.dict("os.environ", {"LG_POER_API_KEY": "eu-token"})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch.object(poer, "_load_weather_config", return_value={"poer_device_id": "p1"})
        cfg.start()
        self.addCleanup(cfg.stop)

    async def test_set_temperature_goes_through_arbiter(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerTemperatureRequest(temperature=21.5)
        result = await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(result, {"success": True, "skipped": False, "skip_reason": None})
        submitted = arbiter.requests[0]
        self.assertEqual(submitted.device_key, "poer:p1")
        self.assertEqual(submitted.key, "set_temp")
        self.assertEqual(submitted.source, CommandSource.MANUAL)

    async def test_set_mode_superseded_is_skipped(self) -> None:
        arbiter = FakeArbiter(error=CommandSuperseded("Nahrazen."))
        body = poer.PoerModeRequest(mode="heat", preset="home")
        result = await poer.set_poer_mode(body, _request(arbiter))
        self.assertEqual(result, {"success": True, "skipped": True, "skip_reason": "Nahrazen."})

    async def test_failure_returns_503(self) -> None:
        arbiter = FakeArbiter(error=RuntimeError("POER command selhal: 500"))
        body = poer.PoerTemperatureRequest(temperature=21.5)
        with self.assertRaises(HTTPException) as ctx:
            await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("500", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_web_command_routes.py -q`
Expected: FAIL – routy ještě nevolají arbitra (`arbiter.requests` prázdné / `set_poer_temperature()` nepřijímá `request`).

- [ ] **Step 3: Helper `_get_arbiter`**

V `src/web/routes/devices.py` přidej import `from command_arbiter import CommandArbiter` k ostatním importům a hned za funkci `_get_api` vlož:

```python
def _get_arbiter(request: Request) -> CommandArbiter:
    """
    Vrátí sdíleného arbitra příkazů z app.state.

    Args:
        request: FastAPI HTTP požadavek

    Returns:
        CommandArbiter: Arbitr vytvořený v lifespanu aplikace
    """
    return request.app.state.arbiter
```

- [ ] **Step 4: `control.py` přes arbitra**

Nahraď importy:

```python
from command_executor import execute_plan
from command_policy import build_command_plan
from server_api import ThinQAPI
from web.routes.devices import _get_api
```

za:

```python
from command_arbiter import CommandRequest as ArbiterRequest
from command_arbiter import CommandSource, CommandSuperseded
from device_jobs import lg_command_job, lg_device_key
from web.routes.devices import _get_api, _get_arbiter
```

(`ArbiterRequest` – v modulu už existuje Pydantic model `CommandRequest`.)

V docstringu modulu nahraď `sestaví bezpečný CommandPlan přes command_policy a provede ho přes command_executor.` za `a předá ho arbitrovi příkazů, který ho vykoná přes command_policy → command_executor.`

V docstringu `send_command` nahraď kroky `Postup:` za:

```
    Postup:
        1. Sestaví úlohu ``lg_command_job`` (stav se čte až při spuštění).
        2. Předá ji arbitrovi se zdrojem MANUAL a počká na výsledek.
        3. Nic neměnící nebo nahrazený příkaz vrátí jako ``skipped=True``.
```

a v `Raises:` nahraď řádky 503/400 za:

```
        HTTPException 503: ThinQ API nedostupné nebo selhání příkazu
        HTTPException 400: Neznámý příkaz v plánu
```

Tělo funkce (od `api = _get_api(request)` do konce) nahraď za:

```python
    api = _get_api(request)
    job = lg_command_job(api, device_id, body.command, tuple(body.args))

    try:
        outcome = await _get_arbiter(request).submit(ArbiterRequest(
            lg_device_key(device_id), body.command, CommandSource.MANUAL, job
        ))
    except CommandSuperseded as exc:
        logger.info(f"Příkaz '{body.command}' nahrazen: {exc}")
        return CommandResponse(skipped=True, skip_reason=str(exc), steps=[])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Chyba při provádění příkazu '{body.command}': {exc}")
        raise HTTPException(status_code=503, detail=f"Chyba při odesílání příkazu: {exc}")

    if not outcome.sent:
        logger.info(f"Příkaz '{body.command}' přeskočen: {outcome.skip_reason}")
        return CommandResponse(skipped=True, skip_reason=outcome.skip_reason, steps=[])
    return CommandResponse(skipped=False, skip_reason=None, steps=outcome.steps)
```

- [ ] **Step 5: `poer.py` přes arbitra**

Nahraď import:

```python
from fastapi import APIRouter, HTTPException
```

za:

```python
from fastapi import APIRouter, HTTPException, Request
```

a:

```python
from poer_api import fetch_poer_status_cached, send_poer_command
from web.routes.weather import _load_weather_config
```

za:

```python
from command_arbiter import CommandRequest, CommandSource, CommandSuperseded
from device_jobs import poer_command_job, poer_device_key
from poer_api import fetch_poer_status_cached
from web.routes.devices import _get_arbiter
from web.routes.weather import _load_weather_config
```

V docstringu modulu nahraď `Endpointy jsou oddělené od LG command pipeline, protože POER používá vlastní cloud API a vlastní sadu příkazů.` za `POER používá vlastní cloud API a sadu příkazů; příkazy jdou stejně jako u LG přes arbitra příkazů.`

Za funkci `_require_poer_api_key` přidej:

```python
async def _submit_poer_command(request: Request, endpoint: str, data: dict) -> dict:
    """
    Předá ruční POER příkaz arbitrovi a převede výsledek na odpověď API.

    Args:
        request:  FastAPI request (přístup k arbitrovi)
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu

    Returns:
        dict: ``{"success": True, "skipped": bool, "skip_reason": str | None}``

    Raises:
        HTTPException 503: POER příkaz selhal i po opakování
    """
    api_key = _require_poer_api_key()
    device_id = _resolve_preferred_device_id()
    job = poer_command_job(api_key, device_id, endpoint, data)
    try:
        outcome = await _get_arbiter(request).submit(CommandRequest(
            poer_device_key(device_id), endpoint, CommandSource.MANUAL, job
        ))
    except CommandSuperseded as exc:
        return {"success": True, "skipped": True, "skip_reason": str(exc)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "POER command selhal")
    return {"success": True, "skipped": not outcome.sent, "skip_reason": outcome.skip_reason}
```

A nahraď oba POST endpointy (`set_poer_temperature`, `set_poer_mode`) za:

```python
@router.post("/command/set-temperature", summary="Nastaví cílovou teplotu POER")
async def set_poer_temperature(body: PoerTemperatureRequest, request: Request) -> dict:
    """Nastaví cílovou teplotu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(request, "set_temp", {"temperature": body.temperature})


@router.post("/command/set-mode", summary="Nastaví režim a předvolbu POER")
async def set_poer_mode(body: PoerModeRequest, request: Request) -> dict:
    """Nastaví režim a předvolbu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(request, "set_mode", {"mode": body.mode, "preset": body.preset})
```

- [ ] **Step 6: Spusť testy a ověř, že prochází**

Run: `uv run pytest tests -q`
Expected: všechny PASS (včetně 7 nových v `test_web_command_routes.py`).

Run: `grep -rn "send_poer_command\|execute_plan" src/web`
Expected: žádný výskyt.

Run: `flake8 src/web/routes/control.py src/web/routes/poer.py src/web/routes/devices.py`
Expected: žádné chyby.

- [ ] **Step 7: Commit**

```bash
git add src/web/routes/devices.py src/web/routes/control.py src/web/routes/poer.py tests/test_web_command_routes.py
git commit -m "Posílej ruční příkazy LG a POER z webu přes arbitra"
```

---

### Task 5: CLI přes úlohu, dokumentace, ověření v běžící aplikaci

**Files:**
- Modify: `src/main.py` (importy ř. 18–19, `cli_execute_command` ř. 204–214)
- Modify: `CLAUDE.md` (sekce „Command execution pipeline“)

**Interfaces:**
- Consumes (Task 2): `lg_command_job`.

- [ ] **Step 1: CLI přes `lg_command_job`**

V `src/main.py` nahraď:

```python
from command_policy import build_command_plan
from command_executor import create_payload_for_step, apply_status_hint, execute_plan
```

za:

```python
from device_jobs import lg_command_job
```

A v `cli_execute_command` nahraď:

```python
        status = await api.get_device_status(device_id)
        plan = build_command_plan(internal_command, internal_args, status)
        if plan.should_skip:
            print(f"Příkaz přeskočen: {plan.skip_reason}")
            await api.close()
            return

        for step_result in await execute_plan(api, device_id, plan, status):
            print(f"Příkaz '{step_result['step']}' úspěšně odeslán: {step_result['result']}")
```

za:

```python
        # CLI je samostatný proces – arbitr web serveru ho koordinovat nemůže,
        # proto jen stejná úloha (čerstvý stav + command pipeline) bez fronty.
        outcome = await lg_command_job(api, device_id, internal_command, internal_args)()
        if not outcome.sent:
            print(f"Příkaz přeskočen: {outcome.skip_reason}")

        for step_result in outcome.steps:
            print(f"Příkaz '{step_result['step']}' úspěšně odeslán: {step_result['result']}")
```

Run: `flake8 src/main.py --select=E9,F63,F7,F82,F401`
Expected: žádné chyby F401/F821 (pokud flake8 hlásí jiné, dřívější problémy mimo upravené řádky, neopravuj je).

- [ ] **Step 2: Aktualizuj CLAUDE.md**

V `CLAUDE.md` nahraď celou sekci `### Command execution pipeline` (nadpis + dva číslované body + odstavec „Never bypass…“) za:

```markdown
### Command execution pipeline

Every device command in the web server (web control routes, HAND scheduler, AUTO loop) goes through the **command arbiter**:
1. The caller builds a job via `device_jobs` (`lg_command_job`, `lg_apply_action_job`, `poer_command_job`) and submits a `CommandRequest(device_key, key, source, job)` to `app.state.arbiter` (`command_arbiter.CommandArbiter`).
2. The arbiter runs jobs **serially per device**, highest source priority first (`emergency` > `manual`/`override` > `schedule`/`automation`); a pending request with the same `key` is replaced (unless it has higher priority); non-bypass sources are rate-limited per device kind (`DEFAULT_CONFIG` in `command_arbiter.py`).
3. The job reads fresh device status only when it runs, then uses `command_policy.build_command_plan` (preconditions, e.g. `change_mode` implies `power_on`; no-op → skip) and `command_executor.execute_plan`.

Never call `execute_plan` / `send_poer_command` directly from web code — that reintroduces command collisions. The CLI is a separate process and runs `lg_command_job` directly (no arbiter). Wind direction axes must still be sent as separate commands; ThinQ Connect API doesn't support setting exact louver position.
```

- [ ] **Step 3: Kompletní testy a lint**

Run: `uv run pytest tests -q`
Expected: všechny PASS.

Run: `flake8 src/ --count --select=E9,F63,F7,F82 --show-source --statistics`
Expected: `0`.

- [ ] **Step 4: Ověření v běžící aplikaci (vyžaduje uživatele)**

Tento krok posílá skutečné příkazy klimatizaci – před spuštěním požádej uživatele o souhlas.

1. Spusť `uv run python src/main.py --mode web`, otevři `http://localhost:8000`.
2. V dashboardu klikni rychle za sebou 3× na +1 °C u klimatizace → v logu serveru max. jeden odeslaný `set_temperature` pro poslední hodnotu (ostatní „přeskočen“/„nahrazen“), v UI žádná chybová hláška.
3. Zapni klimatizaci, když už je zapnutá → odpověď `skipped: true`, v logu žádné volání ThinQ API.
4. Změň cílovou teplotu POER v UI → hodnota se v POER aplikaci projeví; opakované nastavení stejné hodnoty → `skipped: true`.
5. `uv run python src/main.py --mode cli --status` → vypíše stav (CLI import funguje).

Pokud cokoli z toho selže, nepokračuj commitem a nahlas výstup.

- [ ] **Step 5: Commit**

```bash
git add src/main.py CLAUDE.md
git commit -m "Použij úlohu arbitra v CLI a zdokumentuj pipeline s arbitrem"
```
