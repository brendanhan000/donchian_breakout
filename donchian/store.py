"""Parquet cache of Schwab bars, keyed by (base, symbol), with incremental updates.

One file per (base, symbol) under ``data/bars/<base>/<SYMBOL>.parquet`` holding Schwab's raw candle
shape once (``datetime`` epoch-ms, open, high, low, close, volume). Every timeframe is derived from
these at read time. The file's schema metadata records ``fetched_at`` (epoch ms), the moment the data
was retrieved, which is what decides whether the newest bar could be complete.

A rerun never re-pulls full history: it pulls a small overlap, appends, and only rebuilds from
scratch if Schwab has *restated* history (a split) since the cache was written.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings

COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]
FETCHED_AT = b"fetched_at_ms"

Fetcher = Callable[[str, str, datetime, datetime], pd.DataFrame]


def safe_symbol(symbol: str) -> str:
    """Filename-safe symbol: `BRK/B` -> `BRK_B`."""
    return symbol.strip().upper().replace("/", "_").replace(".", "_")


def path_for(settings: Settings, symbol: str, base: str) -> Path:
    return settings.bars_dir / base / f"{safe_symbol(symbol)}.parquet"


def load(path: Path) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Cached raw candles and the time they were fetched (UTC), or an empty frame and None."""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS), None
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    fetched = None
    if FETCHED_AT in meta:
        fetched = pd.Timestamp(int(meta[FETCHED_AT]), unit="ms", tz="UTC")
    return table.to_pandas()[COLUMNS], fetched


def save(path: Path, df: pd.DataFrame, fetched_at: datetime) -> None:
    """Atomic write: a reader sees the old file or the new one, never a partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df[COLUMNS], preserve_index=False)
    table = table.replace_schema_metadata({FETCHED_AT: str(int(fetched_at.timestamp() * 1000)).encode()})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".parquet")
    os.close(fd)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class UpdateResult:
    symbol: str
    base: str
    status: str  # new | incremental | refetched
    rows_added: int
    rows_total: int


def update(
    symbol: str,
    base: str,
    fetch: Fetcher,
    settings: Settings,
    now: datetime | None = None,
) -> UpdateResult:
    """Bring one (symbol, base) cache up to date.

    * no cache: pull the full window (daily: ``daily_history_years``; minute: ``intraday_lookback_days``);
    * cache present: pull only from a small overlap before the last cached bar and append;
    * if any cached close *before the last bar* disagrees with the re-pull by more than
      ``split_rtol`` (Schwab restated history for a split), or the overlap doesn't line up, the whole
      window is rebuilt. The last cached bar is excluded from that check because it may have been
      partial when it was cached.

    ``fetched_at`` is refreshed on every update, even when no rows were added.
    """
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=1)
    path = path_for(settings, symbol, base)
    cached, _ = load(path)

    def full() -> pd.DataFrame:
        days = settings.daily_history_years * 366 if base == "1d" else settings.intraday_lookback_days
        return fetch(symbol, base, now - timedelta(days=days), end)

    if cached.empty:
        df, status = full(), "new"
    else:
        last = int(cached["datetime"].iloc[-1])
        overlap = settings.overlap_days_daily if base == "1d" else settings.overlap_days_intraday
        start = datetime.fromtimestamp(last / 1000, tz=timezone.utc) - timedelta(days=overlap)
        fresh = fetch(symbol, base, start, end)
        prior = cached[cached["datetime"] < last]
        both = prior.merge(fresh[["datetime", "close"]], on="datetime", suffixes=("", "_new"))
        restated = both.empty or ((both["close_new"] / both["close"] - 1).abs() > settings.split_rtol).any()
        if restated:
            df, status = full(), "refetched"
        else:
            keep = cached[cached["datetime"] < int(fresh["datetime"].min())]
            df = pd.concat([keep, fresh], ignore_index=True)
            status = "incremental"

    df = df.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    save(path, df, now)
    return UpdateResult(symbol, base, status, len(df) - len(cached), len(df))
