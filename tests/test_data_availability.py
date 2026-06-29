"""Tests for the centralized data-availability renderers."""

from __future__ import annotations

import pytest

from axiom import data_availability as da


def _snapshot() -> dict:
    return {
        "BTC/USDT": {
            "1h": {
                "ohlcv": {"from": "2023-01-30", "to": "2026-06-28", "rows": 396000},
                "enrichment": {
                    "open_interest": {"from": "2023-03-26", "to": "2026-06-28", "points": 1, "interval": "1h"},
                    "funding_rate": {"from": "2023-03-02", "to": "2026-06-28", "points": 1, "interval": "8h"},
                },
            },
            "1m": {
                "ohlcv": {"from": "2025-12-06", "to": "2026-06-28", "rows": 5600000},
                "enrichment": {
                    "l2_imbalance_bid": {"from": "2025-12-06", "to": "2026-06-28", "points": 1, "interval": "1m"},
                },
            },
        },
        "ETH/USDT": {
            "1h": {
                "ohlcv": {"from": "2023-01-30", "to": "2026-06-28", "rows": 396000},
                "enrichment": {},
            },
        },
    }


def test_render_full_lists_all_pairs_intervals_and_counts():
    out = da.render_full_availability(snapshot=_snapshot())
    assert out.startswith("## DATA AVAILABILITY")
    assert "BTC/USDT:" in out and "ETH/USDT:" in out
    # Coarsest-last interval ordering: 1m before 1h.
    assert out.index("1m:") < out.index("1h:")
    assert "2 enrichment cols" in out  # BTC/USDT 1h
    assert "OHLCV 2023-01-30→2026-06-28" in out


def test_render_scoped_filters_and_lists_exact_columns_with_ranges():
    out = da.render_scoped_availability(["BTC/USDT"], ["1h"], snapshot=_snapshot())
    assert "## DATA AVAILABILITY (your target assets/timeframes)" in out
    assert "BTC/USDT @ 1h:" in out
    assert "- funding_rate (from 2023-03-02)" in out
    assert "- open_interest (from 2023-03-26)" in out
    # Filtered out: ETH and the BTC 1m interval.
    assert "ETH/USDT" not in out
    assert "@ 1m" not in out


def test_render_scoped_flags_interval_mismatch():
    out = da.render_scoped_availability(["BTC/USDT"], ["1h"], snapshot=_snapshot())
    # funding_rate is collected at 8h but the strategy bar is 1h → forward-filled.
    assert "⚠" in out
    assert "forward-filled" in out


def test_render_scoped_notes_missing_timeframe():
    out = da.render_scoped_availability(["BTC/USDT"], ["4h"], snapshot=_snapshot())
    assert "no data collected at this timeframe" in out


def test_render_scoped_resolves_symbol_aliases():
    # Bare base and exchange-style names must resolve to the canonical pair.
    for alias in ("BTC", "BTCUSDT", "BTC-USDT"):
        out = da.render_scoped_availability([alias], ["1h"], snapshot=_snapshot())
        assert "BTC/USDT @ 1h:" in out, alias


def test_empty_snapshot_renders_empty_string():
    assert da.render_full_availability(snapshot={}) == ""
    assert da.render_scoped_availability(["BTC/USDT"], ["1h"], snapshot={}) == ""


def test_unknown_asset_yields_empty_scoped():
    assert da.render_scoped_availability(["DOGE/USDT"], ["1h"], snapshot=_snapshot()) == ""


def test_build_snapshot_merges_enrichment_and_ohlcv(monkeypatch):
    monkeypatch.setattr(
        da.auto_trim,
        "availability_index",
        lambda: {
            "bitcoin": {
                "1h": {"funding_rate": {"from": "2023-03-02", "to": "2026-06-28", "points": 1}},
            }
        },
    )
    monkeypatch.setattr(
        da,
        "_iter_ohlcv_coverage",
        lambda: iter([("BTC-USDT", "1h", {"from": "2023-01-30", "to": "2026-06-28", "rows": 396000})]),
    )

    da.invalidate_cache()
    snap = da.get_availability_snapshot(force=True)
    assert "BTC/USDT" in snap
    node = snap["BTC/USDT"]["1h"]
    assert node["ohlcv"]["rows"] == 396000
    assert "funding_rate" in node["enrichment"]
    assert node["enrichment"]["funding_rate"]["interval"] == "1h"
    da.invalidate_cache()
