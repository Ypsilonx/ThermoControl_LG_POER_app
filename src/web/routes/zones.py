# -*- coding: utf-8 -*-
"""
Router: Řízení zón – režim, program, automatika, dovolená, přebití a deník.

Endpointy:
    GET    /api/control/                  – režim, stav zón a celá konfigurace
    PUT    /api/control/mode              – přepnutí režimu
    PUT    /api/control/config            – změna části konfigurace
    DELETE /api/control/override/{zona}   – zrušení přebití
    GET    /api/control/journal           – deník rozhodnutí (nejnovější první)
    GET    /api/control/zones             – nastavení zón pro editor + nalezená zařízení
    GET    /api/control/zones/proposal    – návrh zón z nalezených zařízení
    PUT    /api/control/zones             – uložení nastavení zón
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from poer_api import PoerApiError, fetch_poer_devices
from server_api import list_ac_device_ids, list_devices
from web.routes.ws import manager as ws_manager
from zones.config import read_zones_raw
from zones.discovery import device_sensors, propose_zones
from zones.loop import ZoneController

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["Řízení zón"])

# Klíče control.json, které lze měnit přes PUT /config (režim má vlastní endpoint).
_EDITABLE_KEYS = frozenset({
    "dry_run", "emergency_min_c", "sensor_max_age_min", "override_hours",
    "program", "automation", "vacation", "sources", "location",
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


def _lg_devices() -> list[dict]:
    """Klimatizace z ``devices.json`` jako ``[{"device_id", "name"}]``."""
    try:
        ac_ids = set(list_ac_device_ids())
        return [{"device_id": d["device_id"], "name": d.get("alias") or d["device_id"]}
                for d in list_devices() if d["device_id"] in ac_ids]
    except Exception as exc:
        logger.warning("⚠️ Seznam klimatizací nelze načíst: %s", exc)
        return []


async def _discover() -> tuple[list[dict], list[dict], str | None]:
    """
    Najde zařízení pro editor zón.

    Returns:
        tuple: (POER termostaty, klimatizace, varování nebo None)
    """
    warning = None
    poer: list[dict] = []
    api_key = os.getenv("LG_POER_API_KEY", "").strip()
    if api_key:
        try:
            poer = [{"device_id": d["device_id"], "name": d["name"]}
                    for d in await fetch_poer_devices(api_key)]
        except PoerApiError as exc:
            warning = f"POER termostaty nelze načíst: {exc}"
    return poer, _lg_devices(), warning


@router.get("/zones", summary="Nastavení zón pro editor")
async def get_zones_setup(request: Request) -> dict:
    """
    Vrátí uložené nastavení zón (nebo None), nalezená zařízení a čidla, která nabízejí.

    Returns:
        dict: ``{"configured", "config", "devices": {"poer", "lg"}, "available_sensors",
              "warning"}``
    """
    zones = _zones(request)
    poer, lg, warning = await _discover()
    return {
        "configured": zones.zones is not None,
        "config": read_zones_raw(zones.zones_path) if zones.zones is not None else None,
        "devices": {"poer": poer, "lg": lg},
        "available_sensors": device_sensors(poer, lg),
        "warning": warning,
    }


@router.get("/zones/proposal", summary="Návrh zón z nalezených zařízení")
async def get_zones_proposal(request: Request) -> dict:
    """
    Navrhne zóny: jedna na každý POER termostat, klimatizace do první zóny.

    Raises:
        HTTPException 404: Nebylo nalezeno žádné zařízení
    """
    poer, lg, warning = await _discover()
    try:
        return propose_zones(poer, lg)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=warning or str(exc))


@router.put("/zones", summary="Uložit nastavení zón")
async def save_zones_setup(body: dict, request: Request) -> dict:
    """
    Zvaliduje a uloží nastavení zón; řízení ho začne hned používat.

    Raises:
        HTTPException 422: Neplatné nastavení (nic se neuloží)
    """
    zones = _zones(request)
    try:
        zones.save_zones(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info("🏠 Nastavení zón uloženo: %s", ", ".join(zones.zone_ids))
    return {"configured": True, "zones": zones.zone_ids}
