# -*- coding: utf-8 -*-
"""
Tarif D25d – okna nízkého tarifu (NT).

Okna se zadávají v aplikaci zvlášť pro pracovní den a víkend; okno může
přecházet přes půlnoc (např. 22:00–06:00). Rozhoduje den, ve kterém čas leží
(sobota 03:00 se řídí víkendovými okny). Státní svátky se neřeší (spec kap. 13).
"""

from datetime import datetime


def _minutes(hhmm: str) -> int:
    """Převede ``HH:MM`` na minuty od půlnoci."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _in_window(minute: int, start: str, end: str) -> bool:
    """Leží minuta dne v okně ``start``–``end`` (i přes půlnoc)?"""
    a, b = _minutes(start), _minutes(end)
    return a <= minute < b if a <= b else (minute >= a or minute < b)


def is_low_tariff(tariff: dict, now: datetime) -> bool:
    """
    Platí v daném okamžiku nízký tarif?

    Args:
        tariff: ``{"workday": [{"from", "to"}], "weekend": [...]}``
        now:    Místní čas

    Returns:
        bool: True v okně NT
    """
    windows = tariff["weekend" if now.weekday() >= 5 else "workday"]
    minute = now.hour * 60 + now.minute
    return any(_in_window(minute, w["from"], w["to"]) for w in windows)
