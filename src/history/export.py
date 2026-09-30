# -*- coding: utf-8 -*-
"""CSV export historie (oddělovač ``;`` jako u exportu energie)."""

import csv
import io
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from history.intervals import heating_intervals
from history.store import FORECAST_FIELDS, HistoryStore

ExportKind = Literal["measurements", "forecast", "intervals", "energy"]


def _utc_bounds(start_day: date, end_day: date) -> tuple[str, str]:
    """Převede lokální dny (včetně) na interval ``<od, do)`` v UTC ISO."""
    start = datetime.combine(start_day, time.min).astimezone()
    end = datetime.combine(end_day + timedelta(days=1), time.min).astimezone()
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _fmt(value) -> str:
    """Hodnota do CSV; None jako prázdná buňka."""
    return "" if value is None else str(value)


def build_csv(store: HistoryStore, kind: ExportKind, start_day: date, end_day: date,
              power_kw: dict[str, float], poll_seconds: float) -> str:
    """
    Sestaví CSV text pro zvolený druh dat a rozsah dní.

    Args:
        store:        Úložiště historie
        kind:         ``measurements`` | ``forecast`` | ``intervals`` | ``energy``
        start_day:    První den (lokální čas, včetně)
        end_day:      Poslední den (včetně)
        power_kw:     Příkon podle ID zařízení (odhad kWh v intervalech)
        poll_seconds: Interval odečtu POER – mezera nad 3× se bere jako výpadek dat

    Returns:
        str: CSV text s hlavičkou
    """
    start_ts, end_ts = _utc_bounds(start_day, end_day)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")

    if kind == "measurements":
        writer.writerow(["cas", "zdroj", "zarizeni", "velicina", "hodnota", "text"])
        for m in store.measurements(start_ts, end_ts):
            writer.writerow([m.ts, m.source, m.device, m.metric, _fmt(m.value), _fmt(m.text)])
    elif kind == "forecast":
        writer.writerow(["stazeno", "cas", "teplota_c", "oblacnost_pct", "srazky_mm_h",
                         "vlhkost_pct", "vitr_ms"])
        for row in store.forecast_rows(start_ts, end_ts):
            writer.writerow([row["fetched_at"], row["target_time"],
                             *(_fmt(row[f]) for f in FORECAST_FIELDS)])
    elif kind == "intervals":
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # LG hlásí jen změny – interval mohl začít před oknem; navázat posledním stavem.
        seed = [replace(m, ts=start_ts) for m in store.last_before(start_ts, "lg", "heating")]
        intervals = heating_intervals(
            seed + store.measurements(start_ts, end_ts), min(end_ts, now_ts),
            {"poer": 3 * poll_seconds}, power_kw,
        )
        writer.writerow(["zdroj", "zarizeni", "od", "do", "minuty", "odhad_kwh", "probiha"])
        for i in intervals:
            # „Probíhá“ jen když interval sahá do přítomnosti, ne když ho uřízl konec okna.
            running = i.open and i.end_ts >= now_ts
            writer.writerow([i.source, i.device, i.start_ts, i.end_ts, i.minutes,
                             _fmt(i.est_kwh), "ano" if running else "ne"])
    else:
        writer.writerow(["zarizeni", "den", "wh"])
        for row in store.energy_rows(start_day.isoformat(), end_day.isoformat()):
            writer.writerow([row["device"], row["day"], row["wh"]])
    return buffer.getvalue()
