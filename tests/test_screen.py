"""Scanner: per-symbol breakout filtering and the background job lifecycle.
Synthetic bars only -- no network, no schwab_hub."""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import donchian.screen as screen
from donchian.screen import ScreenParams, _scan_one, cancel_screen, start_screen
from donchian.timeframes import get_timeframe
from donchian.universe import Member
from .helpers import ms, raw_daily

UP = Member(symbol="UPBRK", name="Up Co", sector="Technology", indices=("sp500",))
FLAT = Member(symbol="FLAT", name="Flat Co", sector="Technology", indices=("sp500",))
AGED = Member(symbol="AGEDBRK", name="Aged Co", sector="Technology", indices=("sp500",))


def _flat_raw(n=70):
    """A series that never breaks its own channel: constant OHLCV."""
    start = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    rows = [(ms(start + timedelta(days=d)), 100.0, 100.0, 100.0, 100.0, 1000.0) for d in range(n)]
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])


def _aged_raw(ramp_days=65, flat_days=5):
    """Ramps (one real breakout, on the last ramp day) then goes flat, so the
    breakout ages by `flat_days` bars without a fresh one replacing it."""
    df = raw_daily([date(2026, 1, 1) + timedelta(days=d) for d in range(ramp_days)])
    last_close = float(df["close"].iloc[-1])
    flat = [
        (ms(datetime(2026, 1, 1, 5, tzinfo=timezone.utc) + timedelta(days=ramp_days + k)),
         last_close, last_close, last_close, last_close, 1000.0)
        for k in range(flat_days)
    ]
    return pd.concat([df, pd.DataFrame(flat, columns=df.columns)], ignore_index=True)


def _fetch(symbol, base, start, end):
    # raw_daily's fixture ramps every day (high[d] = 110+d, close[d] = 105+d): the
    # high pokes past the prior channel but the close stays under it, so this is a
    # standing long WICK breakout, never a close one -- tests below ask for "both".
    if symbol == "UPBRK":
        return raw_daily([date(2026, 1, 1) + timedelta(days=d) for d in range(70)])
    if symbol == "AGEDBRK":
        return _aged_raw()
    return _flat_raw()


def test_scan_one_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "SETTINGS", replace(screen.SETTINGS, data_dir=tmp_path))
    tf = get_timeframe("1D")

    row = _scan_one(UP, tf, ScreenParams(timeframe="1D", kind="both", within_bars=3), _fetch)
    assert row is not None
    assert row["symbol"] == "UPBRK" and row["direction"] == "long" and row["kind"] == "wick"

    # the default kind ("close") must not match a wick-only breakout
    assert _scan_one(UP, tf, ScreenParams(timeframe="1D", within_bars=3), _fetch) is None
    assert _scan_one(FLAT, tf, ScreenParams(timeframe="1D", kind="both", within_bars=3), _fetch) is None
    # a long-only breakout must not show up in a short-only scan
    assert _scan_one(UP, tf, ScreenParams(timeframe="1D", kind="both", direction="short", within_bars=3), _fetch) is None
    # a breakout 5 bars back is outside a 3-bar window but inside a 10-bar one
    assert _scan_one(AGED, tf, ScreenParams(timeframe="1D", kind="both", within_bars=3), _fetch) is None
    aged_hit = _scan_one(AGED, tf, ScreenParams(timeframe="1D", kind="both", within_bars=10), _fetch)
    assert aged_hit is not None and aged_hit["bars_ago"] == 5


def test_start_screen_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "SETTINGS", replace(screen.SETTINGS, data_dir=tmp_path, fetch_workers=2))
    monkeypatch.setattr(screen, "members", lambda indices=(): [UP, FLAT])

    job = start_screen(ScreenParams(timeframe="1D", kind="both", within_bars=3), fetch=_fetch)
    for _ in range(500):
        if job.state in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.01)

    assert job.state == "done", job.message
    assert job.total == 2 and job.done == 2
    assert {r["symbol"] for r in job.rows} == {"UPBRK"}
    assert cancel_screen(job.id) is False  # already finished, nothing to cancel
