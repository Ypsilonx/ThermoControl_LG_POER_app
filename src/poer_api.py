# -*- coding: utf-8 -*-
"""Lehky klient pro POER cloud API bez zavislosti na Home Assistantu.

Modul slouzi pro ziskani aktualniho stavu termostatu POER a pro zapisove
ovladani zakladnich pointu: cilova teplota, hvac mode a preset.
Autentizace pouziva API key z mobilni POER aplikace.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

_POER_CN_URL = "https://open2.poersmart.com"
_POER_EU_URL = "https://open.poersmart.com"

# Krátkodobá cache pro čtecí dotazy (GET /api/poer/status, GET /api/weather/config).
# POER cloud API nemá zdokumentovaný rate limit, ale opakované SYNC+QUERY
# volání při každém načtení stránky/tabu zbytečně zatěžují cizí server –
# proto se stav krátce cachuje a sdílí mezi oběma endpointy.
_STATUS_CACHE_TTL_S = 20.0
_status_cache: dict[str, tuple[float, Any]] = {}
_status_cache_lock = asyncio.Lock()

# Seznam termostatů se prakticky nemění – SYNC stačí jednou za hodinu.
_DEVICES_CACHE_TTL_S = 3600.0
_devices_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class PoerApiError(Exception):
    """Chyba komunikace s POER cloudem (neplatný klíč, síť, HTTP status)."""


def _resolve_poer_endpoint_and_token(api_key: str) -> tuple[str, str] | None:
    """Rozhodne endpoint a token podle prefixu API klice."""

    key = str(api_key or "").strip()
    if len(key) < 3:
        return None

    prefix = key[:2].lower()
    token = key[2:]
    if not token:
        return None

    if prefix == "cn":
        return _POER_CN_URL, token
    if prefix == "eu":
        return _POER_EU_URL, token
    return None


def _build_poer_headers(token: str) -> dict[str, str]:
    """Vytvori standardni hlavičky pro POER requesty."""

    return {
        "Authorization": f"beer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _to_float(value: Any) -> float | None:
    """Převede hodnotu na float, jinak vrátí None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_status(error_text: str, device_id: str | None = None) -> dict[str, Any]:
    """Vrátí stav termostatu bez dat s chybovou zprávou."""
    return {
        "current_temperature_c": None,
        "current_humidity_pct": None,
        "target_temperature_c": None,
        "device_id": device_id,
        "name": None,
        "online": False,
        "mode": None,
        "preset": None,
        "action": None,
        "min_temp_c": None,
        "max_temp_c": None,
        "error_text": error_text,
    }


async def _post_ha(
    api_key: str,
    payload: dict[str, Any],
    session: aiohttp.ClientSession | None = None,
) -> Any:
    """
    Odešle požadavek na POER Home-Assistant endpoint a vrátí JSON odpověď.

    Args:
        api_key: POER API klíč (prefix cn/eu + token)
        payload: Tělo požadavku (SYNC / QUERY / EXECUTE)
        session: Volitelná sdílená aiohttp session

    Returns:
        Any: Dekódovaná JSON odpověď

    Raises:
        PoerApiError: Neplatný klíč, síťová chyba nebo HTTP status různý od 200
    """
    resolved = _resolve_poer_endpoint_and_token(api_key)
    if resolved is None:
        raise PoerApiError("Neplatny POER API key (chybi prefix cn/eu nebo token).")
    api_url, token = resolved
    url = f"{api_url.rstrip('/')}/speaker/ha/v1.0"
    intent = payload["inputs"][0]["intent"].rsplit(".", 1)[-1]

    own_session = session is None
    client = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    try:
        async with client.post(url, json=payload, headers=_build_poer_headers(token)) as response:
            if response.status != 200:
                text = await response.text()
                raise PoerApiError(f"POER {intent} selhal: {response.status} {text}")
            return await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise PoerApiError(f"POER sitova chyba: {exc}") from exc
    except asyncio.TimeoutError as exc:
        # aiohttp při vypršení celkového timeoutu nevyhazuje ClientError.
        raise PoerApiError("POER cloud neodpověděl včas.") from exc
    except ValueError as exc:
        raise PoerApiError(f"POER vrátil neplatnou odpověď: {exc}") from exc
    finally:
        if own_session:
            await client.close()


