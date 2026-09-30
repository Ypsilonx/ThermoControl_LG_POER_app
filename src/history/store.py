# -*- coding: utf-8 -*-
"""
Úložiště historie v SQLite.

Metody jsou synchronní a každá si otevírá vlastní spojení – volají se přes
``asyncio.to_thread`` z různých vláken, takže sdílené spojení by nešlo použít.
Časy se ukládají jako ISO 8601 v UTC, takže rozsahové dotazy fungují
lexikálním porovnáním.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements (ts);
CREATE TABLE IF NOT EXISTS forecast (
    fetched_at TEXT NOT NULL,
    target_time TEXT NOT NULL,
    temp_c REAL,
    cloudiness_pct REAL,
    precip_mm_h REAL,
    humidity_pct REAL,
    wind_ms REAL,
    PRIMARY KEY (fetched_at, target_time)
);
CREATE TABLE IF NOT EXISTS energy_daily (
    device TEXT NOT NULL,
    day TEXT NOT NULL,
    wh REAL NOT NULL,
    PRIMARY KEY (device, day)
);
"""

FORECAST_FIELDS = ("temp_c", "cloudiness_pct", "precip_mm_h", "humidity_pct", "wind_ms")


@dataclass(frozen=True)
class Measurement:
    """
    Jedno měření.

    Args:
        ts:     Čas měření (ISO 8601, UTC)
        source: Zdroj (``poer``, ``lg``, ``chmi``, …)
        device: Zařízení / místo (ID termostatu, ID klimatizace, ``chmi``)
        metric: Veličina (``temperature_c``, ``heating``, ``mode``, …)
        value:  Číselná hodnota
        text:   Textová hodnota (režim, zdroj teploty)
    """

    ts: str
    source: str
    device: str
    metric: str
    value: float | None = None
    text: str | None = None


class HistoryStore:
    """Zápis a čtení historie v jednom SQLite souboru."""

    def __init__(self, path: Path) -> None:
        """
        Args:
            path: Cesta k souboru databáze (vytvoří se i se schématem)
        """
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            # WAL: dlouhé čtení při exportu neblokuje zápisy sběru.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Otevře nové spojení s řádky přístupnými podle názvu sloupce."""
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def add_measurements(self, rows: Iterable[Measurement]) -> None:
        """Uloží měření."""
        data = [(r.ts, r.source, r.device, r.metric, r.value, r.text) for r in rows]
        if not data:
            return
        with closing(self._connect()) as conn, conn:
            conn.executemany("INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?)", data)

    def add_forecast(self, fetched_at: str, hourly: list[dict[str, Any]]) -> None:
        """
        Uloží snímek hodinové předpovědi (opakované uložení téhož stažení nic nezdvojí).

        Args:
            fetched_at: Čas stažení předpovědi (ISO, UTC)
            hourly:     Hodinové body s ``time_utc`` a hodnotami z ``FORECAST_FIELDS``
        """
        data = [
            (fetched_at, point["time_utc"], *(point.get(f) for f in FORECAST_FIELDS))
            for point in hourly if point.get("time_utc")
        ]
        with closing(self._connect()) as conn, conn:
            conn.executemany("INSERT OR REPLACE INTO forecast VALUES (?, ?, ?, ?, ?, ?, ?)", data)

    def upsert_energy_daily(self, device: str, day: str, wh: float) -> None:
        """Uloží (nebo přepíše) denní spotřebu zařízení; ``day`` ve formátu YYYY-MM-DD."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO energy_daily VALUES (?, ?, ?)", (device, day, float(wh))
            )

    def purge_before(self, cutoff_ts: str) -> None:
        """Smaže všechna data starší než ``cutoff_ts`` (ISO, UTC)."""
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM measurements WHERE ts < ?", (cutoff_ts,))
            conn.execute("DELETE FROM forecast WHERE fetched_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM energy_daily WHERE day < ?", (cutoff_ts[:10],))

    def measurements(self, start_ts: str, end_ts: str) -> list[Measurement]:
        """Vrátí měření v intervalu ``<start_ts, end_ts)`` seřazená podle času."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM measurements WHERE ts >= ? AND ts < ? ORDER BY ts, rowid",
                (start_ts, end_ts),
            ).fetchall()
        return [Measurement(**dict(row)) for row in rows]

    def last_before(self, ts: str, source: str, metric: str) -> list[Measurement]:
        """
        Vrátí poslední měření veličiny před ``ts`` pro každé zařízení zdroje.

        Slouží k navázání stavu u zdrojů, které hlásí jen změny (LG) – interval
        topení mohl začít před začátkem exportu.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT m.* FROM measurements m JOIN ("
                "  SELECT device, MAX(ts) AS ts FROM measurements"
                "  WHERE source = ? AND metric = ? AND ts < ? GROUP BY device"
                ") last ON m.device = last.device AND m.ts = last.ts "
                "WHERE m.source = ? AND m.metric = ? ORDER BY m.rowid",
                (source, metric, ts, source, metric),
            ).fetchall()
        latest: dict[str, Measurement] = {}
        for row in rows:
            latest[row["device"]] = Measurement(**dict(row))
        return list(latest.values())

    def forecast_rows(self, start_ts: str, end_ts: str) -> list[dict[str, Any]]:
        """Vrátí snímky předpovědí stažené v intervalu ``<start_ts, end_ts)``."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM forecast WHERE fetched_at >= ? AND fetched_at < ? "
                "ORDER BY fetched_at, target_time",
                (start_ts, end_ts),
            ).fetchall()
        return [dict(row) for row in rows]

    def energy_rows(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        """Vrátí denní spotřebu pro dny ``start_day``–``end_day`` včetně (YYYY-MM-DD)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM energy_daily WHERE day >= ? AND day <= ? ORDER BY day, device",
                (start_day, end_day),
            ).fetchall()
        return [dict(row) for row in rows]
