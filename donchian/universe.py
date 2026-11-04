"""Index membership for the scanner.

Schwab has no index-constituent endpoint, so the S&P 500 / Nasdaq-100 lists come
from a public dataset and are cached to disk. The cache is the source of truth at
runtime -- the network is only touched on an explicit refresh -- so a scan never
depends on a third party being up.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import SETTINGS


def universe_path() -> Path:
    """Resolved fresh from `SETTINGS.data_dir` on every call (not frozen at
    import), matching store.path_for -- so overriding `SETTINGS` for a test
    or a different data dir actually takes effect."""
    return SETTINGS.data_dir / "universe.json"

# Monthly-refreshed constituent lists. Symbol + name only.
SOURCES = {
    "sp500": "https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/constituents-sp500.csv",
    "nasdaq100": "https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/constituents-nasdaq100.csv",
}
# Best-effort GICS sector enrichment; the scanner works fine without it.
SECTOR_SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


@dataclass(frozen=True)
class Member:
    symbol: str  # Schwab-formatted
    name: str
    sector: str = ""
    indices: tuple[str, ...] = field(default_factory=tuple)


def to_schwab_symbol(symbol: str) -> str:
    """Dataset tickers use `BRK.B`; Schwab wants `BRK/B` for share classes."""
    return symbol.strip().upper().replace(".", "/")


# Telltales of UTF-8 bytes that were decoded as latin-1 somewhere upstream.
_MOJIBAKE_MARKS = ("Ã", "Â", "â€")


def repair_text(text: str) -> str:
    """Undo a latin-1/UTF-8 double encoding, e.g. 'EstÃ©e' -> 'Estée'."""
    if not any(mark in text for mark in _MOJIBAKE_MARKS):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": "donchian-breakout/1.0"}, timeout=30)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")  # not all endpoints declare a charset


def _parse_symbol_name(text: str) -> list[tuple[str, str]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
        name = repair_text((row.get("Name") or row.get("Security") or row.get("name") or "").strip())
        if symbol:
            out.append((symbol, name))
    return out


def _parse_sectors(text: str) -> dict[str, str]:
    sectors: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip()
        sector = repair_text((row.get("GICS Sector") or "").strip())
        if symbol and sector:
            sectors[symbol.upper()] = sector
    return sectors


def fetch_universe() -> dict[str, Any]:
    """Pull fresh constituent lists. Raises on network failure."""
    texts = {index: _get(url) for index, url in SOURCES.items()}
    try:
        sectors = _parse_sectors(_get(SECTOR_SOURCE))
    except Exception:  # enrichment only -- never fail the refresh over it
        sectors = {}

    members_by_symbol: dict[str, dict[str, Any]] = {}
    for index, text in texts.items():
        parsed = _parse_symbol_name(text)
        if not parsed:
            raise ValueError(f"{index} source returned no rows; refusing to cache it.")
        for symbol, name in parsed:
            key = symbol.upper()
            entry = members_by_symbol.setdefault(key, {"symbol": key, "name": name, "sector": "", "indices": []})
            if name and not entry["name"]:
                entry["name"] = name
            if index not in entry["indices"]:
                entry["indices"].append(index)

    for key, entry in members_by_symbol.items():
        entry["sector"] = sectors.get(key, "")

    counts = {index: sum(1 for e in members_by_symbol.values() if index in e["indices"]) for index in SOURCES}
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": SOURCES,
        "counts": counts,
        "members": sorted(members_by_symbol.values(), key=lambda e: e["symbol"]),
    }


def save_universe(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or universe_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1))
    return target


_cached: dict[str, Any] | None = None
_cached_mtime: float | None = None


def load_universe(path: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    """Read the cached constituent lists, re-reading when the file changes.

    The mtime check means `python -m donchian.refresh_universe` takes effect on
    the next scan even while the server keeps running -- no restart, no stale list.
    """
    global _cached, _cached_mtime
    target = path or universe_path()
    if not target.exists():
        raise FileNotFoundError(f"No constituent cache at {target}. Run `python -m donchian.refresh_universe`.")
    mtime = target.stat().st_mtime
    if _cached is not None and not force and mtime == _cached_mtime:
        return _cached
    _cached = json.loads(target.read_text())
    _cached_mtime = mtime
    return _cached


def members(universe: dict[str, Any] | None = None, *, indices: tuple[str, ...] = ("sp500", "nasdaq100")) -> list[Member]:
    """Members of the requested indices, with Schwab-formatted symbols."""
    data = universe if universe is not None else load_universe()
    out = []
    for entry in data.get("members", []):
        entry_indices = tuple(entry.get("indices", []))
        if not any(i in entry_indices for i in indices):
            continue
        out.append(Member(symbol=to_schwab_symbol(entry["symbol"]), name=entry.get("name", ""),
                           sector=entry.get("sector", ""), indices=entry_indices))
    return out


def summary(universe: dict[str, Any] | None = None) -> dict[str, Any]:
    data = universe if universe is not None else load_universe()
    return {"fetched_at": data.get("fetched_at"), "counts": data.get("counts", {}),
            "total": len(data.get("members", []))}