def _parse_sync_devices(sync_data: Any) -> list[dict[str, Any]]:
    """
    Vytáhne termostaty z odpovědi SYNC.

    Args:
        sync_data: JSON odpověď SYNC

    Returns:
        list[dict]: ``{"device_id", "name", "min_temp_c", "max_temp_c"}`` pro každý termostat
    """
    payload = sync_data.get("payload", {}) if isinstance(sync_data, dict) else {}
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    result = []
    for item in devices if isinstance(devices, list) else []:
        device_id = str(item.get("id") or "").strip()
        if not device_id:
            continue
        name_info = item.get("name")
        name = name_info.get("name") if isinstance(name_info, dict) else None
        attributes = item.get("attributes") or {}
        temp_range = attributes.get("thermostatTemperatureRange") or {}
        result.append({
            "device_id": device_id,
            "name": name or device_id,
            "min_temp_c": _to_float(temp_range.get("minThresholdCelsius")),
            "max_temp_c": _to_float(temp_range.get("maxThresholdCelsius")),
        })
    return result


def _parse_device_status(device: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """
    Sestaví stav jednoho termostatu ze záznamu SYNC a stavu z QUERY.

    Režim ``eco`` se hlásí jako (``heat``, ``away``) – stejně jako dřív.

    Args:
        device: Položka z ``_parse_sync_devices``
        state:  Stav zařízení z QUERY (``payload.devices[<id>]``)

    Returns:
        dict: Stav termostatu (viz ``_empty_status``)
    """
    online = bool(state.get("online", True))
    current_temperature_c = _to_float(state.get("thermostatTemperatureAmbient"))
    mode = str(state.get("thermostatMode") or "").strip() or None
    preset = "home"
    if mode == "eco":
        mode, preset = "heat", "away"

    error_text = None
    if not online:
        error_text = f"POER termostat {device['name']} je offline."
    elif current_temperature_c is None:
        error_text = "POER nevratil thermostatTemperatureAmbient."

    return {
        "current_temperature_c": current_temperature_c,
        "current_humidity_pct": _to_float(state.get("thermostatHumidityAmbient")),
        "target_temperature_c": _to_float(state.get("thermostatTemperatureSetpoint")),
        "device_id": device["device_id"],
        "name": device["name"],
        "online": online,
        "mode": mode,
        "preset": preset,
        "action": str(state.get("thermostatAction") or "").strip() or None,
        "min_temp_c": device["min_temp_c"],
        "max_temp_c": device["max_temp_c"],
        "error_text": error_text,
    }


async def fetch_poer_devices(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
    ttl_seconds: float = _DEVICES_CACHE_TTL_S,
) -> list[dict[str, Any]]:
    """
    Vrátí seznam termostatů na účtu (SYNC, dlouhodobě cachováno).

    Args:
        api_key:     POER API klíč
        session:     Volitelná sdílená aiohttp session
        ttl_seconds: Platnost cache v sekundách

    Returns:
        list[dict]: ``{"device_id", "name", "min_temp_c", "max_temp_c"}``

    Raises:
        PoerApiError: Chyba komunikace nebo účet bez termostatů
    """
    cached = _devices_cache.get(api_key)
    if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
        return cached[1]
    sync_data = await _post_ha(
        api_key, {"requestId": "111", "inputs": [{"intent": "action.devices.SYNC"}]}, session
    )
    devices = _parse_sync_devices(sync_data)
    if not devices:
        raise PoerApiError("POER nevratil zadna zarizeni.")
    _devices_cache[api_key] = (time.monotonic(), devices)
    return devices


async def fetch_poer_statuses(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """
    Načte stav všech termostatů jedním QUERY.

    Args:
        api_key: POER API klíč
        session: Volitelná sdílená aiohttp session

    Returns:
        list[dict]: Stav každého termostatu v pořadí ze SYNC

    Raises:
        PoerApiError: Chyba komunikace
    """
    devices = await fetch_poer_devices(api_key, session)
    query = {
        "requestId": "112",
        "inputs": [{
            "intent": "action.devices.QUERY",
            "payload": {"devices": [{"id": d["device_id"]} for d in devices]},
        }],
    }
    status_data = await _post_ha(api_key, query, session)
    payload = status_data.get("payload", {}) if isinstance(status_data, dict) else {}
    states = payload.get("devices", {}) if isinstance(payload, dict) else {}
    if not isinstance(states, dict):
        states = {}
    return [_parse_device_status(d, states.get(d["device_id"]) or {}) for d in devices]


async def fetch_poer_status(
    api_key: str,
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """
    Načte stav jednoho termostatu (preferovaného, jinak prvního na účtu).

    Args:
        api_key:             POER API klíč
        preferred_device_id: Volitelné ID termostatu
        session:             Volitelná sdílená aiohttp session

    Returns:
        dict: Stav termostatu; chyby se vrací v ``error_text`` (nikdy nevyhazuje)
    """
    try:
        statuses = await fetch_poer_statuses(api_key, session)
    except PoerApiError as exc:
        return _empty_status(str(exc))
    except Exception as exc:
        return _empty_status(f"POER neocekavana chyba: {exc}")
    for status in statuses:
        if preferred_device_id and status["device_id"] == str(preferred_device_id):
            return status
    return statuses[0]


async def fetch_poer_status_cached(
    api_key: str,
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
    ttl_seconds: float = _STATUS_CACHE_TTL_S,
) -> dict[str, Any]:
    """Načte stav POER termostatu s krátkodobou cache a jedním retry pokusem.

    Určeno pro čtecí endpointy volané z prohlížeče (dashboard, automatizace),
    kde více téměř současných požadavků (např. ``loadPoerStatus`` +
    ``loadWeatherConfig`` po odeslání příkazu, nebo více otevřených tabů)
    by jinak zbytečně znásobilo volání cizího cloud API. Chyby v podobě
    dočasného výpadku (síť, 5xx) se navíc jednou zopakují s krátkou
    prodlevou, než se vrátí chybový stav volajícímu.

    Args:
        api_key:              POER API klíč (prefix cn/eu + token).
        preferred_device_id:  Volitelné konkrétní zařízení.
        session:               Volitelná sdílená aiohttp session.
        ttl_seconds:           Platnost cache v sekundách.

    Returns:
        dict[str, Any]: Stejná struktura jako ``fetch_poer_status``.
    """
    cache_key = f"{api_key}:{preferred_device_id or ''}"

    async with _status_cache_lock:
        cached = _status_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
            return cached[1]

    result = await fetch_poer_status(
        api_key=api_key, preferred_device_id=preferred_device_id, session=session
    )

    if result.get("error_text") is not None:
        # Jeden retry pro přechodné výpadky (síť, 5xx) – nechceme uživateli
        # hlásit chybu kvůli jednomu zahozenému paketu.
        await asyncio.sleep(1.0)
        result = await fetch_poer_status(
            api_key=api_key, preferred_device_id=preferred_device_id, session=session
        )

    if result.get("error_text") is None:
        async with _status_cache_lock:
            _status_cache[cache_key] = (time.monotonic(), result)

    return result


async def fetch_poer_statuses_cached(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
    ttl_seconds: float = _STATUS_CACHE_TTL_S,
) -> list[dict[str, Any]]:
    """
    Stav všech termostatů s krátkodobou cache (pro dashboard).

    Args:
        api_key:     POER API klíč
        session:     Volitelná sdílená aiohttp session
        ttl_seconds: Platnost cache v sekundách

    Returns:
        list[dict]: Stav každého termostatu

    Raises:
        PoerApiError: Chyba komunikace
    """
    cache_key = f"{api_key}:*"
    async with _status_cache_lock:
        cached = _status_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
            return cached[1]
    result = await fetch_poer_statuses(api_key, session)
    async with _status_cache_lock:
        _status_cache[cache_key] = (time.monotonic(), result)
    return result


def _build_execution(endpoint: str, data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """
    Přeloží endpoint a data na ``execution`` pole EXECUTE požadavku.

    POER zpracuje z ``execution`` jen první příkaz – proto vždy jeden.

    Returns:
        list | None: Pole s jedním příkazem, nebo None pro neznámý endpoint
    """
    if endpoint == "set_temp":
        return [{
            "command": "action.devices.commands.ThermostatTemperatureSetpoint",
            "params": {"thermostatTemperatureSetpoint": data["temperature"]},
        }]
    if endpoint == "set_mode":
        mode = str(data.get("mode") or "auto").lower()
        if str(data.get("preset") or "home").lower() == "away":
            mode = "eco"
        return [{
            "command": "action.devices.commands.ThermostatSetMode",
            "params": {"thermostatMode": mode},
        }]
    return None


def _execute_error(response: Any) -> str | None:
    """Vrátí popis chyby, pokud EXECUTE odpověď hlásí jiný status než SUCCESS."""
    payload = response.get("payload", {}) if isinstance(response, dict) else {}
    for command in payload.get("commands", []) if isinstance(payload, dict) else []:
        if command.get("status") != "SUCCESS":
            return f"POER command status {command.get('status')}: {command.get('errorCode', '')}"
    return None


async def send_poer_command(
    api_key: str,
    endpoint: str,
    data: dict[str, Any],
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """
    Odešle write příkaz do POER cloudu.

    S ``preferred_device_id`` jde rovnou jeden EXECUTE požadavek; bez něj se
    zařízení nejdřív dohledá (první termostat na účtu).

    Args:
        api_key:             POER API klíč
        endpoint:            ``"set_temp"`` nebo ``"set_mode"``
        data:                ``{"temperature": float}`` nebo ``{"mode": str, "preset": str}``
        preferred_device_id: ID termostatu
        session:             Volitelná sdílená aiohttp session

    Returns:
        dict: ``{"success": bool, "device_id": str | None, "error_text": str | None}``
    """
    device_id = str(preferred_device_id) if preferred_device_id else None
    if device_id is None:
        state = await fetch_poer_status(api_key=api_key, session=session)
        device_id = state.get("device_id")
        if not device_id:
            return {
                "success": False,
                "device_id": None,
                "error_text": state.get("error_text") or "POER zarizeni neni dostupne.",
            }

    execution = _build_execution(endpoint, data)
    if execution is None:
        return {
            "success": False,
            "device_id": device_id,
            "error_text": f"Nepodporovany POER prikaz: {endpoint}",
        }

    payload = {
        "requestId": "113",
        "inputs": [{
            "intent": "action.devices.EXECUTE",
            "payload": {"commands": [{"devices": [{"id": device_id}], "execution": execution}]},
        }],
    }
    try:
        response = await _post_ha(api_key, payload, session)
    except PoerApiError as exc:
        return {"success": False, "device_id": device_id, "error_text": str(exc)}

    error_text = _execute_error(response)
    if error_text:
        return {"success": False, "device_id": device_id, "error_text": error_text}

    # Zneplatnit cache stavu (jednotlivé i souhrnné) – frontend po příkazu hned
    # načítá stav a stará hodnota by se ukazovala až 20 s.
    async with _status_cache_lock:
        _status_cache.clear()
    return {"success": True, "device_id": device_id, "error_text": None}
