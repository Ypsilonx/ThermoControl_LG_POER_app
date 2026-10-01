# -*- coding: utf-8 -*-
"""Router: POER termostat (stav + základní ovládání).

POER používá vlastní cloud API a sadu příkazů; příkazy jdou stejně jako
u LG přes arbitra příkazů.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from command_arbiter import ArbiterClosed, CommandRequest, CommandSource, CommandSuperseded
from device_jobs import poer_command_job, poer_device_key
from poer_api import (
    PoerApiError,
    fetch_poer_devices,
    fetch_poer_status_cached,
    fetch_poer_statuses_cached,
)
from web.routes.devices import _get_arbiter
from web.routes.weather import _load_weather_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/poer", tags=["POER"])


class PoerModeRequest(BaseModel):
    """Tělo požadavku pro nastavení režimu POER.

    Args:
        mode: HVAC režim (`auto`, `heat`, `off`).
        preset: Předvolba (`home`, `away`).
        device_id: ID termostatu; bez něj výchozí z konfigurace.
    """

    mode: Literal["auto", "heat", "off"]
    preset: Literal["home", "away"] = "home"
    device_id: str | None = None


class PoerTemperatureRequest(BaseModel):
    """Tělo požadavku pro nastavení cílové teploty POER.

    Args:
        temperature: Cílová teplota ve °C.
        device_id: ID termostatu; bez něj výchozí z konfigurace.
    """

    temperature: float = Field(..., ge=5.0, le=35.0)
    device_id: str | None = None


def _resolve_preferred_device_id() -> str | None:
    """Vrátí preferované POER device ID z konfigurace weather."""

    weather_cfg = _load_weather_config()
    raw_device_id = weather_cfg.get("poer_device_id") if isinstance(weather_cfg, dict) else None
    if raw_device_id in (None, ""):
        return None
    return str(raw_device_id)


def _require_poer_api_key() -> str:
    """Načte POER API key z prostředí nebo vyhodí HTTP 400."""

    api_key = os.getenv("LG_POER_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Chybí LG_POER_API_KEY v prostředí.")
    return api_key


async def _resolve_device(api_key: str, requested: str | None) -> dict:
    """
    Vrátí cílový termostat ze seznamu na účtu.

    Bez ``requested`` se použije ``weather.poer_device_id``; chybí-li nebo je
    zastaralé, první termostat. Výsledné ID je tak vždy skutečné – jeden fyzický
    termostat má v arbitrovi jedinou frontu. Neznámé ``requested`` vynutí jedno
    nové načtení seznamu (je cachovaný hodinu a termostat mohl právě přibýt).

    Args:
        api_key:   POER API klíč
        requested: ID z požadavku, nebo None pro výchozí

    Returns:
        dict: Záznam termostatu (``device_id``, ``name``, ``min_temp_c``, ``max_temp_c``)

    Raises:
        HTTPException 404: Termostat ``requested`` na účtu není
        HTTPException 503: Seznam termostatů nelze načíst
    """
    try:
        devices = await fetch_poer_devices(api_key)
    except PoerApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    by_id = {d["device_id"]: d for d in devices}
    if requested and requested not in by_id:
        try:
            devices = await fetch_poer_devices(api_key, ttl_seconds=0)
        except PoerApiError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        by_id = {d["device_id"]: d for d in devices}
    if requested:
        if requested not in by_id:
            raise HTTPException(status_code=404, detail=f"Neznámý POER termostat: {requested}")
        return by_id[requested]
    configured = _resolve_preferred_device_id()
    if configured in by_id:
        return by_id[configured]
    if configured:
        logger.warning("POER termostat %s z konfigurace neexistuje, používám první.", configured)
    return devices[0]


def _check_temperature_range(device: dict, temperature: float) -> None:
    """
    Odmítne teplotu mimo rozsah termostatu dřív, než se cokoli odešle.

    Jinak by prošlo přepnutí do ``heat`` a teprve teplotu by cloud odmítl –
    termostat by zůstal v ručním režimu na své uložené ruční teplotě.

    Args:
        device:      Záznam termostatu z ``_resolve_device``
        temperature: Požadovaná teplota ve °C

    Raises:
        HTTPException 422: Teplota mimo rozsah termostatu
    """
    low, high = device.get("min_temp_c"), device.get("max_temp_c")
    if (low is not None and temperature < low) or (high is not None and temperature > high):
        raise HTTPException(
            status_code=422,
            detail=f"Teplota {temperature} °C je mimo rozsah {low}–{high} °C termostatu.",
        )


async def _submit_poer_command(
    request: Request, endpoint: str, data: dict, requested_device_id: str | None
) -> dict:
    """
    Předá ruční POER příkaz arbitrovi a převede výsledek na odpověď API.

    Args:
        request:  FastAPI request (přístup k arbitrovi)
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu
        requested_device_id: ID termostatu z požadavku

    Returns:
        dict: ``{"success": True, "skipped": bool, "skip_reason": str | None}``;
              u teploty navíc ``override_zone`` (zóna s novým přebitím nebo None)

    Raises:
        HTTPException 404: Neznámý termostat
        HTTPException 422: Teplota mimo rozsah termostatu
        HTTPException 503: POER příkaz selhal i po opakování nebo se server vypíná
    """
    api_key = _require_poer_api_key()
    device = await _resolve_device(api_key, requested_device_id)
    if endpoint == "set_temp":
        _check_temperature_range(device, data["temperature"])
    device_id = device["device_id"]
    job = poer_command_job(api_key, device_id, endpoint, data, check_noop=False)
    try:
        outcome = await _get_arbiter(request).submit(CommandRequest(
            poer_device_key(device_id), endpoint, CommandSource.MANUAL, job
        ))
    except ArbiterClosed as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except CommandSuperseded as exc:
        return {"success": True, "skipped": True, "skip_reason": str(exc)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "POER command selhal")
    response = {"success": True, "skipped": not outcome.sent, "skip_reason": outcome.skip_reason}
    zones = getattr(request.app.state, "zones", None)
    if endpoint == "set_temp" and zones is not None:
        # Ruční teplota v režimu Program/Automatika/Dovolená = dočasné přebití zóny.
        response["override_zone"] = zones.add_override("poer", device_id, data["temperature"])
    return response


@router.get("/devices", summary="Stav všech POER termostatů")
async def get_poer_devices() -> list[dict]:
    """Vrátí stav všech POER termostatů; ``is_default`` označí výchozí (krátce cachováno)."""

    api_key = _require_poer_api_key()
    try:
        statuses = await fetch_poer_statuses_cached(api_key)
    except PoerApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    default_id = (await _resolve_device(api_key, None))["device_id"]
    return [{**status, "is_default": status["device_id"] == default_id} for status in statuses]


@router.get("/status", summary="Aktuální stav POER termostatu")
async def get_poer_status(device_id: str | None = None) -> dict:
    """Vrátí stav jednoho POER termostatu (výchozí z konfigurace, krátce cachováno)."""

    api_key = _require_poer_api_key()
    return await fetch_poer_status_cached(
        api_key=api_key,
        preferred_device_id=(await _resolve_device(api_key, device_id))["device_id"],
    )


@router.post("/command/set-temperature", summary="Nastaví cílovou teplotu POER")
async def set_poer_temperature(body: PoerTemperatureRequest, request: Request) -> dict:
    """Nastaví cílovou teplotu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(
        request, "set_temp", {"temperature": body.temperature}, body.device_id
    )


@router.post("/command/set-mode", summary="Nastaví režim a předvolbu POER")
async def set_poer_mode(body: PoerModeRequest, request: Request) -> dict:
    """Nastaví režim a předvolbu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(
        request, "set_mode", {"mode": body.mode, "preset": body.preset}, body.device_id
    )
