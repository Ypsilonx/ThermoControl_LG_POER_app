# -*- coding: utf-8 -*-
"""
Úlohy pro arbitra příkazů – co přesně se má se zařízením udělat.

Úloha si stav zařízení čte až ve chvíli spuštění, tedy když má zařízení
od arbitra výhradně pro sebe. Rozhodnutí „příkaz nic nemění“ tak vždy
vychází z čerstvého stavu, ne ze stavu přečteného před čekáním ve frontě.
"""

import asyncio
from typing import Any

from command_arbiter import CommandOutcome, JobFactory
from command_executor import execute_plan
from command_policy import build_command_plan
from poer_api import fetch_poer_status, send_poer_command

# Klimatizace propisuje změnu do stavu se zpožděním – pauza před čtením nového stavu.
_SETTLE_SECONDS = 1.5

# Klíč úloh, které nastavují celý stav klimatizace (scheduler, automatika).
LG_STATE_KEY = "state"


def lg_device_key(device_id: str) -> str:
    """
    Vrátí klíč LG zařízení pro arbitra.

    Args:
        device_id: ThinQ Device ID

    Returns:
        str: ``"lg:<device_id>"``
    """
    return f"lg:{device_id}"


def poer_device_key(device_id: str | None) -> str:
    """
    Vrátí klíč POER termostatu pro arbitra.

    Args:
        device_id: POER device ID, nebo None pokud není nakonfigurováno

    Returns:
        str: ``"poer:<device_id>"`` nebo ``"poer:default"``
    """
    return f"poer:{device_id or 'default'}"


def lg_command_job(api: Any, device_id: str, command: str, args: tuple[Any, ...]) -> JobFactory:
    """
    Úloha pro jeden příkaz klimatizace přes ``build_command_plan`` → ``execute_plan``.

    Args:
        api:       Instance ``ThinQAPI``
        device_id: ThinQ Device ID
        command:   Interní název příkazu (např. ``"set_temperature"``)
        args:      Argumenty příkazu

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``
    """
    async def run() -> CommandOutcome:
        status = await api.get_device_status(device_id)
        plan = build_command_plan(command, args, status)
        if plan.should_skip:
            return CommandOutcome(sent=False, skip_reason=plan.skip_reason)
        steps = await execute_plan(api, device_id, plan, status)
        return CommandOutcome(sent=True, steps=steps)
    return run


def _action_sequence(action: dict) -> list[tuple[str, tuple[Any, ...]]]:
    """
    Převede akci plánovače/automatiky na posloupnost příkazů.

    Args:
        action: ``{"mode"?, "temperature"?, "wind_strength"?}``

    Returns:
        list: Dvojice (příkaz, argumenty); první je vždy zapnutí nebo změna módu
    """
    sequence: list[tuple[str, tuple[Any, ...]]] = []
    if action.get("mode"):
        sequence.append(("change_mode", (action["mode"],)))
    else:
        sequence.append(("power_on", ()))
    if action.get("temperature") is not None:
        sequence.append(("set_temperature", (action["temperature"],)))
    if action.get("wind_strength"):
        sequence.append(("set_wind_strength", (action["wind_strength"],)))
    return sequence


def lg_apply_action_job(api: Any, device_id: str, action: dict) -> JobFactory:
    """
    Úloha, která klimatizaci zapne a nastaví mód, teplotu a ventilátor.

    Nahrazuje dřívější ``_run_schedule_on`` v ``web/app.py``; kroky, které
    nic nemění, se přeskočí.

    Args:
        api:       Instance ``ThinQAPI``
        device_id: ThinQ Device ID
        action:    ``{"mode"?, "temperature"?, "wind_strength"?}``

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``
    """
    async def run() -> CommandOutcome:
        status = await api.get_device_status(device_id)
        steps: list[dict] = []
        sequence = _action_sequence(action)
        for idx, (command, args) in enumerate(sequence):
            plan = build_command_plan(command, args, status)
            if plan.should_skip:
                continue
            steps.extend(await execute_plan(api, device_id, plan, status))
            # Po posledním kroku se stav už nečte – šetří limit volání LG API.
            if idx < len(sequence) - 1:
                await asyncio.sleep(_SETTLE_SECONDS)
                status = await api.get_device_status(device_id)
        if not steps:
            return CommandOutcome(sent=False, skip_reason="Zařízení už je v požadovaném stavu.")
        return CommandOutcome(sent=True, steps=steps)
    return run


# Ruční režim POER, jak ho hlásí fetch_poer_status. Jen v něm nastavená teplota
# platí trvale – v „auto“ ji vlastní program termostatu při dalším bloku přepíše.
_POER_MANUAL = ("heat", "home")


