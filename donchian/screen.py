"""Breakout scanner: run the detector across a universe of symbols in the
background and surface fresh breakouts as a job progresses.

Fetching reuses the same store + hub as a single chart -- the incremental
parquet cache and the retry-with-backoff on 429/403/5xx already live in
`hub.PriceFetcher` -- just spread across a bounded thread pool. The hub itself
caps concurrent upstream calls, so no separate rate limiter is needed here.
"""

from __future__ import annotations

import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Thread
from typing import Any, Optional

import pandas as pd

from .config import SETTINGS
from .events import DetectorParams, detect
from .hub import PriceFetcher
from .store import load, path_for, update
from .timeframes import get_timeframe, to_frame
from .universe import Member, members

MAX_JOBS = 8


@dataclass
class ScreenParams:
    timeframe: str = "1D"
    indices: tuple[str, ...] = ("sp500", "nasdaq100")
    direction: str = "both"  # long | short | both
    kind: str = "close"  # close | wick | both
    within_bars: int = 3  # a breakout only counts if it's within the last N bars
    min_width_atr: Optional[float] = None
    limit: Optional[int] = None  # cap the symbol count (a quick look)


@dataclass
class ScreenJob:
    id: str
    params: ScreenParams
    state: str = "pending"  # pending | running | done | failed | cancelled
    total: int = 0
    done: int = 0
    phase: str = "starting"
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    executor: Optional[ThreadPoolExecutor] = field(default=None, repr=False)

    def payload(self, include_rows: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "state": self.state,
            "phase": self.phase,
            "progress": {
                "done": self.done,
                "total": self.total,
                "pct": round(100 * self.done / self.total, 1) if self.total else 0.0,
            },
            "found": len(self.rows),
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "meta": self.meta,
        }
        if include_rows:
            out["rows"] = sorted(self.rows, key=lambda r: r["bars_ago"])
        return out


_JOBS: dict[str, ScreenJob] = {}


def get_job(job_id: str) -> Optional[ScreenJob]:
    return _JOBS.get(job_id)


def _prune_jobs() -> None:
    if len(_JOBS) <= MAX_JOBS:
        return
    finished = sorted((j for j in _JOBS.values() if j.state in {"done", "failed", "cancelled"}),
                       key=lambda j: j.finished_at or "")
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


def _num(v) -> Optional[float]:
    return None if pd.isna(v) else float(v)


def _bool(v) -> Optional[bool]:
    return None if pd.isna(v) else bool(v)


def _scan_one(member: Member, tf, params: ScreenParams, fetch) -> Optional[dict[str, Any]]:
    """One symbol -> its most recent qualifying breakout, or None."""
    update(member.symbol, tf.base, fetch, SETTINGS)
    raw, fetched_at = load(path_for(SETTINGS, member.symbol, tf.base))
    frame = to_frame(raw, tf.key, fetched_at or datetime.now(timezone.utc),
                      SETTINGS.early_close_dates, SETTINGS.bar_settle_seconds)
    if frame.empty:
        return None

    det_params = DetectorParams(SETTINGS.entry_n, SETTINGS.slow_n, SETTINGS.atr_n,
                                 SETTINGS.volume_n, SETTINGS.false_break_k)
    ev = detect(frame, member.symbol, det_params, include_incomplete=True)
    if ev.empty:
        return None

    recent = ev[ev["bar"] >= len(frame) - params.within_bars]
    if params.direction != "both":
        recent = recent[recent["direction"] == params.direction]
    if params.kind != "both":
        recent = recent[recent["kind"] == params.kind]
    if params.min_width_atr is not None:
        recent = recent[recent["width_atr"] >= params.min_width_atr]
    if recent.empty:
        return None

    best = recent.sort_values("bar").iloc[-1]
    return {
        "symbol": member.symbol,
        "name": member.name,
        "sector": member.sector,
        "direction": best.direction,
        "kind": best.kind,
        "bars_ago": int(len(frame) - 1 - best.bar),
        "ts": int(best.ts.timestamp()),
        "entry_price": float(best.entry_price),
        "width_atr": _num(best.width_atr),
        "volume_ratio": _num(best.volume_ratio),
        "agrees_slow": bool(best.agrees_slow),
        "false_breakout": _bool(best.false_breakout),
        "complete": bool(best.complete),
    }


def _run(job: ScreenJob, fetch) -> None:
    params = job.params
    try:
        tf = get_timeframe(params.timeframe)
    except ValueError as exc:
        job.state, job.message = "failed", str(exc)
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    job.state = "running"
    job.started_at = datetime.now(timezone.utc).isoformat()

    try:
        roster = members(indices=params.indices)
    except FileNotFoundError as exc:
        job.state, job.message = "failed", str(exc)
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    if params.limit:
        roster = roster[: params.limit]
    job.total = len(roster)
    job.meta = {"timeframe": tf.key, "timeframe_label": tf.label, "symbols": len(roster)}
    job.phase = "scanning"

    executor = ThreadPoolExecutor(max_workers=SETTINGS.fetch_workers)
    job.executor = executor
    futures = {executor.submit(_scan_one, m, tf, params, fetch): m for m in roster}
    try:
        for future in as_completed(futures):
            member = futures[future]
            try:
                row = future.result()
            except CancelledError:
                job.done += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the scan
                job.errors.append({"symbol": member.symbol, "error": f"{exc.__class__.__name__}: {exc}"})
                job.done += 1
                continue
            if row is not None:
                job.rows.append(row)
            job.done += 1
    finally:
        executor.shutdown(wait=False)

    if job.state != "cancelled":
        job.state = "done"
    job.phase = "complete"
    job.finished_at = datetime.now(timezone.utc).isoformat()


def start_screen(params: ScreenParams, fetch=None) -> ScreenJob:
    job = ScreenJob(id=uuid.uuid4().hex[:12], params=params)
    _JOBS[job.id] = job
    _prune_jobs()
    Thread(target=_run, args=(job, fetch or PriceFetcher(settings=SETTINGS)), daemon=True).start()
    return job


def cancel_screen(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.state not in {"pending", "running"}:
        return False
    job.state = "cancelled"
    if job.executor:
        job.executor.shutdown(wait=False, cancel_futures=True)
    job.finished_at = datetime.now(timezone.utc).isoformat()
    return True


def index_options() -> tuple[str, ...]:
    return ("sp500", "nasdaq100")
