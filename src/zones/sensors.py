# -*- coding: utf-8 -*-
"""
Registr čidel zón: převod dat ze zdrojů na hodnoty s časem měření
a výběr prvního čerstvého zdroje pro roli zóny.

Zdroje se nečtou zvlášť kvůli zónám – ``poer`` používá sdílenou cache stavu
termostatů, ``lg`` poslední stav z MQTT/regulace a ``chmi`` cache počasí.
Zdroj ``smart_plug`` je zatím jen rozhraní (konkrétní zásuvka později).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from zones.config import Sensor, Zone

logger = logging.getLogger(__name__)

# Veličina, kterou role zóny čte.
ROLE_METRIC = {
    "indoor_temperature": "temperature",
    "indoor_humidity": "humidity",
    "outdoor_temperature": "temperature",
    "fireplace": "on",
}

_HTTP_TIMEOUT_S = 10.0

SensorValues = dict[tuple[str, str], "Reading"]


@dataclass(frozen=True)
class Reading:
    """
    Hodnota čidla.

    Args:
        value:     Hodnota (°C, %, u binárních 1.0/0.0)
        ts:        Čas měření (UTC)
        sensor_id: ID čidla z registru
    """

    value: float
    ts: datetime
    sensor_id: str


def resolve_role(
    zone: Zone,
    role: str,
    sensors: dict[str, Sensor],
    values: SensorValues,
    now: datetime,
    default_max_age_min: float,
) -> Reading | None:
    """
    Vrátí hodnotu prvního zdroje role, který není starší než povolený limit.

    Args:
        zone:                Zóna
        role:                Role (``ROLE_METRIC``)
        sensors:             Registr čidel
        values:              Hodnoty podle (ID čidla, veličina)
        now:                 Aktuální čas (UTC)
        default_max_age_min: Limit stáří, pokud ho čidlo nemá vlastní

    Returns:
        Reading | None: Čerstvá hodnota, nebo None když žádný zdroj nevyhovuje
    """
    metric = ROLE_METRIC.get(role)
    for sensor_id in zone.roles.get(role, ()):
        reading = values.get((sensor_id, metric))
        if reading is None:
            continue
        max_age = sensors[sensor_id].max_age_min or default_max_age_min
        if now - reading.ts <= timedelta(minutes=max_age):
            return reading
    return None


def _float(value: Any) -> float | None:
    """Převede hodnotu na float, neplatnou na None."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _put(values: SensorValues, sensor_id: str, metric: str, value: Any, ts: datetime) -> None:
    """Uloží hodnotu, pokud je číselná."""
    number = _float(value)
    if number is not None:
        values[(sensor_id, metric)] = Reading(number, ts, sensor_id)


def _of_source(sensors: dict[str, Sensor], source: str) -> list[Sensor]:
    """Čidla daného zdroje."""
    return [s for s in sensors.values() if s.source == source]


def poer_values(sensors: dict[str, Sensor], statuses: list[dict], fetched_at: datetime
                ) -> SensorValues:
    """
    Hodnoty čidel ``poer`` ze stavu termostatů (``fetch_poer_statuses_cached``).

    Args:
        sensors:    Registr čidel
        statuses:   Stav všech termostatů
        fetched_at: Čas načtení stavu (UTC)

    Returns:
        SensorValues: Teplota a vlhkost online termostatů
    """
    by_id = {s.get("device_id"): s for s in statuses}
    values: SensorValues = {}
    for sensor in _of_source(sensors, "poer"):
        status = by_id.get(sensor.device_id)
        if not status or not status.get("online"):
            continue
        _put(values, sensor.id, "temperature", status.get("current_temperature_c"), fetched_at)
        _put(values, sensor.id, "humidity", status.get("current_humidity_pct"), fetched_at)
    return values


def lg_values(
    sensors: dict[str, Sensor],
    lg_statuses: dict[str, tuple[datetime, dict]],
    proxy_offset_c: float,
) -> SensorValues:
    """
    Hodnoty čidel ``lg`` z posledního známého stavu klimatizací.

    Vnitřní čidlo klimatizace měří u stropu – opravuje se o
    ``ac_indoor_temperature_proxy_offset_c`` stejně jako v regulaci.

    Args:
        sensors:        Registr čidel
        lg_statuses:    Device ID → (čas přijetí, stav)
        proxy_offset_c: Korekce vnitřního čidla AC

    Returns:
        SensorValues: Opravená vnitřní teplota
    """
    values: SensorValues = {}
    for sensor in _of_source(sensors, "lg"):
        device_id = sensor.device_id or next(iter(lg_statuses), None)
        entry = lg_statuses.get(device_id) if device_id else None
        if entry is None:
            continue
        ts, status = entry
        temp = _float((status.get("temperature") or {}).get("currentTemperature"))
        if temp is not None:
            _put(values, sensor.id, "temperature", temp + proxy_offset_c, ts)
    return values


def chmi_values(sensors: dict[str, Sensor], weather_cache: dict | None) -> SensorValues:
    """
    Hodnoty čidel ``chmi`` z cache počasí (čas = okamžik stažení).

    Args:
        sensors:       Registr čidel
        weather_cache: ``app.state.weather_cache`` nebo None

    Returns:
        SensorValues: Aktuální venkovní teplota a vlhkost
    """
    if not isinstance(weather_cache, dict) or not weather_cache.get("fetched_at"):
        return {}
    try:
        ts = datetime.fromisoformat(str(weather_cache["fetched_at"]))
    except ValueError:
        return {}
    ts = ts.astimezone(timezone.utc)
    current = weather_cache.get("current") or {}
    values: SensorValues = {}
    for sensor in _of_source(sensors, "chmi"):
        _put(values, sensor.id, "temperature", weather_cache.get("current_temperature_c"), ts)
        _put(values, sensor.id, "humidity", current.get("humidity_pct"), ts)
    return values


async def _fetch_json(url: str) -> Any:
    """Stáhne JSON z URL čidla."""
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


async def http_values(sensors: dict[str, Sensor], now: datetime) -> SensorValues:
    """
    Hodnoty čidel ``http`` (JSON ``{"temperature_c": …, "humidity_pct"?: …}``).

    Args:
        sensors: Registr čidel
        now:     Čas měření (UTC)

    Returns:
        SensorValues: Hodnoty dostupných čidel; nedostupné čidlo nemá hodnotu
    """
    values: SensorValues = {}
    for sensor in _of_source(sensors, "http"):
        try:
            payload = await _fetch_json(sensor.url)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            logger.warning("⚠️ Čidlo %s nedostupné: %s", sensor.id, exc)
            continue
        if isinstance(payload, dict):
            _put(values, sensor.id, "temperature", payload.get("temperature_c"), now)
            _put(values, sensor.id, "humidity", payload.get("humidity_pct"), now)
    return values
