# -*- coding: utf-8 -*-
"""Router: POER termostat (stav + základní ovládání).

POER používá vlastní cloud API a sadu příkazů; příkazy jdou stejně jako
u LG přes arbitra příkazů.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from command_arbiter import CommandRequest, CommandSource, CommandSuperseded
from device_jobs import poer_command_job, poer_device_key
from poer_api import fetch_poer_status_cached
from web.routes.devices import _get_arbiter
from web.routes.weather import _load_weather_config

router = APIRouter(prefix="/api/poer", tags=["POER"])


class PoerModeRequest(BaseModel):
    """Tělo požadavku pro nastavení režimu POER.

    Args:
        mode: HVAC režim (`auto`, `heat`, `off`).
        preset: Předvolba (`home`, `away`).
    """

    mode: Literal["auto", "heat", "off"]
    preset: Literal["home", "away"] = "home"


class PoerTemperatureRequest(BaseModel):
    """Tělo požadavku pro nastavení cílové teploty POER.

    Args:
        temperature: Cílová teplota ve °C.
    """

    temperature: float = Field(..., ge=5.0, le=35.0)


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
    job = poer_command_job(api_key, device_id, endpoint, data, check_noop=False)
    try:
        outcome = await _get_arbiter(request).submit(CommandRequest(
            poer_device_key(device_id), endpoint, CommandSource.MANUAL, job
        ))
    except CommandSuperseded as exc:
        return {"success": True, "skipped": True, "skip_reason": str(exc)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "POER command selhal")
    return {"success": True, "skipped": not outcome.sent, "skip_reason": outcome.skip_reason}


@router.get("/status", summary="Aktuální stav POER termostatu")
async def get_poer_status() -> dict:
    """Vrátí aktuální stav POER termostatu z cloud API (krátce cachováno)."""

    api_key = _require_poer_api_key()
    status = await fetch_poer_status_cached(
        api_key=api_key,
        preferred_device_id=_resolve_preferred_device_id(),
    )
    return status


@router.post("/command/set-temperature", summary="Nastaví cílovou teplotu POER")
async def set_poer_temperature(body: PoerTemperatureRequest, request: Request) -> dict:
    """Nastaví cílovou teplotu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(request, "set_temp", {"temperature": body.temperature})


@router.post("/command/set-mode", summary="Nastaví režim a předvolbu POER")
async def set_poer_mode(body: PoerModeRequest, request: Request) -> dict:
    """Nastaví režim a předvolbu POER termostatu (přes arbitra příkazů)."""

    return await _submit_poer_command(
        request, "set_mode", {"mode": body.mode, "preset": body.preset}
    )
