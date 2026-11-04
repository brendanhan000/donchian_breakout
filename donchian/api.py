"""FastAPI app: JSON endpoints for OHLCV + Donchian channels + breakout events,
plus the static chart UI.

Channels and events are computed over the **full** cached history so rolling
windows (the slow channel, the false-breakout lookahead) are never starved by
the display window; only the response is trimmed to ``chart_lookback_bars``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .events import DetectorParams, channels, detect
from .hub import FetchFailed, HubAuthError, HubUnavailable, NoData, PriceFetcher
from .screen import ScreenParams, cancel_screen, get_job, index_options, start_screen
from .store import load, path_for, update
from .timeframes import TIMEFRAMES, get_timeframe, to_frame
from .universe import summary as universe_summary

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Donchian Breakout")
app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")

_fetch = PriceFetcher(settings=SETTINGS)


@app.exception_handler(HubUnavailable)
async def _hub_down(_request, exc: HubUnavailable):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.exception_handler(HubAuthError)
async def _hub_auth(_request, exc: HubAuthError):
    return JSONResponse(status_code=401, content={"error": str(exc)})


@app.exception_handler(NoData)
async def _no_data(_request, exc: NoData):
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(FetchFailed)
async def _fetch_failed(_request, exc: FetchFailed):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/style.css")
def style_css() -> FileResponse:
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/api/config")
def config() -> dict:
    return {
        "timeframes": [
            {"key": t.key, "label": t.label, "is_intraday": t.is_intraday} for t in TIMEFRAMES.values()
        ],
        "defaults": {"symbol": SETTINGS.default_symbol, "timeframe": SETTINGS.default_timeframe},
        "params": {
            "entry_n": SETTINGS.entry_n,
            "slow_n": SETTINGS.slow_n,
            "exit_n": SETTINGS.exit_n,
            "atr_n": SETTINGS.atr_n,
            "volume_n": SETTINGS.volume_n,
            "false_break_k": SETTINGS.false_break_k,
        },
    }


def _clean(v):
    """NaN/NA -> None, everything else -> a plain float (JSON has no NaN)."""
    return None if pd.isna(v) else float(v)


def _line(series: pd.Series) -> list[dict]:
    return [{"time": int(ts.timestamp()), "value": float(v)} for ts, v in series.items() if pd.notna(v)]


@app.get("/api/bars")
def api_bars(
    symbol: str = Query(SETTINGS.default_symbol, max_length=16),
    timeframe: str = Query(SETTINGS.default_timeframe),
) -> dict:
    try:
        tf = get_timeframe(timeframe)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol is required")

    result = update(symbol, tf.base, _fetch, SETTINGS)
    raw, fetched_at = load(path_for(SETTINGS, symbol, tf.base))
    full = to_frame(raw, timeframe, fetched_at or pd.Timestamp.now(tz="UTC"),
                     SETTINGS.early_close_dates, SETTINGS.bar_settle_seconds)
    if full.empty:
        raise HTTPException(404, f"no bars for {symbol} {timeframe}")

    ch_full = channels(full, SETTINGS.entry_n, SETTINGS.slow_n, SETTINGS.exit_n)
    params = DetectorParams(SETTINGS.entry_n, SETTINGS.slow_n, SETTINGS.atr_n,
                             SETTINGS.volume_n, SETTINGS.false_break_k)
    ev_full = detect(full, symbol, params, include_incomplete=True)

    n = min(SETTINGS.chart_lookback_bars, len(full))
    frame = full.tail(n)
    cutoff = len(full) - n
    ev = ev_full[ev_full["bar"] >= cutoff]

    bars = [
        {"time": int(ts.timestamp()), "open": float(o), "high": float(h), "low": float(l),
         "close": float(c), "volume": float(v), "complete": bool(comp)}
        for ts, o, h, l, c, v, comp in zip(
            frame.index, frame["open"], frame["high"], frame["low"], frame["close"],
            frame["volume"], frame["complete"])
    ]
    band_payload = {
        name: {col: _line(df.tail(n)[col]) for col in ("upper", "lower", "mid")}
        for name, df in ch_full.items()
    }
    events_payload = [
        {
            "ts": int(row.ts.timestamp()),
            "direction": row.direction,
            "kind": row.kind,
            "entry_price": float(row.entry_price),
            "width_atr": _clean(row.width_atr),
            "penetration_atr": _clean(row.penetration_atr),
            "volume_ratio": _clean(row.volume_ratio),
            "agrees_slow": bool(row.agrees_slow),
            "false_breakout": None if pd.isna(row.false_breakout) else bool(row.false_breakout),
            "complete": bool(row.complete),
        }
        for row in ev.itertuples()
    ]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "cache": {"status": result.status, "rows_total": result.rows_total},
        "bars": bars,
        "channels": band_payload,
        "events": events_payload,
    }


@app.get("/api/universe")
def api_universe() -> dict:
    try:
        return universe_summary()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/scan")
def api_scan_start(
    timeframe: str = Query(SETTINGS.default_timeframe),
    indices: str = Query("sp500,nasdaq100"),
    direction: str = Query("both"),
    kind: str = Query("close"),
    within_bars: int = Query(3, ge=1, le=50),
    min_width_atr: Optional[float] = Query(None, ge=0),
    limit: Optional[int] = Query(None, ge=1, le=1000),
) -> dict:
    try:
        get_timeframe(timeframe)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    wanted = tuple(i.strip() for i in indices.split(",") if i.strip())
    unknown = [i for i in wanted if i not in set(index_options())]
    if unknown or not wanted:
        raise HTTPException(400, f"Unknown index {unknown or ['(none)']}; use sp500 and/or nasdaq100")
    if direction not in ("long", "short", "both"):
        raise HTTPException(400, "direction must be long, short, or both")
    if kind not in ("close", "wick", "both"):
        raise HTTPException(400, "kind must be close, wick, or both")

    job = start_screen(ScreenParams(timeframe=timeframe, indices=wanted, direction=direction,
                                     kind=kind, within_bars=within_bars, min_width_atr=min_width_atr,
                                     limit=limit))
    return job.payload(include_rows=False)


@app.get("/api/scan/{job_id}")
def api_scan_status(job_id: str, rows: bool = Query(True)) -> dict:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"No scan job {job_id!r}")
    return job.payload(include_rows=rows)


@app.delete("/api/scan/{job_id}")
def api_scan_cancel(job_id: str) -> dict:
    if not get_job(job_id):
        raise HTTPException(404, f"No scan job {job_id!r}")
    return {"cancelled": cancel_screen(job_id)}
