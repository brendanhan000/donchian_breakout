/* Donchian breakout chart: candles + entry/slow/exit channels + breakout markers,
 * plus a background scan across an index for fresh breakouts. */

const LWC = window.LightweightCharts;
const state = {
  config: null,
  timeframe: null,      // the /api/config entry for the loaded timeframe
  loadKey: null,        // `${symbol}:${timeframe}` of what's currently drawn
  viewMode: 'default',  // 'default' (auto-framed) | 'manual' (user panned/zoomed)
  lastBarCount: 0,
  suppressRangeEvent: false,
};
const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ time
 * Lightweight Charts renders time as UTC. Bars are UTC-stamped (bar start for
 * intraday, session date 00:00 UTC for daily/weekly): intraday needs an ET
 * shift to read as exchange time, daily/weekly are shown as a plain date. */
const _etOffsets = new Map();
const _etFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour12: false,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function etOffset(epochSec) {
  const hourKey = Math.floor(epochSec / 3600);
  if (_etOffsets.has(hourKey)) return _etOffsets.get(hourKey);
  const p = {};
  for (const { type, value } of _etFmt.formatToParts(new Date(epochSec * 1000))) p[type] = value;
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
  const off = Math.round((asUtc - epochSec) / 60) * 60;
  _etOffsets.set(hourKey, off);
  return off;
}

function utcDateString(epochSec) {
  const d = new Date(epochSec * 1000);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}

function chartTime(epochSec) {
  return state.timeframe && state.timeframe.is_intraday ? epochSec + etOffset(epochSec) : utcDateString(epochSec);
}

/* ------------------------------------------------------------------ chart */

const chart = LWC.createChart(el('chart'), {
  autoSize: true,
  layout: { background: { color: '#0b0d10' }, textColor: '#cfd3d8' },
  grid: { vertLines: { color: '#1b1f24' }, horzLines: { color: '#1b1f24' } },
  rightPriceScale: { borderColor: '#2a2f36' },
  timeScale: { borderColor: '#2a2f36', rightOffset: 4 },
});

const candles = chart.addCandlestickSeries();

function mkBand(color, style) {
  const opts = { color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false };
  return { upper: chart.addLineSeries(opts), lower: chart.addLineSeries(opts) };
}

const bands = {
  entry: mkBand('#2a78d6', LWC.LineStyle.Solid),
  slow: mkBand('#9a9fa6', LWC.LineStyle.Dashed),
  exit: mkBand('#eb6834', LWC.LineStyle.Dotted),
};

// How many bars the x-axis frames on arrival; scrolling/zooming reveals more
// history that's already loaded behind it. Roughly "half a year" per timeframe.
const VIEW_BARS = { '15m': 130, '1H': 110, '4H': 90, '1D': 180, '1W': 104 };

function frameDefaultView() {
  const n = state.lastBarCount;
  const view = (state.timeframe && VIEW_BARS[state.timeframe.key]) || 150;
  state.suppressRangeEvent = true;
  if (n <= view) {
    chart.timeScale().fitContent();
  } else {
    chart.timeScale().setVisibleLogicalRange({ from: n - view, to: n + 2 });
  }
  // Let the range-change event fire (and be ignored) before re-arming below.
  setTimeout(() => { state.suppressRangeEvent = false; }, 0);
}

chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
  if (state.suppressRangeEvent) return;
  state.viewMode = 'manual';
});

el('chart').addEventListener('dblclick', () => {
  state.viewMode = 'default';
  frameDefaultView();
});

/* ------------------------------------------------------------------ recents */

const RECENTS_KEY = 'donchian-recent-symbols';
const MAX_RECENTS = 8;

function loadRecents() {
  try { return JSON.parse(localStorage.getItem(RECENTS_KEY)) || []; } catch { return []; }
}

