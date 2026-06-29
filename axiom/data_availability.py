"""Centralized, prompt-facing source of truth for *what data is available*.

This module is the single place agents learn what they can build on: which
symbols, intervals, enrichment columns, and date ranges actually exist. It reuses
the existing caches rather than adding new network calls:

  * **Enrichment** ranges come from :mod:`axiom.auto_trim`'s cached
    ``/metrics/ranges`` snapshot (refreshed at most daily).
  * **OHLCV** (candle) ranges come from :func:`axiom.api_domains.data.get_coverage`,
    which reads parquet footer statistics (cached per file).

Two render modes back the "unfiltered when reasoning / filtered when focused"
split:

  * :func:`render_full_availability` — the whole menu (all symbols × intervals),
    for ideation / discovery / planning contexts.
  * :func:`render_scoped_availability` — the exact columns + ranges for specific
    target assets/timeframes, for hypothesis-focused strategy authoring.

Both degrade to an empty string when the underlying caches are unavailable, so
prompts assemble fine without them.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from axiom import auto_trim

log = logging.getLogger("data_availability")

_SNAPSHOT_TTL_S = 300.0
_snapshot_cache: Optional[dict] = None
_snapshot_cache_ts: float = 0.0


# ---------------------------------------------------------------------------
# Symbol normalization (pair <-> slug <-> coverage dir name)
# ---------------------------------------------------------------------------

def _slug_to_pair_map() -> dict[str, str]:
    """Asset slug -> canonical display pair, derived from the enricher map."""
    try:
        from axiom.lan_enricher import _SYMBOL_MAP
    except Exception:
        _SYMBOL_MAP = {}
    mapping: dict[str, str] = {}
    for key, slug in _SYMBOL_MAP.items():
        if "/" in key:  # prefer the "BTC/USDT" form as the display pair
            mapping.setdefault(slug, key)
    # Fallback display names for any slug the enricher map didn't cover.
    for slug in auto_trim.ASSET_MAP.values():
        mapping.setdefault(slug, slug.replace("-", " ").title())
    return mapping


def _to_pair(symbol: Optional[str]) -> Optional[str]:
    """Map any symbol form (BTC, BTC/USDT, BTCUSDT, BTC-USDT, slug) to a pair."""
    slug = auto_trim.resolve_asset(symbol)
    if not slug:
        return None
    return _slug_to_pair_map().get(slug, slug)


# ---------------------------------------------------------------------------
# Snapshot assembly (merges enrichment + OHLCV, TTL-cached)
# ---------------------------------------------------------------------------

def _new_interval_node() -> dict:
    return {"ohlcv": None, "enrichment": {}}


def _build_snapshot() -> dict:
    """Merge enrichment ranges and OHLCV coverage into one nested view.

    Returns ``{pair: {interval: {"ohlcv": {from,to,rows}|None,
    "enrichment": {col: {from,to,points,interval}}}}}``.
    """
    snapshot: dict = {}

    # Enrichment columns (auto_trim cache).
    try:
        enrich_index = auto_trim.availability_index()
    except Exception as exc:
        log.debug("enrichment availability_index unavailable: %s", exc)
        enrich_index = {}
    for slug, by_interval in enrich_index.items():
        pair = _to_pair(slug)
        if not pair:
            continue
        for interval, metrics in by_interval.items():
            node = snapshot.setdefault(pair, {}).setdefault(interval, _new_interval_node())
            for metric, rng in metrics.items():
                node["enrichment"][metric] = {
                    "from": rng.get("from"),
                    "to": rng.get("to"),
                    "points": rng.get("points"),
                    "interval": interval,
                }

    # OHLCV candle ranges. Scan the parquet lake directly via the lightweight
    # ``axiom.data`` coverage helpers (footer-stat cached per file) — deliberately
    # NOT axiom.api_domains.data.get_coverage, which drags in the whole api_core
    # import chain and also scans funding/oi streams we don't need here.
    for sym, interval, entry in _iter_ohlcv_coverage():
        pair = _to_pair(sym)
        if not pair:
            continue
        node = snapshot.setdefault(pair, {}).setdefault(interval, _new_interval_node())
        node["ohlcv"] = {
            "from": entry.get("from"),
            "to": entry.get("to"),
            "rows": entry.get("rows"),
        }

    return snapshot


def _iter_ohlcv_coverage():
    """Yield ``(symbol_dir, interval, coverage_entry)`` for each OHLCV parquet.

    Mirrors the OHLCV portion of :func:`axiom.api_domains.data.get_coverage`
    without importing the heavy API layer. Best-effort: yields nothing on error.
    """
    try:
        from pathlib import Path

        from axiom.data import DATA_DIR, coverage_entry, prune_coverage_cache
    except Exception as exc:
        log.debug("OHLCV coverage helpers unavailable: %s", exc)
        return

    root = Path(DATA_DIR)
    if not root.exists():
        return

    visited: set[str] = set()
    try:
        for sym_dir in sorted(root.iterdir()):
            if not sym_dir.is_dir() or sym_dir.name.startswith("."):
                continue
            for pq_file in sorted(sym_dir.glob("*.parquet")):
                visited.add(str(pq_file))
                entry = coverage_entry(pq_file)
                if entry is not None:
                    yield sym_dir.name, pq_file.stem, entry
    finally:
        try:
            prune_coverage_cache(visited)
        except Exception:
            pass


def get_availability_snapshot(*, force: bool = False) -> dict:
    """TTL-cached merged availability snapshot. See :func:`_build_snapshot`."""
    global _snapshot_cache, _snapshot_cache_ts
    now = time.monotonic()
    if not force and _snapshot_cache is not None and (now - _snapshot_cache_ts) < _SNAPSHOT_TTL_S:
        return _snapshot_cache
    try:
        _snapshot_cache = _build_snapshot()
    except Exception as exc:  # pragma: no cover — defence in depth
        log.warning("Failed to build data-availability snapshot: %s", exc)
        _snapshot_cache = {}
    _snapshot_cache_ts = now
    return _snapshot_cache


def invalidate_cache() -> None:
    """Drop the in-process snapshot cache (mainly for tests)."""
    global _snapshot_cache, _snapshot_cache_ts
    _snapshot_cache = None
    _snapshot_cache_ts = 0.0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _sorted_intervals(intervals: Iterable[str]) -> list[str]:
    return sorted(intervals, key=auto_trim.tf_sort_key)


def _day(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if text else "?"


_GUARD_HINT = "always guard optional columns with `if 'col' in df.columns:`"


# ---------------------------------------------------------------------------
# Render: full (unfiltered) menu
# ---------------------------------------------------------------------------

def render_full_availability(snapshot: Optional[dict] = None) -> str:
    """Compact, unfiltered menu of all symbols × intervals. ``""`` when empty."""
    snap = snapshot if snapshot is not None else get_availability_snapshot()
    if not snap:
        return ""

    lines = [
        "## DATA AVAILABILITY",
        "Everything you can build on right now (live from the data caches); "
        f"enrichment columns are optional — {_GUARD_HINT}.",
        "",
    ]
    for pair in sorted(snap):
        by_interval = snap[pair]
        lines.append(f"{pair}:")
        for interval in _sorted_intervals(by_interval):
            node = by_interval[interval]
            parts: list[str] = []
            ohlcv = node.get("ohlcv")
            if ohlcv:
                parts.append(f"OHLCV {_day(ohlcv.get('from'))}→{_day(ohlcv.get('to'))}")
            n_enrich = len(node.get("enrichment") or {})
            if n_enrich:
                parts.append(f"{n_enrich} enrichment col{'s' if n_enrich != 1 else ''}")
            if not parts:
                continue
            lines.append(f"  {interval}: " + " + ".join(parts))
        lines.append("")

    lines.append(
        "Enrichment families typically include funding, open interest, long/short "
        "ratio, taker flow, liquidations, L2 order book, and on-chain/social "
        "metrics. Consume them via create_custom_strategy; request the exact "
        "column names/ranges for a chosen symbol+timeframe when you focus a "
        "hypothesis."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Render: scoped (filtered to target assets/timeframes)
# ---------------------------------------------------------------------------

def render_scoped_availability(
    assets: Optional[Iterable[str]],
    timeframes: Optional[Iterable[str]] = None,
    snapshot: Optional[dict] = None,
) -> str:
    """Exact columns + ranges for the given assets/timeframes. ``""`` when empty.

    ``timeframes`` empty/None → show every interval that has data for the asset.
    """
    snap = snapshot if snapshot is not None else get_availability_snapshot()
    if not snap:
        return ""

    # Resolve requested assets to canonical pairs (dedup, preserve order).
    pairs: list[str] = []
    for asset in (assets or []):
        pair = _to_pair(asset)
        if pair and pair in snap and pair not in pairs:
            pairs.append(pair)
    if not pairs:
        return ""

    wanted_tfs = [str(tf).strip().lower() for tf in (timeframes or []) if str(tf).strip()]

    blocks: list[str] = [
        "## DATA AVAILABILITY (your target assets/timeframes)",
        f"Exact columns and ranges in the dataframes you'll receive — {_GUARD_HINT}.",
        "",
    ]
    rendered_any = False
    for pair in pairs:
        by_interval = snap[pair]
        intervals = wanted_tfs or _sorted_intervals(by_interval)
        for interval in _sorted_intervals(intervals):
            node = by_interval.get(interval)
            if not node:
                if wanted_tfs:
                    blocks.append(f"{pair} @ {interval}: no data collected at this timeframe.")
                    blocks.append("")
                    rendered_any = True
                continue
            rendered_any = True
            blocks.append(f"{pair} @ {interval}:")
            ohlcv = node.get("ohlcv")
            if ohlcv:
                rows = ohlcv.get("rows")
                rows_txt = f", {rows} rows" if rows else ""
                blocks.append(
                    f"  OHLCV: {_day(ohlcv.get('from'))} → {_day(ohlcv.get('to'))}{rows_txt}"
                )
            else:
                blocks.append("  OHLCV: always present (range not cached)")

            enrichment = node.get("enrichment") or {}
            if enrichment:
                blocks.append("  Enrichment columns:")
                for col in sorted(enrichment):
                    rng = enrichment[col]
                    blocks.append(f"    - {col} (from {_day(rng.get('from'))})")
                # Interval-resolution warnings (metric coarser than the bar).
                per_column = {c: {"interval": r.get("interval")} for c, r in enrichment.items()}
                for mm in auto_trim.compute_interval_mismatches(per_column, interval):
                    blocks.append(f"  ⚠ {mm['note']}")
            else:
                blocks.append("  Enrichment columns: none at this timeframe (OHLCV-only).")
            blocks.append("")

    if not rendered_any:
        return ""
    return "\n".join(blocks).rstrip()
