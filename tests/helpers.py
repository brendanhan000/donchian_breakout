"""Shared builders for tests. No network, no Schwab: everything here is synthetic."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")


def make_frame(rows, complete=None) -> pd.DataFrame:
    """Frame from (open, high, low, close, volume) tuples on consecutive UTC-midnight dates."""
    idx = pd.date_range("2026-01-05", periods=len(rows), freq="D", tz="UTC", name="ts")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx, dtype="float64")
    df["complete"] = True if complete is None else complete
    return df


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def raw_30m(day: date, n_bars: int, price0: float = 100.0) -> pd.DataFrame:
    """Raw Schwab-shaped 30m candles for one session, starting 09:30 ET.

    Bar k: open = price0+k, high = open+0.5, low = open-0.5, close = open+0.25, volume = 1000+k.
    """
    start = datetime.combine(day, time(9, 30), tzinfo=ET)
    rows = []
    for k in range(n_bars):
        p = price0 + k
        rows.append((ms(start + timedelta(minutes=30 * k)), p, p + 0.5, p - 0.5, p + 0.25, 1000 + k))
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])


def raw_daily(days: list[date], stamp_hour_utc: int = 5) -> pd.DataFrame:
    """Raw daily candles as Schwab stamps them (midnight US Central = 05:00Z or 06:00Z)."""
    rows = []
    for d, day in enumerate(days):
        t = datetime(day.year, day.month, day.day, stamp_hour_utc, tzinfo=timezone.utc)
        rows.append((ms(t), 100.0 + d, 110.0 + d, 90.0 + d, 105.0 + d, 1000.0 * (d + 1)))
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])


def et(y, m, d, hh=0, mm=0) -> pd.Timestamp:
    return pd.Timestamp(datetime(y, m, d, hh, mm, tzinfo=ET)).tz_convert("UTC")