function pushRecent(symbol) {
  const list = [symbol, ...loadRecents().filter((s) => s !== symbol)].slice(0, MAX_RECENTS);
  try { localStorage.setItem(RECENTS_KEY, JSON.stringify(list)); } catch { /* private mode, etc. */ }
  renderRecents();
}

function renderRecents() {
  el('recents').innerHTML = loadRecents()
    .map((s) => `<button type="button" class="chip" data-symbol="${s}">${s}</button>`).join('');
}

el('recents').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.chip');
  if (!btn) return;
  el('symbol').value = btn.dataset.symbol;
  loadBars();
});

/* ------------------------------------------------------------------ data */

async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  state.config = cfg;
  const options = cfg.timeframes.map((t) => `<option value="${t.key}">${t.label}</option>`).join('');
  el('timeframe').innerHTML = options;
  el('timeframe').value = cfg.defaults.timeframe;
  el('scan-timeframe').innerHTML = options;
  el('scan-timeframe').value = cfg.defaults.timeframe;
  el('symbol').value = cfg.defaults.symbol;
  renderRecents();
}

function eventMarker(e) {
  const long = e.direction === 'long';
  const close = e.kind === 'close';
  return {
    time: chartTime(e.ts),
    position: long ? 'belowBar' : 'aboveBar',
    color: close ? (long ? '#1baf7a' : '#e34948') : (long ? '#7fd6b8' : '#f0a8a5'),
    shape: close ? (long ? 'arrowUp' : 'arrowDown') : 'circle',
    text: close ? '' : 'wick',
  };
}

function renderEventTable(events) {
  const body = document.querySelector('#events tbody');
  body.innerHTML = events.slice().reverse().slice(0, 100).map((e) => `
    <tr class="${e.direction}">
      <td>${new Date(e.ts * 1000).toISOString().replace('T', ' ').slice(0, 16)}</td>
      <td class="dir">${e.direction}</td>
      <td>${e.kind}${e.complete ? '' : ' (forming)'}</td>
      <td>${e.entry_price.toFixed(2)}</td>
      <td>${e.width_atr == null ? '' : e.width_atr.toFixed(2)}</td>
      <td>${e.volume_ratio == null ? '' : e.volume_ratio.toFixed(2)}</td>
      <td>${e.agrees_slow ? '✓' : ''}</td>
      <td>${e.false_breakout == null ? '…' : e.false_breakout ? '✗' : '✓'}</td>
    </tr>`).join('');
}

const REFRESH_MS = 60_000;
let refreshTimer = null;

async function loadBars() {
  const symbol = el('symbol').value.trim().toUpperCase();
  const timeframe = el('timeframe').value;
  if (!symbol) return;
  state.timeframe = state.config.timeframes.find((t) => t.key === timeframe);
  el('status').textContent = 'loading…';

  let data;
  try {
    const resp = await fetch(`/api/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`);
    data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
  } catch (err) {
    el('status').textContent = `error: ${err.message}`;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(loadBars, REFRESH_MS);
    return;
  }

  candles.setData(data.bars.map((b) => ({
    time: chartTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close,
  })));
  for (const [name, series] of Object.entries(bands)) {
    series.upper.setData(data.channels[name].upper.map((p) => ({ time: chartTime(p.time), value: p.value })));
    series.lower.setData(data.channels[name].lower.map((p) => ({ time: chartTime(p.time), value: p.value })));
  }
  candles.setMarkers(data.events.map(eventMarker));
  renderEventTable(data.events);

  const key = `${symbol}:${timeframe}`;
  const freshLoad = key !== state.loadKey;
  state.loadKey = key;
  state.lastBarCount = data.bars.length;
  if (freshLoad) state.viewMode = 'default';
  if (state.viewMode === 'default') frameDefaultView();
  // else: the user has panned/zoomed manually -- a background refresh leaves it alone.

  if (freshLoad) pushRecent(symbol);
  el('status').textContent = `${data.symbol} · ${data.bars.length} bars · cache ${data.cache.status} (${data.cache.rows_total} rows)`;

  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadBars, REFRESH_MS);
}

