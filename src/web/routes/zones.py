# -*- coding: utf-8 -*-
"""
Router: Řízení zón – režim, program, automatika, dovolená, přebití a deník.

Endpointy:
    GET    /api/control/                  – režim, stav zón a celá konfigurace
    PUT    /api/control/mode              – přepnutí režimu
    PUT    /api/control/config            – změna části konfigurace
    DELETE /api/control/override/{zona}   – zrušení přebití
    GET    /api/control/journal           – deník rozhodnutí (nejnovější první)
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from web.routes.ws import manager as ws_manager
from zones.loop import ZoneController

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["Řízení zón"])

# Klíče control.json, které lze měnit přes PUT /config (režim má vlastní endpoint).
_EDITABLE_KEYS = frozenset({
    "dry_run", "emergency_min_c", "sensor_max_age_min", "override_hours",
    "program", "automation", "vacation",
})


class ModeBody(BaseModel):
    """Tělo požadavku pro přepnutí režimu (``manual``/``program``/``automation``/``vacation``)."""

    mode: str


def _zones(request: Request) -> ZoneController:
    """Vrátí řízení zón z ``app.state``."""
    return request.app.state.zones


def zone_list(zones: ZoneController) -> list[dict]:
    """
    Stav zón pro UI; zóna bez prvního průchodu smyčky má jen ID a název.

    Args:
        zones: Řízení zón

    Returns:
        list[dict]: Stav každé zóny v pořadí ze ``zones.json``
    """
    if zones.zones is None:
        return []
    return [zones.zone_states.get(zone.id, {"id": zone.id, "name": zone.name})
            for zone in zones.zones.zones.values()]


@router.get("/", summary="Režim, stav zón a konfigurace řízení")
async def get_control(request: Request) -> dict:
    """Vrátí režim, příznak zkušebního provozu, stav zón a celou konfiguraci řízení."""
    zones = _zones(request)
    return {
        "mode": zones.control["mode"],
        "dry_run": zones.control["dry_run"],
        "zones_configured": zones.zones is not None,
        "zones": zone_list(zones),
        "config": zones.control,
    }


async def change_mode(zones: ZoneController, mode: str) -> dict:
    """
    Přepne režim, vyžádá nové vyhodnocení a ohlásí změnu přes WebSocket.

    Args:
        zones: Řízení zón
        mode:  Nový režim

    Returns:
        dict: ``{"mode", "previous", "changed"}``

    Raises:
        HTTPException 422: Neplatný režim nebo Dovolená bez termínu návratu
    """
    previous = zones.control["mode"]
    try:
        zones.set_mode(mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    changed = mode != previous
    if changed:
        zones.request_tick()
        logger.info("Režim přepnut: %s → %s", previous, mode)
        await ws_manager.broadcast({
            "type": "mode_change",
            "data": {"mode": mode, "previous": previous, "legacy_mode": zones.legacy_mode},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return {"mode": mode, "previous": previous, "changed": changed}


@router.put("/mode", summary="Přepnout režim řízení")
async def set_mode(body: ModeBody, request: Request) -> dict:
    """Přepne režim (``manual``/``program``/``automation``/``vacation``)."""
    return await change_mode(_zones(request), body.mode.strip().lower())


@router.put("/config", summary="Změnit konfiguraci řízení")
async def update_config(body: dict, request: Request) -> dict:
    """
    Změní část konfigurace (program, automatika, dovolená, prahy, zkušební provoz).

    Raises:
        HTTPException 422: Neznámý klíč nebo neplatná hodnota
    """
    unknown = set(body) - _EDITABLE_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"Nelze měnit: {', '.join(sorted(unknown))}")
    zones = _zones(request)
    try:
        control = zones.update_control(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    zones.request_tick()
    return control


@router.delete("/override/{zone_id}", summary="Zrušit přebití zóny")
async def cancel_override(zone_id: str, request: Request) -> dict:
    """
    Okamžitě ukončí dočasné přebití zóny.

    Raises:
        HTTPException 404: Neznámá zóna
    """
    zones = _zones(request)
    if zone_id not in zones.zone_ids:
        raise HTTPException(status_code=404, detail=f"Neznámá zóna: {zone_id}")
    zones.cancel_override(zone_id)
    zones.request_tick()
    return {"zone": zone_id, "cancelled": True}


@router.get("/journal", summary="Deník rozhodnutí")
async def get_journal(request: Request, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """Vrátí posledních ``limit`` záznamů deníku, nejnovější první."""
    journal = list(_zones(request).journal)
    return journal[::-1][:limit]
