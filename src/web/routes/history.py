# -*- coding: utf-8 -*-
"""Router: Historie dat – GET /api/history/export (CSV)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from history.export import ExportKind, build_csv
from web.settings import get_settings

router = APIRouter(prefix="/api/history", tags=["Historie"])


@router.get("/export", summary="Export historie do CSV")
async def export_history(
    request: Request,
    data: ExportKind = Query("measurements", description="Druh dat"),
    od: date | None = Query(None, description="První den (výchozí před 7 dny)"),
    do: date | None = Query(None, description="Poslední den včetně (výchozí dnes)"),
) -> Response:
    """
    Vrátí historii jako CSV (oddělovač ``;``).

    Args:
        request: FastAPI request (přístup k ``app.state.history``)
        data:    ``measurements`` | ``forecast`` | ``intervals`` | ``energy``
        od:      První den (lokální čas)
        do:      Poslední den včetně

    Returns:
        Response: CSV soubor ke stažení

    Raises:
        HTTPException 503: Sběr historie je vypnutý
        HTTPException 400: ``od`` je po ``do``
    """
    collector = getattr(request.app.state, "history", None)
    if collector is None:
        raise HTTPException(
            status_code=503, detail="Sběr historie je vypnutý (LG_HISTORY_ENABLED)."
        )
    end_day = do or date.today()
    start_day = od or end_day - timedelta(days=7)
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="Parametr 'od' musí být před 'do'.")

    settings = get_settings()
    text = await asyncio.to_thread(
        build_csv, collector.store, data, start_day, end_day,
        settings.poer_power_kw, collector.poll_seconds,
    )
    filename = f"historie_{data}_{start_day}_{end_day}.csv"
    return Response(
        # BOM kvůli správné diakritice při otevření v Excelu.
        content=("\ufeff" + text).encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