el('controls').addEventListener('submit', (ev) => { ev.preventDefault(); loadBars(); });

/* ------------------------------------------------------------------ scan */

let scanJobId = null;
let scanTimer = null;

function resetScanButtons() {
  el('scan-start').disabled = false;
  el('scan-cancel').hidden = true;
}

function renderScanTable(rows) {
  const timeframe = el('scan-timeframe').value;
  document.querySelector('#scan-results tbody').innerHTML = rows.map((r) => `
    <tr class="${r.direction}" data-symbol="${r.symbol}" data-timeframe="${timeframe}">
      <td>${r.symbol}</td>
      <td>${r.sector || ''}</td>
      <td class="dir">${r.direction}</td>
      <td>${r.kind}${r.complete ? '' : ' (forming)'}</td>
      <td>${r.bars_ago}</td>
      <td>${r.entry_price.toFixed(2)}</td>
      <td>${r.width_atr == null ? '' : r.width_atr.toFixed(2)}</td>
      <td>${r.volume_ratio == null ? '' : r.volume_ratio.toFixed(2)}</td>
      <td>${r.agrees_slow ? '✓' : ''}</td>
    </tr>`).join('');
}

async function pollScan() {
  if (!scanJobId) return;
  const resp = await fetch(`/api/scan/${scanJobId}`);
  const job = await resp.json();
  if (!resp.ok) {
    el('scan-status').textContent = `error: ${job.error || resp.statusText}`;
    scanJobId = null;
    resetScanButtons();
    return;
  }

  el('scan-bar').style.width = `${job.progress.pct}%`;
  el('scan-status').textContent = `${job.phase} — ${job.progress.done}/${job.progress.total} · ${job.found} found`;
  renderScanTable(job.rows || []);

  if (job.state === 'running' || job.state === 'pending') {
    scanTimer = setTimeout(pollScan, 1000);
    return;
  }
  el('scan-status').textContent = `${job.state} — ${job.found} found (${job.error_count} errors)`;
  scanJobId = null;
  resetScanButtons();
}

async function startScan(ev) {
  ev.preventDefault();
  const indices = [];
  if (el('scan-sp500').checked) indices.push('sp500');
  if (el('scan-nasdaq100').checked) indices.push('nasdaq100');
  if (!indices.length) {
    el('scan-status').textContent = 'pick at least one index';
    return;
  }

  const params = new URLSearchParams({
    timeframe: el('scan-timeframe').value,
    indices: indices.join(','),
    direction: el('scan-direction').value,
    kind: el('scan-kind').value,
    within_bars: el('scan-within').value,
  });

  el('scan-start').disabled = true;
  el('scan-cancel').hidden = false;
  el('scan-status').textContent = 'starting…';
  el('scan-bar').style.width = '0%';
  document.querySelector('#scan-results tbody').innerHTML = '';
  clearTimeout(scanTimer);

  let job;
  try {
    const resp = await fetch(`/api/scan?${params}`, { method: 'POST' });
    job = await resp.json();
    if (!resp.ok) throw new Error(job.error || resp.statusText);
  } catch (err) {
    el('scan-status').textContent = `error: ${err.message}`;
    resetScanButtons();
    return;
  }
  scanJobId = job.id;
  pollScan();
}

async function cancelScan() {
  if (!scanJobId) return;
  await fetch(`/api/scan/${scanJobId}`, { method: 'DELETE' });
}

el('scan-controls').addEventListener('submit', startScan);
el('scan-cancel').addEventListener('click', cancelScan);
el('scan-results').addEventListener('click', (ev) => {
  const row = ev.target.closest('tr[data-symbol]');
  if (!row) return;
  el('symbol').value = row.dataset.symbol;
  el('timeframe').value = row.dataset.timeframe;
  loadBars();
});

(async function init() {
  await loadConfig();
  await loadBars();
})();
