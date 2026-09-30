# -*- coding: utf-8 -*-
"""
Router: Zařízení (GET /api/devices, GET /api/devices/{device_id}/status).

Poskytuje:
    GET /api/devices                    – seznam zařízení z devices.json (bez API volání)
    GET /api/devices/{device_id}/status – živý stav zařízení přes ThinQ HTTP API
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from command_arbiter import CommandArbiter
from server_api import ThinQAPI, list_devices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["Zařízení"])


def _get_api(request: Request) -> ThinQAPI:
    """
    Vrátí sdílenou instanci ThinQAPI z app.state.

    Args:
        request: FastAPI HTTP požadavek

    Returns:
        ThinQAPI: Inicializovaná instance

    Raises:
        HTTPException 503: Pokud API nebylo inicializováno (chybí config.json nebo token)
    """
    api: ThinQAPI | None = request.app.state.api
    if api is None:
        error_detail = getattr(request.app.state, "api_error", "ThinQ API není k dispozici.")
        raise HTTPException(
            status_code=503,
            detail=f"ThinQ API není dostupné: {error_detail}",
        )
    return api


def _get_arbiter(request: Request) -> CommandArbiter:
    """
    Vrátí sdíleného arbitra příkazů z app.state.

    Args:
        request: FastAPI HTTP požadavek

    Returns:
        CommandArbiter: Arbitr vytvořený v lifespanu aplikace
    """
    return request.app.state.arbiter


@router.get(
    "/",
    summary="Seznam zařízení",
    description="Vrátí seznam zařízení z lokálního souboru `devices.json`. Nevyžaduje API volání.",
)
async def get_devices():
    """
    Načte a vrátí seznam zařízení z data/devices.json.

    Returns:
        list[dict]: Normalizovaný seznam zařízení (device_id, alias, device_type, model_name)

    Raises:
        HTTPException 503: Pokud soubor devices.json neexistuje nebo je poškozený
    """
    try:
        all_devices = list_devices()
        # Webové rozhraní je určeno výhradně pro klimatizace
        return [
            d for d in all_devices
            if d.get("device_type") == "DEVICE_AIR_CONDITIONER"
        ]
    except FileNotFoundError as exc:
        logger.error(f"devices.json nenalezen: {exc}")
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("Chyba při načítání zařízení")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/{device_id}/status",
    summary="Stav zařízení",
    description=(
        "Vrátí živý stav zařízení přes ThinQ HTTP API. "
        "Pro časté čtení preferujte WebSocket endpoint `/ws` (bude v Kroku 4)."
    ),
)
async def get_device_status(device_id: str, request: Request):
    """
    Načte aktuální stav zařízení přes ThinQ API.

    Args:
        device_id: Plné ThinQ Device ID
        request:   FastAPI požadavek (přístup k app.state.api)

    Returns:
        dict: Stav zařízení (operation, temperature, airConJobMode, airFlow, ...)

    Raises:
        HTTPException 503: Pokud je ThinQ API nedostupné
        HTTPException 404: Pokud zařízení neexistuje (ThinQ vrátí chybu)
    """
    api = _get_api(request)
    try:
        status = await api.get_device_status(device_id)
        return status
    except Exception as exc:
        error_str = str(exc)
        logger.warning(f"Nelze načíst stav zařízení {device_id[:8]}...: {error_str}")
        # ThinQ vrátí chybu i pro neexistující device_id – mapujeme na 404
        if "not found" in error_str.lower() or "404" in error_str:
            raise HTTPException(status_code=404, detail=f"Zařízení {device_id[:8]}... nenalezeno")
        raise HTTPException(status_code=503, detail=f"ThinQ API chyba: {error_str}")


@router.get(
    "/profile-limits",
    summary="Teplotní limity z device profilu",
    description=(
        "Vrátí povolené teplotní rozsahy (min, max °C) pro každý pracovní režim "
        "klimatizace. Data jsou čtena z ``data/device_profile.json``. "
        "Frontend je používá pro dynamické zobrazení rozsahu a validaci vstupu."
    ),
)
async def get_profile_limits() -> dict:
    """
    Načte a vrátí per-mód teplotní limity z device_profile.json.

    Returns:
        dict: Klíče jsou názvy módů (COOL, HEAT, AUTO, AIR_DRY, FAN).
              Hodnota je ``{"min": int, "max": int}`` nebo ``null`` pro FAN.
    """
    from profile_limits import get_all_temp_limits
    return get_all_temp_limits()
