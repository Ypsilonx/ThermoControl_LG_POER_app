# -*- coding: utf-8 -*-
"""
Sběr historie: POER periodicky, LG z MQTT zpráv, ČHMÚ při stažení předpovědi,
denní spotřeba LG jednou denně.

Chyba zdroje nebo zápisu se jen zaloguje – sběr historie nikdy nesmí
zastavit řízení.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from history.store import HistoryStore, Measurement
from poer_api import PoerApiError, fetch_poer_statuses_cached

logger = logging.getLogger(__name__)

_WEATHER_METRICS = ("cloudiness_pct", "precip_mm_h", "humidity_pct", "wind_ms")

# Spotřeba LG se stahuje až ráno – těsně po půlnoci LG součet za včerejšek ještě nemá.
_ENERGY_EARLIEST_HOUR = 6
# Jedním voláním se stahuje týden zpět, takže se dopočítají i dříve neúplné dny.
_ENERGY_DAYS_BACK = 7


def _utc_now() -> datetime:
    """Aktuální čas v UTC."""
    return datetime.now(timezone.utc)


def parse_lg_push(topic: Any, data: Any, known_ids: set[str]) -> tuple[str | None, dict | None]:
    """
    Vytáhne z MQTT zprávy LG ID klimatizace a stav.

    Podporuje tvar ThinQ Connect (``{"pushType": "DEVICE_STATUS", "deviceId", "report"}``,
    topic je společný pro klienta a ID neobsahuje) i starší ``{"event": {"push": …}}``
    s ID v topicu.

    Args:
        topic:     MQTT topic
        data:      Dekódovaný payload
        known_ids: ID sledovaných klimatizací

    Returns:
        tuple: (ID, stav), nebo (None, None) pro zprávu, která není stavem známé AC
    """
    if not isinstance(data, dict):
        return None, None
    push_type = data.get("pushType")
    if push_type is not None and push_type != "DEVICE_STATUS":
        return None, None
    device_id = data.get("deviceId")
    if device_id not in known_ids:
        topic_str = str(topic or "")
        device_id = next((d for d in known_ids if d and d in topic_str), None)
    if device_id is None:
        return None, None
    if isinstance(data.get("report"), dict):
        return device_id, data["report"]
    push = data.get("event", {}).get("push") if isinstance(data.get("event"), dict) else None
    return device_id, push if isinstance(push, dict) else data


def _merge(target: dict, fragment: dict) -> None:
    """Sloučí částečný stav LG (MQTT posílá jen změněné sekce) do známého stavu."""
    for key, value in fragment.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        elif isinstance(value, dict):
            # Kopie – zpráva se může ještě serializovat pro WebSocket klienty.
            target[key] = dict(value)
        else:
            target[key] = value


class HistoryCollector:
    """Sbírá měření ze všech zdrojů a ukládá je do ``HistoryStore``."""

    def __init__(
        self,
        store: HistoryStore,
        poer_api_key: str | None,
        poll_seconds: float = 300,
        retention_days: int = 90,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """
        Args:
            store:          Úložiště historie
            poer_api_key:   POER API klíč (None/prázdný = POER se nesbírá)
            poll_seconds:   Interval odečtu POER
            retention_days: Jak dlouho se data uchovávají
            clock:          Zdroj času v UTC (pro testy)
        """
        self.store = store
        self.poll_seconds = poll_seconds
        self._poer_api_key = poer_api_key or None
        self._retention = timedelta(days=retention_days)
        self._clock = clock
        self._lg_state: dict[str, dict] = {}
        # Zápisy LG musí jít v pořadí příchodu zpráv (OFF→ON ve stejné sekundě).
        self._lg_write_lock = asyncio.Lock()
        self._purge_day: str | None = None
        self._energy_day: str | None = None

    def _ts(self) -> str:
        """Aktuální čas jako ISO řetězec v UTC."""
        return self._clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    async def _write(self, rows: list[Measurement]) -> None:
        """Zapíše měření mimo event loop; chybu jen zaloguje."""
        try:
            await asyncio.to_thread(self.store.add_measurements, rows)
        except Exception as exc:
            logger.error("❌ Historie: zápis měření selhal: %s", exc)

    async def poll_poer(self) -> None:
        """Odečte oba POER termostaty (jedno QUERY) a uloží jejich stav."""
        if not self._poer_api_key:
            return
        try:
            statuses = await fetch_poer_statuses_cached(self._poer_api_key)
        except PoerApiError as exc:
            logger.warning("⚠️ Historie: POER nedostupný, odečet vynechán: %s", exc)
            return
        ts = self._ts()
        rows: list[Measurement] = []
        for status in statuses:
            device = status["device_id"]
            rows.append(Measurement(ts, "poer", device, "online", float(bool(status["online"]))))
            if status.get("error_text"):
                continue
            for metric, key in (("temperature_c", "current_temperature_c"),
                                ("humidity_pct", "current_humidity_pct"),
                                ("target_c", "target_temperature_c")):
                if status.get(key) is not None:
                    rows.append(Measurement(ts, "poer", device, metric, float(status[key])))
            rows.append(Measurement(ts, "poer", device, "heating",
                                    float(status.get("action") == "heating")))
            if status.get("mode"):
                rows.append(Measurement(ts, "poer", device, "mode", None, status["mode"]))
        await self._write(rows)

    async def record_lg_status(self, device_id: str, fragment: dict[str, Any]) -> None:
        """
        Uloží stav klimatizace z MQTT zprávy nebo úvodního čtení.

        ``heating`` (zapnuto v režimu HEAT) se zapisuje jen když zpráva mění
        napájení nebo režim; počítá se ze sloučeného známého stavu. Zpráva
        nečekaného tvaru se jen zaloguje (volá se z MQTT vlákna, kde by se
        výjimka ztratila).

        Args:
            device_id: ThinQ ID klimatizace
            fragment:  Celý nebo částečný stav ve formátu ThinQ API
        """
        try:
            rows = self._lg_rows(device_id, fragment)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("⚠️ Historie: neočekávaný tvar stavu LG: %s", exc)
            return
        async with self._lg_write_lock:
            await self._write(rows)

    def _lg_rows(self, device_id: str, fragment: dict[str, Any]) -> list[Measurement]:
        """Sloučí zprávu do známého stavu LG a vrátí měření k zápisu."""
        state = self._lg_state.setdefault(device_id, {})
        _merge(state, fragment)
        ts = self._ts()
        rows: list[Measurement] = []
        temperature = fragment.get("temperature") or {}
        for metric, key in (("temperature_c", "currentTemperature"),
                            ("target_c", "targetTemperature")):
            if temperature.get(key) is not None:
                rows.append(Measurement(ts, "lg", device_id, metric, float(temperature[key])))
        if "operation" in fragment or "airConJobMode" in fragment:
            power_on = state.get("operation", {}).get("airConOperationMode") == "POWER_ON"
            mode = state.get("airConJobMode", {}).get("currentJobMode")
            rows.append(Measurement(ts, "lg", device_id, "power_on", float(power_on)))
            if mode:
                rows.append(Measurement(ts, "lg", device_id, "mode", None, mode))
            rows.append(Measurement(ts, "lg", device_id, "heating",
                                    float(power_on and mode == "HEAT")))
        return rows

    async def record_initial_lg_status(self, api: Any, device_ids: list[str]) -> None:
        """
        Zapíše výchozí stav klimatizací (1 čtení LG na AC při startu sběru).

        MQTT posílá jen změny – bez výchozího stavu by historie nevěděla, zda
        klimatizace po startu serveru topí. Pokud mezitím přišla MQTT zpráva,
        je novější a výchozí čtení se zahodí.

        Args:
            api:        ThinQAPI instance
            device_ids: ThinQ ID klimatizací
        """
        for device_id in device_ids:
            if device_id in self._lg_state:
                continue
            try:
                status = await api.get_device_status(device_id)
            except Exception as exc:
                logger.warning("⚠️ Historie: výchozí stav AC %s... nelze načíst: %s",
                               device_id[:8], exc)
                continue
            if device_id not in self._lg_state:
                await self.record_lg_status(device_id, status)

    async def mark_stopped(self) -> None:
        """
        Při vypnutí serveru zapíše u klimatizací neznámý stav topení (``heating`` = None).

        Interval topení se tak ukončí v okamžiku vypnutí a nepoběží přes celý
        výpadek serveru.
        """
        ts = self._ts()
        rows = [Measurement(ts, "lg", device_id, "heating", None) for device_id in self._lg_state]
        async with self._lg_write_lock:
            await self._write(rows)

    async def record_weather(self, weather: dict[str, Any]) -> None:
        """
        Uloží aktuální venkovní hodnoty a snímek předpovědi z výsledku stažení ČHMÚ.

        Args:
            weather: Výsledek ``_fetch_weather_data`` (``current``, ``hourly``, …)
        """
        ts = self._ts()
        rows: list[Measurement] = []
        outdoor = weather.get("outdoor_current_temperature_c")
        if outdoor is not None:
            rows.append(Measurement(ts, "chmi", "chmi", "temperature_c", float(outdoor),
                                    weather.get("outdoor_current_temperature_source")))
        current = weather.get("current") or {}
        for metric in _WEATHER_METRICS:
            if current.get(metric) is not None:
                rows.append(Measurement(ts, "chmi", "chmi", metric, float(current[metric])))
        await self._write(rows)
        try:
            await asyncio.to_thread(self.store.add_forecast, ts, weather.get("hourly") or [])
        except Exception as exc:
            logger.error("❌ Historie: zápis předpovědi selhal: %s", exc)

    async def daily_maintenance(self, api: Any, device_ids: list[str]) -> None:
        """
        Denní údržba: smazání starých dat a spotřeba klimatizací.

        Spotřeba se stahuje nejdřív v 6:00 místního času a jen pokud v databázi
        chybí včerejšek (přežije restart serveru), jedním voláním na AC za týden
        zpět. Pokus se dělá jednou denně i při chybě, aby výpadek LG nevedl
        k opakovaným voláním.

        Args:
            api:        ThinQAPI instance, nebo None (spotřeba se vynechá)
            device_ids: ThinQ ID klimatizací
        """
        now = self._clock()
        local_now = now.astimezone()
        today = local_now.date().isoformat()
        if self._purge_day != today:
            self._purge_day = today
            cutoff = (now - self._retention).astimezone(timezone.utc).isoformat(timespec="seconds")
            try:
                await asyncio.to_thread(self.store.purge_before, cutoff)
            except Exception as exc:
                logger.error("❌ Historie: mazání starých dat selhalo: %s", exc)
        if api is None or self._energy_day == today or local_now.hour < _ENERGY_EARLIEST_HOUR:
            return
        self._energy_day = today
        yesterday = local_now.date() - timedelta(days=1)
        for device_id in device_ids:
            await self._store_energy(api, device_id, yesterday)

    async def _store_energy(self, api: Any, device_id: str, day) -> None:
        """Stáhne a uloží spotřebu za týden do ``day``, pokud ``day`` v DB chybí."""
        try:
            existing = await asyncio.to_thread(
                self.store.energy_rows, day.isoformat(), day.isoformat()
            )
            if any(row["device"] == device_id for row in existing):
                return
            start = day - timedelta(days=_ENERGY_DAYS_BACK - 1)
            records = await api.get_energy_usage(
                device_id, "DAILY", start.strftime("%Y%m%d"), day.strftime("%Y%m%d")
            )
            for record in records:
                used = str(record.get("usedDate") or "")
                if len(used) == 8 and record.get("energyUsage") is not None:
                    await asyncio.to_thread(
                        self.store.upsert_energy_daily, device_id,
                        f"{used[:4]}-{used[4:6]}-{used[6:]}", record["energyUsage"],
                    )
        except Exception as exc:
            logger.warning("⚠️ Historie: spotřebu LG do %s nelze načíst: %s", day, exc)

    async def run(self, get_api: Callable[[], Any],
                  get_device_ids: Callable[[], list[str]]) -> None:
        """
        Smyčka sběru: odečet POER každých ``poll_seconds`` a denní údržba.

        Args:
            get_api:        Vrací aktuální ThinQAPI instanci (nebo None)
            get_device_ids: Vrací ID sledovaných klimatizací
        """
        logger.info("📈 Sběr historie spuštěn (POER každých %.0f s)", self.poll_seconds)
        api = get_api()
        if api is not None:
            await self.record_initial_lg_status(api, get_device_ids())
        while True:
            try:
                await self.poll_poer()
                await self.daily_maintenance(get_api(), get_device_ids())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("❌ Historie: neočekávaná chyba sběru: %s", exc)
            await asyncio.sleep(self.poll_seconds)
