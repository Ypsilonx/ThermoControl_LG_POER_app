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

# Zdroje, které se každý tik přepočítávají z aktuálního stavu. Jejich čekající
# příkaz vznikl nad starším stavem – důležitější příkaz ho proto zahodí, aby
# ho po provedení nevrátil zpět (např. automatika by znovu zapnula ručně vypnutou AC).
REEVALUATED_SOURCES = {CommandSource.AUTOMATION}


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


class ArbiterClosed(CommandSuperseded):
    """Příkaz nebyl proveden, protože se arbitr ukončuje (vypínání serveru)."""


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
            ArbiterClosed:     Arbitr je nebo byl během čekání ukončen
            CommandSuperseded: Požadavek nahradil jiný
            Exception:         Poslední chyba úlohy po vyčerpání pokusů
        """
        if self._closed:
            raise ArbiterClosed("Arbitr příkazů je ukončen.")
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
        """Ukončí workery a všem čekajícím požadavkům vrátí ``ArbiterClosed``."""
        self._closed = True
        workers = []
        for lane in self._lanes.values():
            for item in lane.pending:
                _fail(item.future, ArbiterClosed("Arbitr příkazů byl ukončen."))
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

    def _abandon_lane(self, device_key: str, lane: _DeviceLane, item: _Pending,
                      exc: Exception) -> None:
        """
        Zahodí frontu zařízení po interní chybě arbitra (worker končí).

        Rozpracovaný i čekající požadavky dostanou ``CommandSuperseded`` – jinak
        by volající visel navždy. Další ``submit`` založí frontu s novým workerem.
        Běží synchronně ve workeru, takže nový požadavek nemůže skončit v mrtvé frontě.
        """
        logger.error("❌ Arbitr: worker %s spadl: %r", device_key, exc)
        if self._lanes.get(device_key) is lane:
            del self._lanes[device_key]
        for pending in [item, *lane.pending]:
            _fail(pending.future, CommandSuperseded("Interní chyba arbitra příkazů."))
        lane.pending.clear()

    def _enqueue(self, lane: _DeviceLane, request: CommandRequest, future: asyncio.Future) -> None:
        """
        Zařadí požadavek do fronty zařízení.

        Čekající příkaz se stejným klíčem nahradí, pokud nemá vyšší prioritu.
        Čekající příkazy přepočítávaných zdrojů (automatika) s nižší prioritou
        zahodí bez ohledu na klíč.

        Args:
            lane:    Fronta zařízení
            request: Nový požadavek
            future:  Future, do které se zapíše výsledek
        """
        new_priority = SOURCE_PRIORITY[request.source]
        for existing in list(lane.pending):
            if existing.future.done():
                continue
            existing_priority = SOURCE_PRIORITY[existing.request.source]
            if existing.request.source in REEVALUATED_SOURCES and existing_priority < new_priority:
                lane.pending.remove(existing)
                _fail(existing.future, CommandSuperseded(
                    f"Zahozen kvůli důležitějšímu příkazu '{request.key}' ({request.source})."
                ))
                continue
            if existing.request.key != request.key:
                continue
            if existing_priority > new_priority:
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
            try:
                await self._execute(lane, timing, item)
            except Exception as exc:
                self._abandon_lane(device_key, lane, item, exc)
                return

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
            _fail(item.future, ArbiterClosed("Arbitr příkazů byl ukončen."))
            raise
        except ValueError as exc:
            # Neplatný příkaz/argument je chyba volajícího, ne porucha zařízení.
            _fail(item.future, exc)
            return
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

        ``ValueError`` (neplatný příkaz) se neopakuje – další pokus by dopadl stejně.

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
            except ValueError:
                raise
            except Exception:
                await asyncio.sleep(timing.backoff_base_s * 2 ** (attempt - 1))
        return await request.run()

    def _record_failure(self, device_key: str, lane: _DeviceLane, exc: Exception) -> None:
        """Započítá selhání; po překročení prahu označí zařízení jako nedostupné."""
        health = lane.health
        health.consecutive_failures += 1
        health.last_error = str(exc)
        threshold = self._config.unavailable_after_failures
        if health.available and health.consecutive_failures >= threshold:
            health.available = False
            logger.warning("⚠️ Arbitr: zařízení %s je nedostupné (%s)", device_key, exc)

    @staticmethod
    def _record_success(device_key: str, lane: _DeviceLane) -> None:
        """Vynuluje počítadlo selhání a případně ohlásí obnovení zařízení."""
        if not lane.health.available:
            logger.info("✅ Arbitr: zařízení %s je opět dostupné", device_key)
        lane.health = DeviceHealth()
