"""Bases, timeframes, and session-aware resampling.

Schwab serves minute bars at 1/5/10/15/30 (30m and 15m go back ~9 months, 1m ~48 days) and
daily/weekly bars. It does not serve 60m or 4h (HTTP 400), so:

* 15m  is native.
* 1H, 4H are built from the cached **30m** bars. 30m and 1m give identical 1H/4H OHLCV (the 30m
  buckets align with the 09:30 anchor) but 30m reaches ~5x further back.
* 1D   is native.
* 1W   is built from the cached daily bars, anchored to W-FRI, rather than using Schwab's own
  Monday-stamped weekly bars, so there is one data path and one incomplete-bar rule.

Every frame this module returns has a UTC DatetimeIndex named ``ts`` (bar START for intraday, the
session date at 00:00 UTC for daily, the Friday date for weekly), float OHLCV columns, and a boolean
``complete``. A bar is complete only if it should already have ended *as of the moment the data was
fetched* (not just "as of now"), so a partial bar frozen in the cache can never be mistaken for a
finished one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTES = 9 * 60 + 30  # 09:30 ET
SESSION_CLOSE_MINUTES = 16 * 60  # 16:00 ET
EARLY_CLOSE_MINUTES = 13 * 60  # 13:00 ET on half-days

OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Base:
    """A cached series exactly as Schwab serves it."""

    key: str
    frequency_type: str  # Schwab frequencyType
    frequency: int
    minutes: int | None  # bar length; None for daily


@dataclass(frozen=True)
class Timeframe:
    key: str
    label: str
    base: str
    bucket_minutes: int | None  # session-anchored bucket built from base bars; None = daily/weekly
    weekly: bool = False

    @property
    def is_intraday(self) -> bool:
        return self.bucket_minutes is not None


BASES: dict[str, Base] = {
    b.key: b
    for b in (
        Base("1d", "daily", 1, None),
        Base("30m", "minute", 30, 30),
        Base("15m", "minute", 15, 15),
    )
}

TIMEFRAMES: dict[str, Timeframe] = {
    t.key: t
    for t in (
        Timeframe("15m", "15 min", "15m", 15),
        Timeframe("1H", "1 hour", "30m", 60),
        Timeframe("4H", "4 hour", "30m", 240),
        Timeframe("1D", "1 day", "1d", None),
        Timeframe("1W", "1 week", "1d", None, weekly=True),
    )
}


def get_timeframe(key: str) -> Timeframe:
    try:
        return TIMEFRAMES[key]
    except KeyError:
        raise ValueError(f"Unknown timeframe {key!r}. Valid: {', '.join(TIMEFRAMES)}") from None


def empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    out = pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLCV}, index=idx)
    out["complete"] = pd.Series(dtype="bool")
    return out


# ------------------------------------------------------------------ raw -> frame


def raw_to_frame(raw: pd.DataFrame, base: str) -> pd.DataFrame:
    """Schwab candle rows (``datetime`` epoch-ms + OHLCV) -> a sorted, de-duplicated frame.

    Daily candles are stamped at 05:00/06:00 UTC (midnight US Central) of the session date, so
    the index keeps just that date at 00:00 UTC. Intraday candles are stamped at the bar start.
    """
    if raw.empty:
        return empty_frame().drop(columns="complete")
    df = raw.drop_duplicates("datetime", keep="last").sort_values("datetime")
    idx = pd.DatetimeIndex(pd.to_datetime(df["datetime"].to_numpy(dtype="int64"), unit="ms", utc=True))
    if BASES[base].minutes is None:
        idx = idx.normalize()
    out = df[OHLCV].astype("float64")
    out.index = idx.rename("ts")
    return out[~out.index.duplicated(keep="last")]


# ------------------------------------------------------------------ sessions


def _close_utc(local_dates: pd.DatetimeIndex, early_closes: frozenset[date]) -> pd.DatetimeIndex:
    """Session close (16:00 ET, or 13:00 on half-days) for each tz-naive local date, in UTC."""
    if early_closes:
        early = np.isin(np.asarray(local_dates.date), list(early_closes))
    else:
        early = np.zeros(len(local_dates), dtype=bool)
    minutes = np.where(early, EARLY_CLOSE_MINUTES, SESSION_CLOSE_MINUTES)
    naive = local_dates + pd.to_timedelta(minutes, unit="m")
    return naive.tz_localize(MARKET_TZ, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")


def _bucket_starts(index: pd.DatetimeIndex, bucket_minutes: int) -> pd.DatetimeIndex:
    """Map each bar timestamp to the start of its session-anchored bucket.

    Buckets are counted from the 09:30 ET open, so 1H runs 09:30-10:30, 10:30-11:30, ... and a
    bucket never spans two sessions. The arithmetic runs in naive local time and is re-localized
    afterwards: adding an elapsed Timedelta to a tz-aware midnight would land on 10:30 instead of
    09:30 on a spring-forward day, since that day is only 23 hours long.
    """
    naive = index.tz_convert(MARKET_TZ).tz_localize(None)
    session_open = naive.normalize() + pd.Timedelta(minutes=SESSION_OPEN_MINUTES)
    offset = (naive - session_open) // pd.Timedelta(minutes=1)
    # Floor toward -inf so pre-market bars bucket cleanly into negative slots.
    bucket_index = (offset // bucket_minutes) * bucket_minutes
    starts = session_open + pd.to_timedelta(bucket_index, unit="m")
    return pd.DatetimeIndex(starts).tz_localize(
        MARKET_TZ, ambiguous=True, nonexistent="shift_forward"
    ).tz_convert("UTC")


def _agg(g) -> pd.DataFrame:
    return g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )


# ------------------------------------------------------------------ resamplers


def resample_intraday(
    df: pd.DataFrame,
    bucket_minutes: int,
    as_of: pd.Timestamp,
    early_closes: frozenset[date] = frozenset(),
    settle: pd.Timedelta = pd.Timedelta(0),
) -> pd.DataFrame:
    """Aggregate base bars into session-anchored buckets and flag completeness.

    A bucket ends at ``min(start + bucket, session close)``, so the last bucket of a session (a
    half-hour 1H bar at 15:30, a 2.5h 4H bar at 13:30) is complete once the session has closed.
    """
    if df.empty:
        return empty_frame()
    starts = _bucket_starts(df.index, bucket_minutes)
    out = _agg(df.groupby(starts, sort=True))
    out.index.name = "ts"
    local_dates = out.index.tz_convert(MARKET_TZ).tz_localize(None).normalize()
    nominal_end = out.index + pd.Timedelta(minutes=bucket_minutes)
    close = _close_utc(local_dates, early_closes)
    expected_end = nominal_end.where(nominal_end <= close, close)
    out["complete"] = np.asarray(expected_end + settle <= as_of)
    return out


def resample_daily(
    df: pd.DataFrame,
    as_of: pd.Timestamp,
    early_closes: frozenset[date] = frozenset(),
    settle: pd.Timedelta = pd.Timedelta(0),
) -> pd.DataFrame:
    """Daily bars: complete once that session's close (plus settle) is behind ``as_of``."""
    if df.empty:
        return empty_frame()
    out = df[OHLCV].copy()
    close = _close_utc(out.index.tz_localize(None), early_closes)
    out["complete"] = np.asarray(close + settle <= as_of)
    return out


