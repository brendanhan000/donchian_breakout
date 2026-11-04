"""Donchian channels and ATR. Pure functions over pandas Series; no defaults, no I/O."""

from __future__ import annotations

import pandas as pd


def donchian(high: pd.Series, low: pd.Series, n: int) -> pd.DataFrame:
    """Donchian channel over the **prior** ``n`` bars.

    ``upper[i] = max(high[i-n : i])`` and ``lower[i] = min(low[i-n : i])``: bar ``i`` itself is
    excluded (the whole window is shifted by one). A bar therefore breaks out of a channel it is
    not part of, and plotted on a chart the band steps rather than hugging the current bar.

    The first ``n`` rows are NaN (fewer than ``n`` prior bars); with fewer than ``n + 1`` bars in
    total every row is NaN and nothing can break out.
    """
    if n < 1:
        raise ValueError("channel length must be >= 1")
    upper = high.rolling(n, min_periods=n).max().shift(1)
    lower = low.rolling(n, min_periods=n).min().shift(1)
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: the largest of high-low, |high-prev close|, |low-prev close|.

    The first bar has no previous close, so its true range is just high-low.
    """
    prev_close = close.shift(1)
    parts = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return parts.max(axis=1, skipna=True)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Average true range with Wilder smoothing (an EMA with alpha = 1/n, seeded from the first
    true range). NaN until ``n`` bars exist."""
    if n < 1:
        raise ValueError("ATR length must be >= 1")
    return true_range(high, low, close).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
