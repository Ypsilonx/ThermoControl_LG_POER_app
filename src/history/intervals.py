# -*- coding: utf-8 -*-
"""Výpočet intervalů topení z měření ``heating`` (0/1)."""

from dataclasses import dataclass
from datetime import datetime
from itertools import groupby

from history.store import Measurement


@dataclass(frozen=True)
class HeatingInterval:
    """
    Souvislý interval topení jednoho zařízení.

    Args:
        source:   Zdroj (``poer``, ``lg``)
        device:   ID zařízení
        start_ts: Začátek (první měření s topením)
        end_ts:   Konec (první měření bez topení, poslední známé topení před
                  výpadkem dat, nebo konec exportu)
        minutes:  Délka v minutách
        est_kwh:  Odhad spotřeby podle příkonu, None pokud příkon neznáme
        open:     True pokud interval na konci exportu stále běží
    """

    source: str
    device: str
    start_ts: str
    end_ts: str
    minutes: float
    est_kwh: float | None
    open: bool


def _seconds(start_ts: str, end_ts: str) -> float:
    """Vrátí počet sekund mezi dvěma ISO časy."""
    return (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds()


def _interval(source: str, device: str, start_ts: str, end_ts: str,
              power_kw: dict[str, float], is_open: bool) -> HeatingInterval:
    """Sestaví interval včetně délky a odhadu spotřeby."""
    minutes = round(_seconds(start_ts, end_ts) / 60, 1)
    kw = power_kw.get(device)
    est_kwh = round(kw * minutes / 60, 3) if kw is not None else None
    return HeatingInterval(source, device, start_ts, end_ts, minutes, est_kwh, is_open)


def _device_intervals(samples: list[Measurement], end_ts: str, max_gap_s: float | None,
                      power_kw: dict[str, float]) -> list[HeatingInterval]:
    """
    Intervaly topení jednoho zařízení.

    Zdroj s ``max_gap_s`` se odečítá periodicky: delší mezera mezi měřeními je
    výpadek dat a interval se uzavře na posledním známém topení (nic se
    nedomýšlí). Zdroj bez limitu (události z MQTT) drží stav až do další změny.
    """
    result: list[HeatingInterval] = []
    source, device = samples[0].source, samples[0].device
    start: str | None = None
    last_ts: str | None = None
    for sample in samples:
        if start and max_gap_s is not None and _seconds(last_ts, sample.ts) > max_gap_s:
            result.append(_interval(source, device, start, last_ts, power_kw, False))
            start = None
        if sample.value and start is None:
            start = sample.ts
        elif not sample.value and start is not None:
            result.append(_interval(source, device, start, sample.ts, power_kw, False))
            start = None
        last_ts = sample.ts
    if start is not None:
        if max_gap_s is not None and _seconds(last_ts, end_ts) > max_gap_s:
            result.append(_interval(source, device, start, last_ts, power_kw, False))
        else:
            result.append(_interval(source, device, start, end_ts, power_kw, True))
    return result


def heating_intervals(rows: list[Measurement], end_ts: str,
                      max_gap_s: dict[str, float | None],
                      power_kw: dict[str, float]) -> list[HeatingInterval]:
    """
    Spočítá intervaly topení ze všech měření ``heating``.

    Args:
        rows:      Měření (ostatní veličiny se ignorují)
        end_ts:    Konec exportu – uzavře ještě běžící intervaly
        max_gap_s: Maximální mezera mezi měřeními podle zdroje (None = bez limitu)
        power_kw:  Příkon podle ID zařízení pro odhad spotřeby

    Returns:
        list[HeatingInterval]: Intervaly seřazené podle zdroje, zařízení a času
    """
    heating = sorted((r for r in rows if r.metric == "heating"),
                     key=lambda r: (r.source, r.device, r.ts))
    result: list[HeatingInterval] = []
    for (source, _device), group in groupby(heating, key=lambda r: (r.source, r.device)):
        result.extend(_device_intervals(list(group), end_ts, max_gap_s.get(source), power_kw))
    return result