def _poer_steps(endpoint: str, data: dict, status: dict | None) -> list[tuple[str, dict]]:
    """
    Rozloží POER příkaz na kroky.

    Nastavení teploty se posílá jako ``heat`` + teplota. POER zpracuje v jednom
    požadavku jen první příkaz, proto dva požadavky; přepnutí do ``heat`` na chvíli
    aktivuje uloženou ruční teplotu, kterou druhý krok hned přepíše.

    Args:
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu
        status:   Čerstvý stav termostatu, nebo None (neznámý – posílá se vše)

    Returns:
        list: Dvojice (endpoint, data) v pořadí odeslání
    """
    if endpoint != "set_temp":
        return [(endpoint, data)]
    steps: list[tuple[str, dict]] = []
    if status is None or (status.get("mode"), status.get("preset")) != _POER_MANUAL:
        steps.append(("set_mode", {"mode": "heat", "preset": "home"}))
    steps.append(("set_temp", data))
    return steps


def _poer_requested_state(data: dict) -> tuple[str, str]:
    """
    Normalizuje požadovaný režim POER stejně, jako ho vrací ``fetch_poer_status``.

    Předvolba ``away`` se posílá jako ``eco`` a stav ji hlásí jako (``heat``, ``away``).

    Args:
        data: ``{"mode": str, "preset": str}``

    Returns:
        tuple[str, str]: (mode, preset)
    """
    preset = str(data.get("preset") or "home").lower()
    if preset == "away":
        return "heat", "away"
    return str(data.get("mode") or "auto").lower(), "home"


def _poer_skip_reason(status: dict, endpoint: str, data: dict) -> str | None:
    """
    Zjistí, zda POER už je v požadovaném stavu.

    Args:
        status:   Výsledek ``fetch_poer_status``
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu

    Returns:
        str | None: Důvod přeskočení, nebo None pokud je třeba příkaz odeslat
    """
    if status.get("error_text"):
        return None
    if endpoint == "set_temp":
        current = status.get("target_temperature_c")
        in_manual = (status.get("mode"), status.get("preset")) == _POER_MANUAL
        if (in_manual and current is not None
                and abs(float(current) - float(data["temperature"])) < 0.05):
            return f"Cílová teplota POER už je {current} °C."
        return None
    if endpoint == "set_mode":
        if (status.get("mode"), status.get("preset")) == _poer_requested_state(data):
            return "Režim POER je již nastaven."
    return None


def poer_command_job(
    api_key: str,
    device_id: str | None,
    endpoint: str,
    data: dict,
    check_noop: bool = True,
) -> JobFactory:
    """
    Úloha pro příkaz POER termostatu.

    Args:
        api_key:    POER API klíč
        device_id:  POER device ID (None = první termostat na účtu)
        endpoint:   ``"set_temp"`` nebo ``"set_mode"``
        data:       ``{"temperature": float}`` nebo ``{"mode": str, "preset": str}``
        check_noop: Před odesláním ověřit čerstvý stav a nic neměnící příkaz přeskočit.
                    Ruční příkazy ho vypínají – POER cloud propisuje změny se
                    zpožděním a uživatel musí vždy dostat, co zadal.
                    Nastavení teploty vždy končí v ručním režimu (``heat``).

    Returns:
        JobFactory: Úloha vracející ``CommandOutcome``; při selhání cloudu vyhodí
                    ``RuntimeError`` (arbitr ji zopakuje). Opakovaný pokus přeskočí
                    kroky, které už prošly – jinak by znovu poslal ``heat`` a
                    termostat by na chvíli skočil na uloženou ruční teplotu.
    """
    done_steps: list[dict] = []

    async def run() -> CommandOutcome:
        status = None
        if check_noop:
            # Bez cache – stav starý až 20 s by mohl přeskočit skutečnou změnu.
            status = await fetch_poer_status(api_key=api_key, preferred_device_id=device_id)
            skip_reason = _poer_skip_reason(status, endpoint, data)
            if skip_reason:
                return CommandOutcome(sent=False, skip_reason=skip_reason)
            if status.get("error_text"):
                status = None
        finished = {step["step"] for step in done_steps}
        for step_endpoint, step_data in _poer_steps(endpoint, data, status):
            if step_endpoint in finished:
                continue
            result = await send_poer_command(
                api_key=api_key, endpoint=step_endpoint, data=step_data,
                preferred_device_id=device_id,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_text") or "POER příkaz selhal.")
            done_steps.append({"step": step_endpoint, "result": result})
        return CommandOutcome(sent=True, steps=list(done_steps))
    return run
