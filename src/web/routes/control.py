# -*- coding: utf-8 -*-
"""
Router: Ovládání zařízení (POST /api/devices/{device_id}/command).

Přijme příkaz s volitelnými argumenty a předá ho arbitrovi
příkazů, který ho vykoná přes command_policy → command_executor.

Endpoint záměrně nepřijímá surový ThinQ payload – veškerá validace
a sestavení probíhá na serveru přes command_policy.build_command_plan.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from command_arbiter import CommandRequest as ArbiterRequest
from command_arbiter import ArbiterClosed, CommandSource, CommandSuperseded
from device_jobs import lg_command_job, lg_device_key
from web.routes.devices import _get_api, _get_arbiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["Ovládání"])

# Povolené interní příkazy (whitelist – brání injekci libovolného příkazu)
ALLOWED_COMMANDS = {
    "power_on",
    "power_off",
    "toggle_power",
    "change_mode",
    "set_temperature",
    "set_wind_strength",
    "set_wind_direction",
    "set_rotate_updown",
    "set_rotate_leftright",
    "set_power_save",
    "cancel_all_timers",
}


class CommandRequest(BaseModel):
    """
    Tělo POST požadavku pro odeslání příkazu klimatizaci.

    Args:
        command: Interní název příkazu (musí být v ALLOWED_COMMANDS)
        args:    Volitelné argumenty příkazu
                 - ``change_mode``     → ``["HEAT"]``
                 - ``set_temperature`` → ``[22.0]``
                 - ``set_wind_strength`` → ``["AUTO"]``
                 - ``set_wind_direction`` → ``[true, false]``
    """

    command: str
    args: list[Any] = []

    @field_validator("command")
    @classmethod
    def command_must_be_allowed(cls, v: str) -> str:
        """Zamítne příkazy mimo whitelist – chrání před nevalidovaným vstupem."""
        if v not in ALLOWED_COMMANDS:
            raise ValueError(
                f"Nepovolený příkaz '{v}'. "
                f"Povolené příkazy: {sorted(ALLOWED_COMMANDS)}"
            )
        return v


class CommandResponse(BaseModel):
    """
    Odpověď po provedení příkazu.

    Args:
        skipped:     True pokud byl příkaz přeskočen (precondition již splněna)
        skip_reason: Důvod přeskočení, jinak None
        steps:       Výsledky provedených kroků
    """

    skipped: bool
    skip_reason: str | None
    steps: list[dict]


@router.post(
    "/{device_id}/command",
    response_model=CommandResponse,
    summary="Odeslat příkaz zařízení",
    description=(
        "Sestaví bezpečný plán příkazů přes `command_policy` a provede ho. "
        "Automaticky přidá precondition kroky (např. power_on před change_mode). "
        "Pokud je požadovaný stav již aktivní, příkaz se přeskočí."
    ),
)
async def send_command(device_id: str, body: CommandRequest, request: Request):
    """
    Odešle příkaz klimatizaci.

    Postup:
        1. Sestaví úlohu ``lg_command_job`` (stav se čte až při spuštění).
        2. Předá ji arbitrovi se zdrojem MANUAL a počká na výsledek.
        3. Nic neměnící nebo nahrazený příkaz vrátí jako ``skipped=True``.

    Args:
        device_id: ThinQ Device ID
        body:      Příkaz a argumenty
        request:   FastAPI request (přístup k app.state.api)

    Returns:
        CommandResponse: Výsledek provedení příkazu

    Raises:
        HTTPException 422: Nepovolený příkaz (Pydantic validace)
        HTTPException 503: ThinQ API nedostupné, selhání příkazu nebo vypínání serveru
        HTTPException 400: Neznámý příkaz v plánu
    """
    api = _get_api(request)
    job = lg_command_job(api, device_id, body.command, tuple(body.args))

    try:
        outcome = await _get_arbiter(request).submit(ArbiterRequest(
            lg_device_key(device_id), body.command, CommandSource.MANUAL, job
        ))
    except ArbiterClosed as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except CommandSuperseded as exc:
        logger.info(f"Příkaz '{body.command}' nahrazen: {exc}")
        return CommandResponse(skipped=True, skip_reason=str(exc), steps=[])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Chyba při provádění příkazu '{body.command}': {exc}")
        raise HTTPException(status_code=503, detail=f"Chyba při odesílání příkazu: {exc}")

    if not outcome.sent:
        logger.info(f"Příkaz '{body.command}' přeskočen: {outcome.skip_reason}")
        return CommandResponse(skipped=True, skip_reason=outcome.skip_reason, steps=[])
    return CommandResponse(skipped=False, skip_reason=None, steps=outcome.steps)
