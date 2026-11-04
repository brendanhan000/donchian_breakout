"""Breakout event detection. Vectorised over bars; loops only over the 4 (direction, kind) combos
and the K look-ahead offsets. All parameters are passed in (see config.Settings): nothing here is
hardcoded, and nothing here knows about symbols, paths or Schwab.

Two kinds of breakout are reported separately, never merged:

``close``  the bar CLOSED beyond the prior channel (close > prior upper, or close < prior lower).
``wick``   the bar's high/low poked beyond the prior channel but it did NOT close beyond it
           (an intrabar touch that was rejected). A close-confirmed breakout is not repeated as a
           wick event, so the two kinds never overlap.

A single wide bar can break both sides (a long ``wick`` and a short ``close``, say); both are
emitted, one row each.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .channel import atr, donchian

EVENT_COLUMNS = [
    "symbol",
    "ts",
    "bar",
    "direction",
    "kind",
    "entry_price",
    "prior_high",
    "prior_low",
    "channel_width",
    "width_atr",
    "penetration_atr",
    "bars_since_prev",
    "volume_ratio",
    "agrees_slow",
    "false_breakout",
    "complete",
]


@dataclass(frozen=True)
class DetectorParams:
    """Detector inputs. No defaults on purpose: callers take them from config."""

    entry_n: int  # entry channel length
    slow_n: int  # slow channel used for the agreement flag
    atr_n: int
    volume_n: int
    false_break_k: int


def channels(df: pd.DataFrame, entry_n: int, slow_n: int, exit_n: int) -> dict[str, pd.DataFrame]:
    """The three Donchian channels (each shifted by one bar) for display."""
    return {
        "entry": donchian(df["high"], df["low"], entry_n),
        "slow": donchian(df["high"], df["low"], slow_n),
        "exit": donchian(df["high"], df["low"], exit_n),
    }


def detect(
    df: pd.DataFrame,
    symbol: str,
    params: DetectorParams,
    include_incomplete: bool = False,
) -> pd.DataFrame:
    """Breakout events for one symbol/timeframe frame.

    ``df`` has a DatetimeIndex, float ``open/high/low/close/volume`` and (optionally) a boolean
    ``complete``. A bar that is still forming (``complete == False``) never produces an event
    unless ``include_incomplete`` is set, in which case the event carries ``complete=False``.

    Per event (column: meaning):

    entry_price      close for a ``close`` event; for a ``wick`` event the channel level that was
                     touched, or the open if the bar gapped past it.
    prior_high/low   the channel that was broken (bars i-N .. i-1), and channel_width = high - low.
    width_atr        channel_width / ATR, using the ATR of the PRIOR bar so the breakout bar's own
                     range cannot inflate the denominator.
    penetration_atr  how far beyond the broken level, in ATR units: measured from the close for a
                     ``close`` event and from the extreme (high/low) for a ``wick`` event.
    bars_since_prev  bars since the previous event of the same direction AND kind (NaN for the first).
    volume_ratio     the bar's volume / mean volume of the prior ``volume_n`` bars.
    agrees_slow      the same measure (close or extreme, per kind) also beyond the prior SLOW
                     channel on the same bar. False if the slow channel is not yet warmed up.
    false_breakout   True if any of the next K complete bars closed back inside the broken channel
                     (long: close <= prior high; short: close >= prior low); False if K complete
                     bars followed and none did; <NA> while that is still undecided.
    """
    o, h, l, c, v = (df[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close", "volume"))
    n = len(df)
    complete = df["complete"].to_numpy(dtype=bool) if "complete" in df.columns else np.ones(n, dtype=bool)
    eligible = np.ones(n, dtype=bool) if include_incomplete else complete

    entry = donchian(df["high"], df["low"], params.entry_n)
    slow = donchian(df["high"], df["low"], params.slow_n)
    up, lo = entry["upper"].to_numpy(), entry["lower"].to_numpy()
    slow_up, slow_lo = slow["upper"].to_numpy(), slow["lower"].to_numpy()
    prior_atr = atr(df["high"], df["low"], df["close"], params.atr_n).shift(1).to_numpy()
    prior_vol = (
        df["volume"].rolling(params.volume_n, min_periods=params.volume_n).mean().shift(1).to_numpy()
    )

    with np.errstate(invalid="ignore", divide="ignore"):
        long_close = (c > up) & eligible
        short_close = (c < lo) & eligible
        long_wick = (h > up) & ~(c > up) & eligible
        short_wick = (l < lo) & ~(c < lo) & eligible

        frames = []
        for direction, kind, mask in (
            ("long", "close", long_close),
            ("long", "wick", long_wick),
            ("short", "close", short_close),
            ("short", "wick", short_wick),
        ):
            pos = np.flatnonzero(mask)
            if pos.size == 0:
                continue
            is_long = direction == "long"
            sign = 1.0 if is_long else -1.0
            level = up[pos] if is_long else lo[pos]
            extreme = c[pos] if kind == "close" else (h[pos] if is_long else l[pos])
            if kind == "close":
                entry_price = c[pos]
            else:
                entry_price = np.maximum(level, o[pos]) if is_long else np.minimum(level, o[pos])
            width = up[pos] - lo[pos]
            slow_level = slow_up[pos] if is_long else slow_lo[pos]
            agrees = (extreme > slow_level) if is_long else (extreme < slow_level)

            since = np.full(pos.size, np.nan)
            since[1:] = np.diff(pos)

            # Did price close back inside the broken channel within K complete bars?
            inside_any = np.zeros(pos.size, dtype=bool)
            all_seen = np.ones(pos.size, dtype=bool)
            usable = np.ones(n, dtype=bool) if include_incomplete else complete
            for j in range(1, params.false_break_k + 1):
                nxt = pos + j
                seen = (nxt < n) & usable[np.minimum(nxt, n - 1)]
                nc = c[np.minimum(nxt, n - 1)]
                back = (nc <= level) if is_long else (nc >= level)
                inside_any |= seen & back
                all_seen &= seen
            false_break = pd.arrays.BooleanArray(inside_any, ~(inside_any | all_seen))

            frames.append(
                pd.DataFrame(
                    {
                        "symbol": symbol,
                        "ts": df.index[pos],
                        "bar": pos,
                        "direction": direction,
                        "kind": kind,
                        "entry_price": entry_price,
                        "prior_high": up[pos],
                        "prior_low": lo[pos],
                        "channel_width": width,
                        "width_atr": width / prior_atr[pos],
                        "penetration_atr": sign * (extreme - level) / prior_atr[pos],
                        "bars_since_prev": since,
                        "volume_ratio": v[pos] / prior_vol[pos],
                        "agrees_slow": agrees,
                        "false_breakout": false_break,
                        "complete": complete[pos],
                    }
                )
            )

    if not frames:
        return pd.DataFrame({c_: pd.Series(dtype="object") for c_ in EVENT_COLUMNS})
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["bar", "direction", "kind"], kind="stable").reset_index(drop=True)[EVENT_COLUMNS]