def resample_weekly(
    daily: pd.DataFrame,
    as_of: pd.Timestamp,
    early_closes: frozenset[date] = frozenset(),
    settle: pd.Timedelta = pd.Timedelta(0),
) -> pd.DataFrame:
    """Daily -> weeks ending Friday (W-FRI), labelled with that Friday.

    A short week (holiday Friday) still gets the Friday label. The bar is complete once that
    Friday's session close has passed, whether or not the market traded that day.
    """
    if daily.empty:
        return empty_frame()
    w = (
        daily[OHLCV]
        .resample("W-FRI", label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )
    w.index.name = "ts"
    close = _close_utc(w.index.tz_localize(None), early_closes)
    w["complete"] = np.asarray(close + settle <= as_of)
    return w


def to_frame(
    raw: pd.DataFrame,
    tf_key: str,
    as_of: pd.Timestamp,
    early_closes: frozenset[date] = frozenset(),
    settle_seconds: int = 0,
) -> pd.DataFrame:
    """Cached Schwab rows -> the OHLCV + ``complete`` frame for one timeframe.

    ``as_of`` is when the cache was fetched (UTC). Nothing later than that can be complete.
    """
    tf = get_timeframe(tf_key)
    settle = pd.Timedelta(seconds=settle_seconds)
    base = raw_to_frame(raw, tf.base)
    if tf.weekly:
        return resample_weekly(resample_daily(base, as_of, early_closes, settle), as_of, early_closes, settle)
    if tf.bucket_minutes is None:
        return resample_daily(base, as_of, early_closes, settle)
    return resample_intraday(base, tf.bucket_minutes, as_of, early_closes, settle)


def drop_incomplete(df: pd.DataFrame) -> pd.DataFrame:
    """The frame without any bar that is still forming."""
    return df[df["complete"]]
