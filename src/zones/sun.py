# -*- coding: utf-8 -*-
"""
Východ a západ slunce bez externí knihovny (zjednodušený algoritmus NOAA).

Přesnost je v řádu minut, což pro úpravu cíle podle slunce stačí.
"""

import math
from datetime import date, datetime, timedelta, timezone, tzinfo

# Výška středu slunce při východu/západu: refrakce + poloměr disku.
_ZENITH_DEG = 90.833


def _solar_noon_and_half_day(day: date, lat: float, lon: float) -> tuple[float, float]:
    """
    Sluneční poledne a polovina délky dne v minutách UTC od půlnoci.

    Returns:
        tuple: (poledne v min UTC, polovina dne v min); polární den/noc → výjimka
    """
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eq_time = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(_ZENITH_DEG)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        raise ValueError("Slunce v tento den nevychází nebo nezapadá.")
    half_day = 4 * math.degrees(math.acos(cos_ha))
    noon = 720 - 4 * lon - eq_time
    return noon, half_day


def sun_times(day: date, lat: float, lon: float, tz: tzinfo | None = None
              ) -> tuple[datetime, datetime]:
    """
    Východ a západ slunce.

    Args:
        day: Datum
        lat: Zeměpisná šířka (°, sever +)
        lon: Zeměpisná délka (°, východ +)
        tz:  Časová zóna výsledku; None = místní zóna systému

    Returns:
        tuple: (východ, západ) jako ``datetime`` s časovou zónou
    """
    noon, half_day = _solar_noon_and_half_day(day, lat, lon)
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    sunrise = midnight + timedelta(minutes=noon - half_day)
    sunset = midnight + timedelta(minutes=noon + half_day)
    return sunrise.astimezone(tz), sunset.astimezone(tz)
