# Donchian Breakout

An interactive Donchian-channel breakout chart, plus a background scanner that
runs the same detector across the S&P 500 / Nasdaq-100 and surfaces fresh
breakouts — modeled on this account's `relative_value_charts` (chart interaction
and the screener), backed by Schwab price history.

## Status

**Working:**
- Channel/ATR math, breakout detection (close vs. wick, false-breakout tracking,
  volume ratio, slow-channel agreement), session-aware timeframe resampling
  (15m/1H/4H/1D/1W), and the parquet bar cache with incremental updates.
- A FastAPI + [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
  chart: candles + entry/slow/exit bands + breakout markers, framed to a sensible
  default window per timeframe with more history loaded behind it. Scroll/drag to
  pan and zoom; double-click to fit everything loaded. The chart polls for new
  bars every 60s and **leaves your pan/zoom alone** once you've moved it — only a
  fresh symbol/timeframe reframes automatically. Recently viewed symbols are
  remembered as chips.
- A background **scan**: pick a timeframe, S&P 500 and/or Nasdaq-100, direction
  (long/short), kind (close/wick), and "within N bars", and it runs the detector
  across the whole roster with a progress bar, surfacing one row per symbol with
  a qualifying breakout. Click a row to load that symbol on the chart. Fetching
  reuses the same cache + retry/backoff as the single-symbol chart, spread across
  a bounded thread pool (`DONCHIAN_FETCH_WORKERS`) — no separate rate limiter was
  needed since `hub.PriceFetcher` already backs off on 429/403/5xx.

All of the above is covered by `tests/`.

**Not built yet:**
- No alerting (email/push) on a new breakout — the scan is look-at-it-yourself.
- No live tick-level quotes (the chart's 60s bar poll is as live as it gets).
- The scan is capped to S&P 500 / Nasdaq-100 membership; no custom watchlist.

## Run

Schwab access is handled by the central **schwab_hub** (`../schwab_hub`), which owns
the credentials. This project holds no Schwab secrets.

```bash
../schwab_hub/run.sh login   # first time, then weekly (Schwab expires refresh tokens after 7 days)
../schwab_hub/run.sh &       # leave running

../.venv/bin/python -m donchian
```

Open <http://127.0.0.1:8183> (or whatever `HOST`/`PORT` you set in `.env` — see
`.env.example`). Type a symbol, pick a timeframe, hit **Load**.

Pre-warm the cache for a symbol (all three base resolutions) without opening the
server:

```bash
../.venv/bin/python -m donchian fetch QQQ SPY
```

### Scanning

The scan panel needs the S&P 500 / Nasdaq-100 constituent list cached once:

```bash
../.venv/bin/python -m donchian.refresh_universe
```

Re-run it whenever you want current membership — the scanner reads whatever is
cached at `data/universe.json` and never touches the network itself. A scan of
~520 symbols takes a couple of minutes at the default 8-way concurrency; both
the timeframe and the roster in the panel are yours to shrink for a quicker look.

## Test

```bash
../.venv/bin/python -m pytest
```

Everything runs offline against synthetic bars — no network, no credentials.
`tests/test_api.py` monkeypatches the Schwab fetch call to exercise the web routes
the same way.

## How it fits together

```
donchian/
  channel.py         Donchian channel + ATR/true-range, pure functions over pandas
  events.py          breakout detection: close vs. wick, false-breakout lookahead,
                     volume ratio, agreement with the slow channel
  timeframes.py      base bars (15m/30m/1d) -> session-anchored 15m/1H/4H/1D/1W
                     frames, with a "complete" flag so a forming bar is never read
                     as finished
  hub.py             talks to schwab_hub for candles; retry/backoff on 429/403/5xx
  store.py           parquet cache per (base, symbol), incremental updates,
                     rebuilds in full if Schwab restates history (a split)
  universe.py        S&P 500 / Nasdaq-100 membership, cached to data/universe.json
  refresh_universe.py   refresh the cached constituent list
  screen.py          background scan job: runs the detector across a roster on a
                     bounded thread pool, reusing hub + store as-is
  config.py          .env-backed settings — the one place tunables live
  api.py             FastAPI routes + static file serving
  __main__.py        `python -m donchian` (serve) / `... fetch SYMBOL...` (cache)
web/
  index.html, app.js, style.css
  vendor/            TradingView Lightweight Charts (vendored, Apache-2.0)
tests/
  test_channel.py, test_events.py, test_timeframes.py   pure-math, offline
  test_universe.py, test_screen.py, test_api.py         cache/scan/web, offline
```

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/config` | timeframes, detector params, defaults |
| `GET /api/bars` | `symbol`, `timeframe` -> OHLCV bars, entry/slow/exit channel lines, breakout events |
| `GET /api/universe` | cached constituent-list summary (counts, fetch date) |
| `POST /api/scan` | start a scan — `timeframe`, `indices` (`sp500,nasdaq100`), `direction`, `kind`, `within_bars`, optional `min_width_atr`/`limit` |
| `GET /api/scan/{id}` | progress + found rows (`?rows=false` for progress only) |
| `DELETE /api/scan/{id}` | cancel a running scan |

Channels and events are computed over the **full** cached history (so the slow
channel and the false-breakout lookahead are never starved by the display window)
and only trimmed to `DONCHIAN_CHART_LOOKBACK_BARS` in the `/api/bars` response.
A scan reports the single most recent qualifying breakout per symbol, using the
same full-history computation.

### Breakout events

Two kinds, reported separately: `close` (the bar closed beyond the prior channel)
and `wick` (an intrabar poke that closed back inside — a rejected breakout). Each
event carries how far beyond the level it went in ATR units, the bar's volume
against its recent average, whether the slow channel agrees, and whether price
closed back inside the broken channel within `DONCHIAN_FALSE_BREAK_K` bars
(`false_breakout`; `null`/`…` while still undecided).

## Configuration

All tunables live in `.env` (see `.env.example` for the full list with defaults):
detector lengths (`DONCHIAN_ENTRY_N` / `_SLOW_N` / `_EXIT_N` / `_ATR_N` /
`_VOLUME_N` / `_FALSE_BREAK_K`), the default symbol/timeframe, cache location and
history depth, fetch concurrency/retries, and the half-day session calendar
(`DONCHIAN_EARLY_CLOSES` — extend it each year).
