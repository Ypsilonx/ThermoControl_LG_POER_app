# -*- coding: utf-8 -*-
"""
Rozhodování zóny – čisté funkce bez I/O.

Pořadí priorit (spec kap. 3): nouzové minimum > pojistky (krb) > přebití >
cíl podle režimu. Časy jsou místní (naivní ``datetime``), stejně jako časy
v ``control.json``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from zones.config import WEEKDAYS


@dataclass(frozen=True)
class ZoneDecision:
    """
    Výsledek rozhodnutí zóny.

    Args:
        zone_id:   ID zóny
        target_c:  Cílová teplota zóny; None = aplikace zónu neřídí (režim Ručně)
        reason:    Důvod pro deník a UI
        emergency: Teplota je pod nouzovým minimem (příkazy se zdrojem EMERGENCY)
        paused:    Zóna je pozastavená (krb topí) – topidla jen na nouzové minimum
        hold:      Chybí čerstvá vnitřní teplota – neposílat nové příkazy (spec kap. 8)
    """

    zone_id: str
    target_c: float | None
    reason: str
    emergency: bool = False
    paused: bool = False
    hold: bool = False


def _minutes(hhmm: str) -> int:
    """Převede ``HH:MM`` na minuty od půlnoci."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _at(day: datetime, hhmm: str) -> datetime:
    """Datum ``day`` v čase ``hhmm``."""
    minutes = _minutes(hhmm)
    return day.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def program_block(program: dict, now: datetime) -> tuple[dict, datetime]:
    """
    Najde právě platný blok programu.

    Blok platí do začátku dalšího; před prvním blokem dne platí poslední blok
    předchozího dne (po neděli pokračuje pondělí).

    Args:
        program: ``control["program"]`` (bloky seřazené podle času)
        now:     Místní čas

    Returns:
        tuple: (blok, okamžik jeho začátku)
    """
    for days_back in range(8):
        day = now - timedelta(days=days_back)
        blocks = program[WEEKDAYS[day.weekday()]]
        started = [b for b in blocks if days_back > 0 or _at(day, b["from"]) <= now]
        if started:
            return started[-1], _at(day, started[-1]["from"])
    raise ValueError("Program neobsahuje žádný blok.")


def next_block_start(program: dict, now: datetime) -> datetime:
    """
    Začátek příštího bloku programu po ``now``.

    Args:
        program: ``control["program"]``
        now:     Místní čas

    Returns:
        datetime: Začátek dalšího bloku (i v následujících dnech)
    """
    for days_ahead in range(8):
        day = now + timedelta(days=days_ahead)
        for block in program[WEEKDAYS[day.weekday()]]:
            start = _at(day, block["from"])
            if start > now:
                return start
    raise ValueError("Program neobsahuje žádný blok.")


def override_until(control: dict, now: datetime) -> datetime:
    """
    Konec nového přebití: v Programu do začátku dalšího bloku, jinak ``override_hours``.

    Args:
        control: Konfigurace řízení
        now:     Místní čas

    Returns:
        datetime: Okamžik, kdy přebití skončí
    """
    if control["mode"] == "program":
        return next_block_start(control["program"], now)
    return now + timedelta(hours=control["override_hours"])


def _in_window(now: datetime, start: str, end: str) -> bool:
    """Leží čas ``now`` v okně ``start``–``end`` (okno může přecházet přes půlnoc)?"""
    t = now.hour * 60 + now.minute
    a, b = _minutes(start), _minutes(end)
    return a <= t < b if a <= b else (t >= a or t < b)


def _automation_target(control: dict, zone_id: str, now: datetime) -> tuple[float, str]:
    """Cíl Automatiky včetně volitelného nočního útlumu."""
    automation = control["automation"]
    target = automation["targets"][zone_id]
    setback = automation["night_setback"]
    if setback["enabled"] and _in_window(now, setback["from"], setback["to"]):
        return target - setback["delta_c"], f"Automatika – noční útlum −{setback['delta_c']} °C"
    return target, "Automatika"


