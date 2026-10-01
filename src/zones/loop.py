# -*- coding: utf-8 -*-
"""
Řídicí smyčka zón.

Každou minutu: sběr čidel → rozhodnutí zóny → deník → (mimo zkušební provoz)
setpointy POER termostatů přes arbitra a cíl pro regulaci klimatizace.
Setpoint se posílá jen při změně, takže ustálený stav nestojí žádné volání API.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from command_arbiter import CommandRequest, CommandSource, CommandSuperseded
from device_jobs import poer_command_job, poer_device_key
from poer_api import PoerApiError, fetch_poer_statuses_cached
from zones.config import ZonesConfig, load_control, load_zones, normalize_control, save_control
from zones.decide import ZoneDecision, decide_zone, override_until
from zones.sensors import (
    SensorValues,
    chmi_values,
    http_values,
    lg_values,
    poer_values,
    resolve_role,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
# Stav POER termostatů stačí obnovit jednou za 2 min (sdílená cache s dashboardem).
POER_MAX_AGE_S = 120.0
# Po selhání se stejný setpoint zkusí znovu nejdřív za tuto dobu (šetří API při výpadku).
RETRY_AFTER_FAILURE = timedelta(minutes=10)
JOURNAL_SIZE = 200
# Klíče control.json, které update_control nahrazuje celé (jinak by nešlo mazat položky).
_REPLACED_KEYS = {"program", "overrides"}


def _round_half(value: float) -> float:
    """Zaokrouhlí na 0,5 °C (krok setpointu POER)."""
    return round(value * 2) / 2


def _command_source(control: dict, decision: ZoneDecision, has_override: bool) -> CommandSource:
    """Zdroj příkazu pro arbitra podle důvodu rozhodnutí."""
    if decision.emergency:
        return CommandSource.EMERGENCY
    if has_override:
        return CommandSource.OVERRIDE
    if control["mode"] == "program":
        return CommandSource.SCHEDULE
    return CommandSource.AUTOMATION


class ZoneController:
    """Stav a řídicí smyčka zón (sdílená v ``app.state.zones``)."""

    def __init__(
        self,
        zones_path: Path,
        control_path: Path,
        state_path: Path,
        poer_api_key: str,
        arbiter: Any,
        weather_cache: Callable[[], dict | None] = lambda: None,
        proxy_offset_c: Callable[[], float] = lambda: 0.0,
        local_now: Callable[[], datetime] = datetime.now,
    ) -> None:
        """
        Args:
            zones_path:     ``data/zones.json``
            control_path:   ``data/control.json``
            state_path:     ``data/state.json`` (migrace režimu)
            poer_api_key:   POER API klíč (prázdný = bez POER)
            arbiter:        Sdílený arbitr příkazů
            weather_cache:  Vrací aktuální cache počasí (zdroj ``chmi``)
            proxy_offset_c: Vrací korekci vnitřního čidla AC (zdroj ``lg``)
            local_now:      Místní čas (pro testy)
        """
        self._zones_path = zones_path
        self._control_path = control_path
        self._state_path = state_path
        self._poer_api_key = poer_api_key
        self._arbiter = arbiter
        self._weather_cache = weather_cache
        self._proxy_offset_c = proxy_offset_c
        self._local_now = local_now
        self.zones: ZonesConfig | None = None
        self.control: dict = {}
        self.zone_states: dict[str, dict] = {}
        self.journal: deque[dict] = deque(maxlen=JOURNAL_SIZE)
        self.lg_statuses: dict[str, tuple[datetime, dict]] = {}
        self._decisions: dict[str, ZoneDecision] = {}
        self._last_journal_key: dict[str, tuple] = {}
        # Klíč zařízení → (setpoint, čas selhání nebo None)
        self._sent: dict[str, tuple[float, datetime | None]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    # ── konfigurace ──────────────────────────────────────────────

    @property
    def zone_ids(self) -> list[str]:
        """ID zón ze ``zones.json`` (prázdné, pokud soubor chybí)."""
        return list(self.zones.zones) if self.zones else []

    @property
    def legacy_mode(self) -> str:
        """
        Dřívější přepínač HAND/AUTO odvozený z režimu: Ručně → HAND, ostatní → AUTO.

        Podle něj běží HAND plánovač (jen Ručně) a regulace klimatizace (ostatní režimy).
        """
        return "HAND" if self.control.get("mode") == "manual" else "AUTO"

    def reload(self) -> None:
        """Načte ``zones.json`` a ``control.json``; neplatný zones.json zóny vypne."""
        try:
            self.zones = load_zones(self._zones_path)
        except ValueError as exc:
            logger.error("❌ Zóny: %s", exc)
            self.zones = None
        if self.zones is None:
            logger.info("ℹ️ Zóny: %s chybí – zónové řízení neběží", self._zones_path.name)
        self.control = load_control(self._control_path, self._state_path, self.zone_ids)

    def _save(self) -> None:
        """Uloží ``control.json``."""
        save_control(self._control_path, self.control)

    def update_control(self, changes: dict) -> dict:
        """
        Změní části konfigurace řízení, zvaliduje a uloží.

        Slovníky se slučují o úroveň níž (``{"vacation": {"return_at": …}}`` nemění
        ostatní pole dovolené); ``program`` a ``overrides`` se nahrazují celé.

        Args:
            changes: Např. ``{"program": {...}}`` nebo ``{"dry_run": False}``

        Returns:
            dict: Nová konfigurace

        Raises:
            ValueError: Neplatná konfigurace
        """
        merged = {**self.control}
        for key, value in changes.items():
            if (isinstance(value, dict) and isinstance(merged.get(key), dict)
                    and key not in _REPLACED_KEYS):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        dry_run_changed = merged.get("dry_run") != self.control.get("dry_run")
        self.control = normalize_control(merged, self.zone_ids)
        self._save()
        if dry_run_changed:
            # Setpointy „odeslané“ ve zkušebním provozu se ve skutečnosti neposlaly.
            self._sent.clear()
        return self.control

    def set_mode(self, mode: str) -> dict:
        """
        Přepne režim; při přechodu na Dovolenou si zapamatuje předchozí režim.

        Raises:
            ValueError: Neplatný režim nebo Dovolená bez termínu návratu
        """
        changes: dict[str, Any] = {"mode": mode}
        current = self.control.get("mode")
        if mode == "vacation" and current != "vacation":
            changes["vacation"] = {"previous_mode": current}
        return self.update_control(changes)

    def add_override(self, kind: str, device_id: str | None, device_target_c: float
                     ) -> str | None:
        """
        Založí dočasné přebití zóny po ruční změně teploty zařízení.

        Teplota zařízení se převede na cíl zóny: odečte se posun topidla (fólie −1 °C)
        a u klimatizace se setpoint AC přepočte na teplotu místnosti (korekce čidla AC).

        Args:
            kind:            ``"poer"`` nebo ``"lg"``
            device_id:       ID zařízení
            device_target_c: Teplota nastavená ručně na zařízení

        Returns:
            str | None: ID zóny s novým přebitím; None v Ručně, u zařízení mimo zóny
                        nebo když cíl vychází mimo povolený rozsah
        """
        if self.zones is None or self.control.get("mode") == "manual":
            return None
        for zone in self.zones.zones.values():
            for heater in zone.heaters:
                if heater.kind == kind and heater.device_id in (device_id, None):
                    target_c = device_target_c - heater.offset_c
                    if kind == "lg":
                        target_c += self._proxy_offset_c()
                    now = self._local_now()
                    until = override_until(self.control, now).replace(microsecond=0)
                    overrides = {**self.control["overrides"], zone.id: {
                        "target_c": target_c, "until": until.isoformat(),
                    }}
                    try:
                        self.update_control({"overrides": overrides})
                    except ValueError as exc:
                        logger.warning("⚠️ Přebití zóny %s nevzniklo: %s", zone.id, exc)
                        return None
                    if kind == "poer" and device_id:
                        # Termostat už ruční teplotu má – smyčka ji nemusí posílat znovu.
                        self._sent[poer_device_key(device_id)] = (device_target_c, None)
                    logger.info("✋ Přebití zóny %s do %s", zone.id, until)
                    self.request_tick()
                    return zone.id
        return None

    def cancel_override(self, zone_id: str) -> None:
        """Zruší přebití zóny (tlačítko Zrušit) a vynutí nové vyhodnocení setpointů."""
        overrides = {z: o for z, o in self.control["overrides"].items() if z != zone_id}
        self.update_control({"overrides": overrides})

    # ── vstupy ───────────────────────────────────────────────────

    def record_lg_status(self, device_id: str, status: dict, full: bool = False) -> None:
        """
        Zapamatuje si stav klimatizace (zdroj čidla ``lg``).

        Args:
            device_id: ThinQ Device ID
            status:    Úplný stav nebo fragment z MQTT
            full:      True = úplný stav (nahradí), False = fragment (sloučí)
        """
        previous = {} if full else self.lg_statuses.get(device_id, (None, {}))[1]
        merged = {**previous}
        for key, value in status.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        self.lg_statuses[device_id] = (datetime.now(timezone.utc), merged)

    def ac_target(self, device_id: str) -> tuple[float, bool] | None:
        """
        Cíl zóny pro regulaci klimatizace.

        Args:
            device_id: ThinQ Device ID

        Returns:
            tuple | None: (cíl °C, přeskočit regulaci – krb topí nebo chybí vnitřní
                          teplota); None = regulace jede postaru (zkušební provoz,
                          Ručně, ještě bez rozhodnutí nebo AC mimo zóny)
        """
        if self.zones is None or self.control.get("dry_run", True):
            return None
        for zone in self.zones.zones.values():
            decision = self._decisions.get(zone.id)
            if decision is None or decision.target_c is None:
                continue
            for heater in zone.heaters:
                if heater.kind == "lg" and heater.device_id in (device_id, None):
                    return decision.target_c + heater.offset_c, decision.paused or decision.hold
        return None

    async def _collect(self, now_utc: datetime) -> SensorValues:
        """Sebere hodnoty všech čidel z cache a HTTP čidel."""
        sensors = self.zones.sensors
        values: SensorValues = {}
        if self._poer_api_key and any(s.source == "poer" for s in sensors.values()):
            try:
                statuses = await fetch_poer_statuses_cached(
                    self._poer_api_key, ttl_seconds=POER_MAX_AGE_S)
                values.update(poer_values(sensors, statuses, now_utc))
            except PoerApiError as exc:
                logger.warning("⚠️ Zóny: stav POER nedostupný: %s", exc)
        values.update(lg_values(sensors, self.lg_statuses, self._proxy_offset_c()))
        values.update(chmi_values(sensors, self._weather_cache()))
        values.update(await http_values(sensors, now_utc))
        return values

    # ── smyčka ───────────────────────────────────────────────────

    def _housekeeping(self, now: datetime) -> None:
        """Ukončí prošlou dovolenou a odstraní prošlá přebití."""
        control = self.control
        if control["mode"] == "vacation":
            return_at = datetime.fromisoformat(control["vacation"]["return_at"])
            if now >= return_at:
                previous = control["vacation"]["previous_mode"]
                self.update_control({"mode": previous, "vacation": {"return_at": None}})
                self._journal_event(now, f"Konec dovolené – návrat do režimu {previous}")
        expired = [z for z, o in control["overrides"].items()
                   if datetime.fromisoformat(o["until"]) <= now]
        if expired:
            self.update_control({"overrides": {z: o for z, o in self.control["overrides"].items()
                                               if z not in expired}})

    def _journal_event(self, now: datetime, text: str) -> None:
        """Zapíše do deníku událost, která se netýká jedné zóny."""
        self.journal.append({"ts": now.isoformat(timespec="seconds"), "zone": None,
                             "target_c": None, "reason": text, "indoor_c": None,
                             "indoor_sensor": None, "actions": [],
                             "dry_run": self.control["dry_run"]})

    def _actions_for(self, zone, decision: ZoneDecision, now_utc: datetime
                     ) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
        """
        Spočítá setpointy POER topidel zóny a vybere ty, které je třeba poslat.

        Returns:
            tuple: (všechny setpointy podle klíče zařízení, [(klíč, device_id, setpoint)])
        """
        setpoints: dict[str, float] = {}
        to_send = []
        if decision.target_c is None or decision.hold:
            return setpoints, to_send
        minimum = self.control["emergency_min_c"]
        for heater in zone.heaters:
            if heater.kind != "poer" or not heater.device_id:
                continue
            key = poer_device_key(heater.device_id)
            setpoint = _round_half(max(decision.target_c + heater.offset_c, minimum))
            setpoints[key] = setpoint
            last = self._sent.get(key)
            if last is not None and last[0] == setpoint and (
                    last[1] is None or now_utc - last[1] < RETRY_AFTER_FAILURE):
                continue
            to_send.append((key, heater.device_id, setpoint))
        return setpoints, to_send

    async def _send(self, key: str, device_id: str, setpoint: float,
                    source: CommandSource) -> None:
        """Pošle setpoint přes arbitra; při selhání si poznamená čas pro pozdější opakování."""
        job = poer_command_job(self._poer_api_key, device_id, "set_temp",
                               {"temperature": setpoint})
        try:
            await self._arbiter.submit(CommandRequest(key, "set_temp", source, job))
        except CommandSuperseded as exc:
            # Přednost dostal ruční příkaz – příště se setpoint vyhodnotí znovu.
            self._sent.pop(key, None)
            logger.info("ℹ️ Zóny: setpoint %s nahrazen: %s", key, exc)
        except Exception as exc:
            self._sent[key] = (setpoint, datetime.now(timezone.utc))
            logger.error("❌ Zóny: setpoint %s → %s °C selhal: %s", key, setpoint, exc)

    def _dispatch(self, key: str, device_id: str, setpoint: float,
                  source: CommandSource) -> None:
        """Spustí odeslání na pozadí – arbitr může kvůli limitu frekvence čekat minuty."""
        self._sent[key] = (setpoint, None)
        task = asyncio.create_task(self._send(key, device_id, setpoint, source))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def request_tick(self) -> None:
        """Vyžádá okamžitý průchod smyčky (po změně režimu nebo konfigurace z UI)."""
        self._wake.set()

    async def drain(self) -> None:
        """Počká na dokončení odesílání (testy, vypínání)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _zone_state(self, zone, decision: ZoneDecision, indoor, humidity,
                    setpoints: dict[str, float]) -> dict:
        """Stav zóny pro API/UI."""
        return {
            "id": zone.id,
            "name": zone.name,
            "target_c": decision.target_c,
            "reason": decision.reason,
            "emergency": decision.emergency,
            "paused": decision.paused,
            "hold": decision.hold,
            "indoor_c": indoor.value if indoor else None,
            "indoor_sensor": indoor.sensor_id if indoor else None,
            "indoor_ts": indoor.ts.isoformat() if indoor else None,
            "humidity_pct": humidity.value if humidity else None,
            "override": self.control["overrides"].get(zone.id),
            "setpoints": setpoints,
        }

    async def tick(self) -> None:
        """Jeden průchod: čidla → rozhodnutí → deník → příkazy."""
        if self.zones is None:
            return
        now = self._local_now()
        now_utc = datetime.now(timezone.utc)
        self._housekeeping(now)
        values = await self._collect(now_utc)
        max_age = self.control["sensor_max_age_min"]
        dry_run = self.control["dry_run"]
        for zone in self.zones.zones.values():
            sensors = self.zones.sensors
            indoor = resolve_role(zone, "indoor_temperature", sensors, values, now_utc, max_age)
            humidity = resolve_role(zone, "indoor_humidity", sensors, values, now_utc, max_age)
            plug = resolve_role(zone, "fireplace", sensors, values, now_utc, max_age)
            decision = decide_zone(zone.id, self.control, indoor.value if indoor else None,
                                   bool(plug.value) if plug else None, now)
            self._decisions[zone.id] = decision
            setpoints, to_send = self._actions_for(zone, decision, now_utc)
            actions = [f"{device_id} → {sp} °C" for _, device_id, sp in to_send]
            has_override = zone.id in self.control["overrides"]
            source = _command_source(self.control, decision, has_override)
            for key, device_id, setpoint in to_send:
                if dry_run:
                    # Jen poznamenat, ať se stejná akce nezapisuje do deníku každou minutu.
                    self._sent[key] = (setpoint, None)
                else:
                    self._dispatch(key, device_id, setpoint, source)
            self.zone_states[zone.id] = self._zone_state(zone, decision, indoor, humidity,
                                                         setpoints)
            self._journal(now, zone.id, decision, indoor, actions)

    def _journal(self, now: datetime, zone_id: str, decision: ZoneDecision, indoor,
                 actions: list[str]) -> None:
        """Zapíše rozhodnutí do deníku, jen když se změnilo nebo se něco posílá."""
        key = (decision.target_c, decision.reason, decision.paused, decision.emergency)
        if not actions and self._last_journal_key.get(zone_id) == key:
            return
        self._last_journal_key[zone_id] = key
        self.journal.append({
            "ts": now.isoformat(timespec="seconds"),
            "zone": zone_id,
            "target_c": decision.target_c,
            "reason": decision.reason,
            "indoor_c": indoor.value if indoor else None,
            "indoor_sensor": indoor.sensor_id if indoor else None,
            "actions": actions,
            "dry_run": self.control["dry_run"],
        })

    async def run(self, on_update: Callable[[], Any] | None = None) -> None:
        """
        Nekonečná smyčka s periodou ``TICK_SECONDS``.

        Args:
            on_update: Volitelná korutinová funkce volaná po každém průchodu (WS broadcast)
        """
        logger.info("🏠 Řízení zón spuštěno")
        while True:
            try:
                await self.tick()
                if on_update is not None:
                    await on_update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("❌ Zóny: chyba v řídicí smyčce: %s", exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
