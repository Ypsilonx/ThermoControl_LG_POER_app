# -*- coding: utf-8 -*-
"""
Router: Režim řízení (kompatibilita) – GET /api/mode/ + POST /api/mode/.

Dřívější přepínač HAND/AUTO. Režim je nově v řízení zón (``/api/control``);
tento endpoint ho mapuje: HAND ↔ Ručně, AUTO ↔ Automatika (Program a Dovolená
se hlásí jako AUTO).
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web.routes.zones import change_mode

router = APIRouter(prefix="/api/mode", tags=["Režim řízení"])

_LEGACY_TO_MODE = {"HAND": "manual", "AUTO": "automation"}


class ModeRequest(BaseModel):
    """Tělo požadavku pro nastavení režimu (``HAND`` nebo ``AUTO``)."""

    mode: str


@router.get("/", summary="Aktuální režim (HAND/AUTO)")
async def get_mode(request: Request) -> dict:
    """
    Vrátí režim ve starém tvaru.

    Returns:
        dict: ``{"mode": "AUTO" | "HAND"}``
    """
    return {"mode": request.app.state.zones.legacy_mode}


@router.post("/", summary="Nastavit režim (HAND/AUTO)")
async def set_mode(body: ModeRequest, request: Request) -> dict:
    """
    Nastaví režim ve starém tvaru: HAND → Ručně, AUTO → Automatika.

    Returns:
        dict: ``{"mode": "HAND" | "AUTO", "changed": bool}``

    Raises:
        HTTPException 422: Neplatná hodnota mode
    """
    legacy = body.mode.strip().upper()
    if legacy not in _LEGACY_TO_MODE:
        raise HTTPException(status_code=422, detail="Povolené hodnoty: AUTO, HAND")
    result = await change_mode(request.app.state.zones, _LEGACY_TO_MODE[legacy])
    return {"mode": legacy, "changed": result["changed"]}
