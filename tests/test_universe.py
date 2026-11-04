"""Constituent-list parsing and caching. No network -- fetch_universe() itself
(the only part that touches GitHub) is intentionally left untested here."""
from __future__ import annotations

from dataclasses import replace

import pytest

import donchian.universe as universe
from donchian.universe import Member, load_universe, members, repair_text, save_universe, to_schwab_symbol


def test_to_schwab_symbol():
    assert to_schwab_symbol(" brk.b ") == "BRK/B"
    assert to_schwab_symbol("AAPL") == "AAPL"


def test_repair_text_only_touches_mojibake():
    assert repair_text("EstÃ©e") == "Estée"
    assert repair_text("Estée") == "Estée"  # already correct: left alone
    assert repair_text("Apple Inc.") == "Apple Inc."


PAYLOAD = {
    "fetched_at": "2026-01-01T00:00:00Z",
    "sources": {}, "counts": {"sp500": 2, "nasdaq100": 1},
    "members": [
        {"symbol": "AAPL", "name": "Apple", "sector": "Technology", "indices": ["sp500", "nasdaq100"]},
        {"symbol": "JPM", "name": "JPMorgan", "sector": "Financials", "indices": ["sp500"]},
    ],
}


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "SETTINGS", replace(universe.SETTINGS, data_dir=tmp_path))
    universe._cached = None

    with pytest.raises(FileNotFoundError):
        load_universe()

    path = save_universe(PAYLOAD)
    assert path == tmp_path / "universe.json"
    assert load_universe() == PAYLOAD

    roster = members(indices=("nasdaq100",))
    assert [m.symbol for m in roster] == ["AAPL"]
    assert isinstance(roster[0], Member) and roster[0].sector == "Technology"

    both = members(indices=("sp500", "nasdaq100"))
    assert {m.symbol for m in both} == {"AAPL", "JPM"}
