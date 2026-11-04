"""The only place this project asks Schwab for anything, and it does so through schwab_hub.

No HTTP to Schwab, no tokens, no keys: `schwab_hub_client.HubClient` talks to the local hub, which
owns the credentials and the token refresh. This module adds what the hub deliberately does not:
retry with backoff on throttling (429, and the 403 Schwab's edge protection returns), and turning
candles into a DataFrame. Regular-hours bars only: extended-hours prints manufacture false breakouts.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Callable

import pandas as pd
import requests
from schwab_hub_client import HubClient, HubError

from .config import SETTINGS, Settings
from .timeframes import BASES

RAW_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]

# Statuses worth retrying: throttling (429; 403 is what Schwab's edge protection sends when pushed
# too hard) and transient upstream failures.
RETRY_STATUSES = {429, 403, 500, 502, 503, 504}


class HubUnavailable(RuntimeError):
    """schwab_hub is not running."""


class HubAuthError(RuntimeError):
    """The hub has no valid Schwab token: run `../schwab_hub/run.sh login`."""


class NoData(LookupError):
    """Schwab answered 200 with no candles (bad symbol, or nothing in that window)."""


class FetchFailed(RuntimeError):
    """A request failed and was not (or no longer) retryable."""


def to_schwab_symbol(symbol: str) -> str:
    """Share classes are `BRK/B` on Schwab; `BRK.B` silently returns no candles."""
    return symbol.strip().upper().replace(".", "/")


class PriceFetcher:
    """Callable ``(symbol, base, start, end) -> raw candle DataFrame`` (Schwab's own shape)."""

    def __init__(
        self,
        hub=None,
        settings: Settings = SETTINGS,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.hub = hub if hub is not None else HubClient(url=settings.hub_url)
        self.settings = settings
        self._sleep = sleep
        self._jitter = jitter

    def __call__(self, symbol: str, base: str, start: datetime, end: datetime) -> pd.DataFrame:
        b = BASES[base]
        sym = to_schwab_symbol(symbol)
        last: str = ""
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = self.hub.get_price_history(
                    sym,
                    frequency_type=b.frequency_type,
                    frequency=b.frequency,
                    start_datetime=start,
                    end_datetime=end,
                    need_extended_hours_data=False,  # regular session only
                    need_previous_close=False,
                )
            except HubError as exc:
                raise HubUnavailable(str(exc)) from exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json(), sym, base)
                if resp.status_code in (401, 503) and _is_hub_auth(resp):
                    raise HubAuthError(_hub_message(resp))
                last = f"HTTP {resp.status_code}: {resp.text[:160]}"
                if resp.status_code not in RETRY_STATUSES:
                    raise FetchFailed(f"{sym} {base}: {last}")
            if attempt < self.settings.max_retries:
                self._sleep(self.settings.backoff_seconds * (2**attempt) * (0.5 + self._jitter()))
        raise FetchFailed(f"{sym} {base}: gave up after {self.settings.max_retries + 1} attempts ({last})")

    @staticmethod
    def _parse(payload: dict, symbol: str, base: str) -> pd.DataFrame:
        candles = payload.get("candles") or []
        if not candles:
            raise NoData(
                f"Schwab returned no {base} candles for {symbol} "
                "(check the symbol; share classes use '/', e.g. BRK/B)"
            )
        df = pd.DataFrame(candles)
        missing = set(RAW_COLUMNS) - set(df.columns)
        if missing:
            raise FetchFailed(f"{symbol} {base}: candle payload is missing {sorted(missing)}")
        df = df[RAW_COLUMNS].copy()
        df["datetime"] = df["datetime"].astype("int64")
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype("float64")
        df["volume"] = pd.to_numeric(df["volume"]).fillna(0).astype("int64")
        return df.sort_values("datetime").drop_duplicates("datetime", keep="last").reset_index(drop=True)


def _is_hub_auth(resp) -> bool:
    try:
        return resp.json().get("error") in {"schwab_auth", "hub_not_configured"}
    except ValueError:
        return False


def _hub_message(resp) -> str:
    try:
        body = resp.json()
        return f"{body.get('detail', '')} {body.get('hint', '')}".strip()
    except ValueError:
        return resp.text[:200]
