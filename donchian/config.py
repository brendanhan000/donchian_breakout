"""Runtime configuration, loaded from .env / environment. The one place tunables live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _dates(name: str, default: str) -> frozenset[date]:
    raw = os.getenv(name, "") or default
    return frozenset(date.fromisoformat(p.strip()) for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    # Schwab data comes from the central schwab_hub, which owns all credentials.
    hub_url: str = os.getenv("SCHWAB_HUB_URL", "http://127.0.0.1:8765")

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _int("PORT", 8183)

    # Detector defaults: entry channel, slow channel, exit channel, ATR, volume average,
    # and how many bars after a breakout a close back inside the channel makes it "false".
    entry_n: int = _int("DONCHIAN_ENTRY_N", 20)
    slow_n: int = _int("DONCHIAN_SLOW_N", 55)
    exit_n: int = _int("DONCHIAN_EXIT_N", 10)
    atr_n: int = _int("DONCHIAN_ATR_N", 14)
    volume_n: int = _int("DONCHIAN_VOLUME_N", 20)
    false_break_k: int = _int("DONCHIAN_FALSE_BREAK_K", 3)

    default_symbol: str = os.getenv("DONCHIAN_DEFAULT_SYMBOL", "QQQ")
    default_timeframe: str = os.getenv("DONCHIAN_DEFAULT_TIMEFRAME", "1D")
    chart_lookback_bars: int = _int("DONCHIAN_CHART_LOOKBACK_BARS", 400)

    # Cache. Bars are stored exactly as Schwab returns them, one parquet per (base, symbol).
    data_dir: Path = ROOT / os.getenv("DONCHIAN_DATA_DIR", "data")
    daily_history_years: int = _int("DONCHIAN_DAILY_HISTORY_YEARS", 10)
    # Schwab documents "nine months" of 5/10/15/30m bars and measured ~258 days on 2026-09-20.
    # We ask for 270 and take whatever it returns.
    intraday_lookback_days: int = _int("DONCHIAN_INTRADAY_LOOKBACK_DAYS", 270)
    # Incremental updates re-pull this much overlap to catch the last (possibly partial) bar
    # and to detect split restatements.
    overlap_days_daily: int = _int("DONCHIAN_OVERLAP_DAYS_DAILY", 15)
    overlap_days_intraday: int = _int("DONCHIAN_OVERLAP_DAYS_INTRADAY", 5)
    # Schwab restates history after a split; a cached close that moved more than this on
    # re-pull means the cache is stale and gets rebuilt in full.
    split_rtol: float = _float("DONCHIAN_SPLIT_RTOL", 0.001)

    # Fetching. The hub caps concurrent upstream calls at 8; relative_value_charts measured
    # 16 concurrent as clean and 24 as tripping Schwab's edge protection (429, then minutes of 403).
    fetch_workers: int = _int("DONCHIAN_FETCH_WORKERS", 8)
    max_retries: int = _int("DONCHIAN_MAX_RETRIES", 4)
    backoff_seconds: float = _float("DONCHIAN_BACKOFF_SECONDS", 1.5)

    # A bar counts as complete only this long after it should have ended.
    bar_settle_seconds: int = _int("DONCHIAN_BAR_SETTLE_SECONDS", 60)
    # 13:00 ET closes. Update yearly (NYSE calendar).
    early_close_dates: frozenset[date] = _dates("DONCHIAN_EARLY_CLOSES", "2026-11-27,2026-12-24")

    @property
    def bars_dir(self) -> Path:
        return self.data_dir / "bars"


SETTINGS = Settings()