def _base_target(control: dict, mode: str, zone_id: str, now: datetime) -> tuple[float, str]:
    """Cíl režimu Program nebo Automatika (Ručně se při návratu z dovolené řídí Automatikou)."""
    if mode == "program":
        block, _ = program_block(control["program"], now)
        return block["targets"][zone_id], f"Program – blok od {block['from']}"
    return _automation_target(control, zone_id, now)


def _vacation_target(control: dict, zone_id: str, now: datetime) -> tuple[float, str]:
    """Cíl Dovolené: nepřítomnost, předtopení před návratem, po návratu předchozí režim."""
    vacation = control["vacation"]
    return_at = datetime.fromisoformat(vacation["return_at"])
    if now >= return_at:
        return _base_target(control, vacation["previous_mode"], zone_id, now)
    if now >= return_at - timedelta(hours=vacation["preheat_hours"]):
        target, _ = _base_target(control, vacation["previous_mode"], zone_id, return_at)
        return target, f"Dovolená – předtopení před návratem {return_at:%d.%m. %H:%M}"
    return vacation["away_targets"][zone_id], f"Dovolená – návrat {return_at:%d.%m. %H:%M}"


def mode_target(control: dict, zone_id: str, now: datetime) -> tuple[float | None, str]:
    """
    Cíl zóny podle režimu (bez přebití, krbu a nouze).

    Args:
        control: Konfigurace řízení
        zone_id: ID zóny
        now:     Místní čas

    Returns:
        tuple: (cíl nebo None v Ručně, důvod)
    """
    mode = control["mode"]
    if mode == "manual":
        return None, "Ručně – aplikace zónu neřídí"
    if mode == "vacation":
        return _vacation_target(control, zone_id, now)
    return _base_target(control, mode, zone_id, now)


def active_override(control: dict, zone_id: str, now: datetime) -> dict | None:
    """Vrátí platné přebití zóny (v režimu Ručně se přebití neuplatní)."""
    override = control["overrides"].get(zone_id)
    if control["mode"] == "manual" or override is None:
        return None
    return override if datetime.fromisoformat(override["until"]) > now else None


def decide_zone(
    zone_id: str,
    control: dict,
    indoor_c: float | None,
    fireplace_on: bool | None,
    now: datetime,
) -> ZoneDecision:
    """
    Rozhodne cílovou teplotu zóny.

    Args:
        zone_id:      ID zóny
        control:      Konfigurace řízení (``normalize_control``)
        indoor_c:     Čerstvá vnitřní teplota, nebo None
        fireplace_on: Krb topí (zásuvka čerpadla); None = neznámo
        now:          Místní čas

    Returns:
        ZoneDecision: Cíl a důvod
    """
    minimum = control["emergency_min_c"]
    below_minimum = indoor_c is not None and indoor_c < minimum
    emergency_reason = f"Nouze: {indoor_c} °C pod minimem {minimum} °C"

    if control["mode"] == "manual":
        if below_minimum:
            return ZoneDecision(zone_id, minimum, emergency_reason, emergency=True)
        return ZoneDecision(zone_id, None, "Ručně – aplikace zónu neřídí")

    if fireplace_on:
        return ZoneDecision(zone_id, minimum, "Krb topí – zóna pozastavena", paused=True)

    override = active_override(control, zone_id, now)
    if override is not None:
        until = datetime.fromisoformat(override["until"])
        target, reason = override["target_c"], f"Přebití do {until:%d.%m. %H:%M}"
    else:
        target, reason = mode_target(control, zone_id, now)

    if below_minimum:
        return ZoneDecision(zone_id, max(target, minimum), emergency_reason, emergency=True)
    if indoor_c is None:
        return ZoneDecision(zone_id, target, f"{reason} (bez vnitřní teploty – drží se)",
                            hold=True)
    return ZoneDecision(zone_id, target, reason)
