"""Run the web app (`python -m donchian`) or warm the bar cache
(`python -m donchian fetch [SYMBOL ...]`, default: the configured default symbol)."""

from __future__ import annotations

import sys

import uvicorn

from .config import SETTINGS
from .hub import PriceFetcher
from .store import update
from .timeframes import BASES


def _fetch_cmd(symbols: list[str]) -> None:
    symbols = symbols or [SETTINGS.default_symbol]
    fetch = PriceFetcher(settings=SETTINGS)
    for symbol in symbols:
        for base in BASES:
            result = update(symbol, base, fetch, SETTINGS)
            print(f"{symbol:<8} {base:<4} {result.status:<11} +{result.rows_added:<5} ({result.rows_total} total)")


def _serve() -> None:
    print(f"Donchian breakout -> http://{SETTINGS.host}:{SETTINGS.port}")
    uvicorn.run("donchian.api:app", host=SETTINGS.host, port=SETTINGS.port, reload=False)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "fetch":
        _fetch_cmd(argv[1:])
    else:
        _serve()


if __name__ == "__main__":
    main()
