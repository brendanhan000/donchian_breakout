"""Smoke test for the web app's JSON routes: TestClient + a monkeypatched fetcher,
no network, no schwab_hub."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from fastapi.testclient import TestClient

import donchian.api as api
from .helpers import raw_daily


def _fake_fetch(symbol, base, start, end):
    return raw_daily([date(2026, 1, 1) + timedelta(days=d) for d in range(70)])


def test_api_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_fetch", _fake_fetch)
    monkeypatch.setattr(api, "SETTINGS", replace(api.SETTINGS, data_dir=tmp_path))

    client = TestClient(api.app)

    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "1D" in [t["key"] for t in resp.json()["timeframes"]]

    resp = client.get("/api/bars", params={"symbol": "ZZTEST", "timeframe": "1D"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "ZZTEST"
    assert len(body["bars"]) > 0
    assert set(body["channels"]) == {"entry", "slow", "exit"}
    assert all(isinstance(p["value"], float) for p in body["channels"]["entry"]["upper"])

    bad = client.get("/api/bars", params={"symbol": "ZZTEST", "timeframe": "nope"})
    assert bad.status_code == 400


def test_api_universe_and_scan_routing(tmp_path, monkeypatch):
    """Scan filtering/lifecycle is covered in test_screen.py -- this just checks
    the HTTP routing and validation around it."""
    monkeypatch.setattr(api, "SETTINGS", replace(api.SETTINGS, data_dir=tmp_path))
    client = TestClient(api.app)

    assert client.get("/api/universe").status_code == 503  # no cached constituent list

    assert client.post("/api/scan", params={"timeframe": "nope"}).status_code == 400
    assert client.post("/api/scan", params={"indices": "nasdaq500"}).status_code == 400
    assert client.post("/api/scan", params={"direction": "sideways"}).status_code == 400
    assert client.post("/api/scan", params={"kind": "gap"}).status_code == 400

    assert client.get("/api/scan/does-not-exist").status_code == 404
    assert client.delete("/api/scan/does-not-exist").status_code == 404
