# -*- coding: utf-8 -*-
"""
Volba zdroje tepla a úpravy cíle zóny – čisté funkce bez I/O (spec kap. 2.2, 6, 7).

- Zóna s klimatizací: AC je hlavní zdroj, POER topidla v ní jsou fólie –
  v NT drží základ (cíl + posun), ve VT jen jako záloha.
- Zóna bez klimatizace (koupelna): POER topidlo topí na cíl vždy.
- Automatika navíc upravuje cíl podle slunce a vysoušení koupelny.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from zones.config import Heater
from zones.decide import ZoneDecision


@dataclass(frozen=True)
class SourceContext:
    """
    Okolnosti pro volbu zdroje v zóně.

    Args:
        low_tariff:      Platí nízký tarif
        outdoor_c:       Venkovní teplota zóny, nebo None
        frost_expected:  Předpověď mrazu v nejbližších hodinách
        ac_available:    Klimatizace zóny je dostupná
        ac_insufficient: Klimatizace topí, a přesto teplota klesá
    """

    low_tariff: bool
    outdoor_c: float | None
    frost_expected: bool
    ac_available: bool
    ac_insufficient: bool


def _backup_reason(ctx: SourceContext, sources: dict) -> str | None:
    """Důvod, proč má fólie ve VT zastoupit klimatizaci, nebo None."""
    if not ctx.ac_available:
        return "AC nedostupná"
    if ctx.ac_insufficient:
        return "AC nestačí"
    threshold = sources["ac_min_outdoor_c"]
    if ctx.outdoor_c is not None and ctx.outdoor_c < threshold:
        return f"venku {ctx.outdoor_c:.1f} °C pod {threshold:.1f} °C"
    return None


def heater_setpoint(
    heater: Heater,
    zone_has_ac: bool,
    decision: ZoneDecision,
    ctx: SourceContext,
    minimum: float,
    sources: dict,
) -> tuple[float, str]:
    """
    Setpoint POER topidla podle volby zdroje.

    Args:
        heater:      POER topidlo zóny
        zone_has_ac: Zóna má klimatizaci (POER topidlo je pak fólie)
        decision:    Rozhodnutí zóny (cíl už upravený)
        ctx:         Tarif, venkovní teplota, stav AC
        minimum:     Nouzové minimum – nikdy se nejde pod něj
        sources:     ``control["sources"]``

    Returns:
        tuple: (setpoint °C bez zaokrouhlení, důvod do deníku)
    """
    target = decision.target_c
    if decision.paused:
        return minimum, "pozastaveno"
    if not zone_has_ac:
        return max(target + heater.offset_c, minimum), "jediný zdroj"
    if decision.emergency:
        return max(target, minimum), "fólie – nouze"
    backup = _backup_reason(ctx, sources)
    if backup is not None:
        return max(target, minimum), f"fólie záloha: {backup}"
    if ctx.low_tariff:
        if ctx.frost_expected:
            return max(target, minimum), "fólie NT – předpověď mrazu, nahřívá do zásoby"
        return max(target + heater.offset_c, minimum), "fólie NT – základ"
    return minimum, "fólie VT – vypnuto, topí AC"


def _hourly_at(hourly: list[dict], start: datetime, end: datetime, key: str) -> list[float]:
    """Hodnoty ``key`` z hodinové předpovědi s časem v intervalu ``[start, end)``."""
    values = []
    for hour in hourly:
        try:
            ts = datetime.fromisoformat(hour["time_utc"])
        except (KeyError, TypeError, ValueError):
            continue
        value = hour.get(key)
        if start <= ts < end and value is not None:
            values.append(float(value))
    return values


def frost_expected(hourly: list[dict], now_utc: datetime, hours: float, threshold_c: float
                   ) -> bool:
    """
    Ukazuje předpověď mráz v nejbližších ``hours`` hodinách?

    Args:
        hourly:      Hodinová předpověď (``time_utc``, ``temp_c``)
        now_utc:     Aktuální čas (s časovou zónou)
        hours:       Horizont
        threshold_c: Teplota považovaná za mráz (pod ní)

    Returns:
        bool: True při mrazu; bez předpovědi False
    """
    temps = _hourly_at(hourly, now_utc, now_utc + timedelta(hours=hours), "temp_c")
    return bool(temps) and min(temps) < threshold_c


def sun_adjustment(
    side: str | None,
    sun: dict,
    now: datetime,
    sunrise: datetime,
    sunset: datetime,
    hourly: list[dict],
) -> tuple[float, str | None]:
    """
    Snížení cíle, když na stranu zóny brzy (nebo právě) svítí slunce.

    Okno: východ = od ``lead_hours`` před východem slunce do poledne,
    západ = od poledne do západu slunce. Platí jen při průměrné oblačnosti
    v předpovědi (od teď do konce okna) pod ``max_cloudiness_pct``.

    Args:
        side:    ``"east"``, ``"west"`` nebo None
        sun:     ``control["automation"]["sun"]``
        now:     Aktuální čas (s časovou zónou)
        sunrise: Východ slunce (s časovou zónou)
        sunset:  Západ slunce (s časovou zónou)
        hourly:  Hodinová předpověď (``time_utc``, ``cloudiness_pct``)

    Returns:
        tuple: (změna cíle °C, důvod nebo None)
    """
    if not sun["enabled"] or side is None:
        return 0.0, None
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if side == "east":
        start, end = sunrise - timedelta(hours=sun["lead_hours"]), noon
    else:
        start, end = noon, sunset
    if not start <= now < end:
        return 0.0, None
    clouds = _hourly_at(hourly, now.replace(minute=0, second=0, microsecond=0), end,
                        "cloudiness_pct")
    if not clouds or sum(clouds) / len(clouds) >= sun["max_cloudiness_pct"]:
        return 0.0, None
    return -sun["reduction_c"], f"slunce −{sun['reduction_c']} °C"


@dataclass(frozen=True)
class DryingState:
    """
    Stav vysoušení zóny.

    Args:
        active_since: Začátek běžícího vysoušení, nebo None
        armed:        Smí se vysoušení znovu spustit (vlhkost mezitím klesla pod návrat)
    """

    active_since: datetime | None = None
    armed: bool = True


def drying_adjustment(
    humidity: float | None,
    drying: dict,
    state: DryingState,
    now: datetime,
) -> tuple[float, str | None, DryingState]:
    """
    Zvýšení cíle při vysoké vlhkosti (proti plísni), časově omezené a s hysterezí.

    Args:
        humidity: Vnitřní vlhkost zóny (%), nebo None
        drying:   ``control["automation"]["drying"]``
        state:    Předchozí stav
        now:      Aktuální čas

    Returns:
        tuple: (změna cíle °C, důvod nebo None, nový stav)
    """
    if not drying["enabled"] or humidity is None:
        return 0.0, None, DryingState(armed=state.armed)
    below_rearm = humidity < drying["rearm_pct"]
    if state.active_since is not None:
        expired = now - state.active_since >= timedelta(minutes=drying["max_minutes"])
        if expired or below_rearm:
            return 0.0, None, DryingState(armed=below_rearm)
        return drying["boost_c"], f"vysoušení – vlhkost {humidity:.0f} %", state
    if state.armed and humidity > drying["humidity_pct"]:
        return (drying["boost_c"], f"vysoušení – vlhkost {humidity:.0f} %",
                DryingState(active_since=now, armed=False))
    return 0.0, None, DryingState(armed=state.armed or below_rearm)


def ac_insufficient(
    history: list[tuple[datetime, float]],
    heating_since: datetime | None,
    target_c: float,
    now: datetime,
    cfg: dict,
) -> bool:
    """
    Klimatizace nestačí: topí aspoň ``minutes`` a teplota za tu dobu klesla o ``drop_c``.

    Bez dostatečně dlouhé historie (např. po restartu serveru) vrací False.

    Args:
        history:       Vnitřní teploty zóny ``[(čas, °C)]`` vzestupně
        heating_since: Od kdy klimatizace nepřetržitě topí, nebo None
        target_c:      Cíl zóny
        now:           Aktuální čas
        cfg:           ``control["sources"]["ac_insufficient"]``

    Returns:
        bool: True, pokud má fólie zastoupit klimatizaci
    """
    window = timedelta(minutes=cfg["minutes"])
    if heating_since is None or now - heating_since < window or not history:
        return False
    older = [temp for ts, temp in history if ts <= now - window]
    if not older:
        return False
    current = history[-1][1]
    return current < target_c and older[-1] - current >= cfg["drop_c"]
