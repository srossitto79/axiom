"""Enrichment-aware data-availability for backtests.

Two responsibilities:

1. Keep a local cache of the LAN metrics API ``/metrics/ranges`` response
   (``enrichment_ranges.json``) fresh — refetched when missing or older than a
   day — so every availability check is a local lookup with no per-backtest HTTP.

2. Given a strategy (its source code / params / type), the asset and the
   timeframe, work out the window over which the enrichment columns it actually
   references have data. This drives two things:

     * a registration-time *report* ("considering columns X, Y you can backtest
       this from <date> to <date>"), surfaced back to the authoring agent;
     * backtest-time *smart start/end selection* when the caller gives no
       explicit window, so a run never wastes the front of the window on
       NaN-poisoned bars (e.g. liquidation columns only exist from Dec 2025) nor
       the tail on a metric that stopped being collected.

Usage:
    from axiom.auto_trim import maybe_trim_start_date, compute_data_availability

    start = maybe_trim_start_date(stype, params, symbol, tf, explicit_start, code)
    report = compute_data_availability(asset=symbol, timeframe=tf, strategy_code=code)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("auto_trim")

_RANGES_FILENAME = "enrichment_ranges.json"
_DEFAULT_MAX_AGE_HOURS = 24
_DEFAULT_WARMUP_BARS = 210
# Cap the forward warmup shift so a long-warmup daily strategy doesn't push its
# start months past where the data begins (210 * 1d would be 7 months).
_MAX_WARMUP_SHIFT_DAYS = 45
_LAN_URL_DEFAULT = "http://192.168.0.210:8001"
# (connect, read) — fail fast on connect so a down LAN API never stalls a
# backtest/registration that only triggered a best-effort daily refresh.
_RANGES_FETCH_TIMEOUT = (5, 30)

ASSET_MAP = {
    "btc": "bitcoin", "bitcoin": "bitcoin", "xbt": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "bnb": "binance-coin", "binance-coin": "binance-coin", "binancecoin": "binance-coin",
}

# OHLCV / base candle columns are always present, so they never constrain the
# window. They are excluded from the enrichment metric universe even when the
# ranges file lists them.
BASE_COLUMNS = frozenset({
    "open", "high", "low", "close", "volume", "price",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume",
})

# The enrichment-column universe is derived entirely from the ranges file so new
# metrics are tracked automatically. There is no hardcoded fallback list —
# if the file is unavailable, no columns are detected and the window is
# unconstrained (safe degraded behaviour; no risk of a stale list missing new
# metrics or retaining deleted ones).

_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}

# ---------------------------------------------------------------------------
# Cache state (invalidated by file mtime so a refreshed file is picked up)
# ---------------------------------------------------------------------------
_LOOKUP: Optional[dict] = None          # (asset, interval, metric) -> {from,to,points}
_BY_ASSET_METRIC: Optional[dict] = None  # (asset, metric) -> [entries]  (cross-interval)
_METRIC_UNIVERSE: Optional[frozenset] = None
_KNOWN_ASSETS: Optional[frozenset] = None
_CACHE_MTIME: Optional[float] = None

# Throttle failed-refresh retries so a down LAN API doesn't add a connect-timeout
# stall to every availability check during e.g. a backtest sweep.
_LAST_REFRESH_ATTEMPT: float = 0.0
_REFRESH_RETRY_COOLDOWN_S = 1800.0


# ---------------------------------------------------------------------------
# Ranges file: path, refresh, load
# ---------------------------------------------------------------------------

def _ranges_path() -> Path:
    """Canonical on-disk path for the cached ranges file (next to this module)."""
    return Path(__file__).resolve().parent / _RANGES_FILENAME


def _lan_base_url() -> str:
    return os.environ.get("LAN_METRICS_URL", _LAN_URL_DEFAULT).rstrip("/")


def _fetch_ranges_from_lan() -> Optional[dict]:
    """GET /metrics/ranges from the LAN metrics API. None on any failure."""
    url = f"{_lan_base_url()}/metrics/ranges"
    try:
        import requests

        r = requests.get(url, timeout=_RANGES_FETCH_TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Failed to fetch enrichment ranges from %s: %s", url, exc)
        return None
    if not isinstance(data, dict) or "ranges" not in data:
        log.warning("Unexpected /metrics/ranges payload from %s (no 'ranges' key)", url)
        return None
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def refresh_ranges_if_stale(max_age_hours: float = _DEFAULT_MAX_AGE_HOURS, *, force: bool = False) -> bool:
    """Refresh the cached ranges file from the LAN API when missing or stale.

    Returns True when the file was (re)written, False otherwise. A failed fetch
    leaves any existing (stale) file in place — degraded but functional.
    """
    global _LAST_REFRESH_ATTEMPT

    path = _ranges_path()
    file_exists = path.exists()
    try:
        if not force and file_exists:
            age_hours = (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600.0
            if age_hours < max_age_hours:
                return False
    except OSError:
        pass  # treat as needing refresh

    # Don't re-attempt a failing fetch on every call — but never skip when there
    # is no file at all (we have nothing to fall back to).
    import time as _time

    if not force and file_exists and (_time.monotonic() - _LAST_REFRESH_ATTEMPT) < _REFRESH_RETRY_COOLDOWN_S:
        return False
    _LAST_REFRESH_ATTEMPT = _time.monotonic()

    data = _fetch_ranges_from_lan()
    if data is None:
        if path.exists():
            log.info("Keeping existing enrichment ranges cache (refresh fetch failed)")
        return False

    try:
        _atomic_write_json(path, data)
    except Exception as exc:
        log.warning("Failed to write enrichment ranges cache %s: %s", path, exc)
        return False

    _invalidate_cache()
    log.info("Refreshed enrichment ranges cache: %s (%d entries)", path, len(data.get("ranges", [])))
    return True


def _invalidate_cache() -> None:
    global _LOOKUP, _BY_ASSET_METRIC, _METRIC_UNIVERSE, _KNOWN_ASSETS, _CACHE_MTIME
    _LOOKUP = _BY_ASSET_METRIC = _METRIC_UNIVERSE = _KNOWN_ASSETS = _CACHE_MTIME = None


def _build_indices(data: dict) -> tuple[dict, dict, frozenset, frozenset]:
    lookup: dict = {}
    by_asset_metric: dict = {}
    universe: set = set()
    assets: set = set()
    for entry in data.get("ranges", []):
        try:
            asset = entry["asset"].strip().lower()
            interval = entry["collection_interval"].strip()
            metric = entry["metric_name"].strip().lower()
            rng = {
                "from": entry["range"]["from"],
                "to": entry["range"]["to"],
                "points": int(entry.get("point_count") or 0),
                "interval": interval,
            }
        except (KeyError, TypeError, AttributeError):
            continue
        lookup[(asset, interval, metric)] = rng
        by_asset_metric.setdefault((asset, metric), []).append(rng)
        assets.add(asset)
        if metric not in BASE_COLUMNS:
            universe.add(metric)
    return lookup, by_asset_metric, frozenset(universe), frozenset(assets)


def _ensure_loaded() -> None:
    """Load (and refresh-if-stale) the ranges file into the module cache."""
    global _LOOKUP, _BY_ASSET_METRIC, _METRIC_UNIVERSE, _KNOWN_ASSETS, _CACHE_MTIME

    # Best-effort refresh; never let a refresh failure block a lookup.
    try:
        refresh_ranges_if_stale()
    except Exception as exc:
        log.debug("Ranges refresh check skipped: %s", exc)

    path = _ranges_path()
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None

    if _LOOKUP is not None and mtime == _CACHE_MTIME:
        return  # cache current

    if mtime is None:
        log.warning("No enrichment ranges cache found - column detection disabled (no window constraint applied)")
        _LOOKUP, _BY_ASSET_METRIC = {}, {}
        _METRIC_UNIVERSE, _KNOWN_ASSETS = frozenset(), frozenset()
        _CACHE_MTIME = None
        return

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        _LOOKUP, _BY_ASSET_METRIC, _METRIC_UNIVERSE, _KNOWN_ASSETS = _build_indices(raw)
        _CACHE_MTIME = mtime
        log.info("Loaded enrichment ranges from %s (%d entries, %d metrics)",
                 path, len(_LOOKUP), len(_METRIC_UNIVERSE))
    except Exception as exc:
        log.warning("Failed to read enrichment ranges %s: %s", path, exc)
        _LOOKUP, _BY_ASSET_METRIC = {}, {}
        _METRIC_UNIVERSE, _KNOWN_ASSETS = frozenset(), frozenset()
        _CACHE_MTIME = mtime


def _metric_universe() -> frozenset:
    _ensure_loaded()
    return _METRIC_UNIVERSE or frozenset()


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_asset(symbol: Optional[str]) -> Optional[str]:
    """Map an Axiom symbol (BTC, BTC/USDT, BTCUSDT) to a ranges asset slug."""
    raw = str(symbol or "").strip().lower()
    if not raw:
        return None
    _ensure_loaded()
    # Already a slug?
    if raw in (_KNOWN_ASSETS or frozenset()) or raw in ASSET_MAP.values():
        return raw
    # Strip pair separators then known quote suffixes.
    base = re.split(r"[/\-_]", raw)[0]
    for suffix in ("usdt", "usdc", "usd", "perp"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    return ASSET_MAP.get(base)


def _tf_seconds(timeframe: Optional[str]) -> int:
    return _TF_SECONDS.get(str(timeframe or "").strip().lower(), 3600)


def _parse_ts(value: object) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_referenced_columns(
    strategy_type: Optional[str] = None,
    params: Optional[dict] = None,
    strategy_code: Optional[str] = None,
) -> set:
    """Return the enrichment metric names a strategy references.

    Tokenises the strategy source, the params blob and the type name, then
    intersects with the metric universe derived from the ranges file. OHLCV/base
    columns are never in the universe, so an OHLCV-only strategy yields an empty
    set (→ no window constraint).
    """
    universe = _metric_universe()
    if not universe:
        return set()

    text_parts = [str(strategy_code or ""), str(strategy_type or "")]
    if params:
        try:
            text_parts.append(json.dumps(params, default=str))
        except (TypeError, ValueError):
            text_parts.append(str(params))
    blob = "\n".join(text_parts).lower()

    tokens = set(re.findall(r"[a-z_][a-z0-9_]*", blob))
    return tokens & set(universe)


def _best_entry(asset: str, metric: str, timeframe: str) -> Optional[dict]:
    """Best range entry for a metric.

    Exact-interval match wins (it reflects exactly what the metrics API serves at
    that interval). Otherwise fall back to the series with the *earliest* start —
    the metric is collected from that date and the enricher resamples/back-fills
    it onto the requested timeframe, so the widest history is the true
    availability (e.g. funding_rate's 8h series reaches back years, while its 1m
    series is only weeks deep).
    """
    _ensure_loaded()
    tf = str(timeframe or "").strip().lower()
    exact = (_LOOKUP or {}).get((asset, tf, metric))
    if exact and exact["points"] > 0:
        return exact
    candidates = [e for e in (_BY_ASSET_METRIC or {}).get((asset, metric), []) if e["points"] > 0]
    if not candidates:
        return exact  # may be a zero-point exact entry or None

    def _sort_key(e: dict):
        ts = _parse_ts(e.get("from"))
        # Earliest start first; unparseable dates sink to the bottom.
        return (ts is None, ts or dt.datetime.max.replace(tzinfo=dt.timezone.utc))

    return min(candidates, key=_sort_key)


# ---------------------------------------------------------------------------
# Core: data-availability computation
# ---------------------------------------------------------------------------

def _compute_interval_mismatches(
    per_column: dict,
    strategy_tf: str,
) -> list[dict]:
    """Return a list of metrics whose native collection interval is *coarser* than
    the strategy's timeframe — meaning the metric updates less often than the
    strategy's bars, so most bars will hold a forward-filled stale value.

    Fine-or-equal resolution (metric interval <= strategy tf) is silently OK:
    the enricher aggregates/resamples those correctly at no signal cost.

    Each entry: {column, metric_interval, strategy_timeframe, ratio, note}
    sorted worst (highest ratio) first.
    """
    tf_s = _tf_seconds(strategy_tf)
    mismatches: list[dict] = []
    for col, entry in per_column.items():
        metric_interval = entry.get("interval")
        if not metric_interval:
            continue
        metric_s = _tf_seconds(metric_interval)
        if metric_s <= tf_s:
            continue  # same or finer resolution - no concern
        ratio = metric_s // max(tf_s, 1)
        mismatches.append({
            "column": col,
            "metric_interval": metric_interval,
            "strategy_timeframe": strategy_tf,
            "ratio": ratio,
            "note": (
                f"{col} is collected every {metric_interval} but strategy runs at {strategy_tf} "
                f"({ratio}x coarser) - value is forward-filled across {ratio} bars between updates"
            ),
        })
    mismatches.sort(key=lambda x: -x["ratio"])
    return mismatches


def compute_data_availability(
    *,
    asset: Optional[str],
    timeframe: Optional[str],
    columns: Optional[set] = None,
    strategy_type: Optional[str] = None,
    params: Optional[dict] = None,
    strategy_code: Optional[str] = None,
    warmup_bars: int = _DEFAULT_WARMUP_BARS,
) -> dict:
    """Work out the backtestable window for a strategy's enrichment columns.

    Returns a JSON-serialisable dict:
        asset, timeframe, columns
        resolvable      asset slug + ranges cache were available
        constrained     at least one enrichment column was referenced
        usable          a non-empty [start, end] window exists
        start, end      recommended ISO window (None when unconstrained)
        data_from       latest column start (raw, before warmup shift)
        data_to         earliest column end
        limiting_start_columns / limiting_end_columns
        per_column      {col: {from, to, points, interval}}
        summary         human-readable report line(s)
    """
    tf = str(timeframe or "1h").strip().lower() or "1h"
    if columns is None:
        columns = _extract_referenced_columns(strategy_type, params, strategy_code)
    columns = set(columns)

    result: dict = {
        "asset": None,
        "timeframe": tf,
        "columns": sorted(columns),
        "resolvable": False,
        "constrained": bool(columns),
        "usable": True,
        "start": None,
        "end": None,
        "data_from": None,
        "data_to": None,
        "limiting_start_columns": [],
        "limiting_end_columns": [],
        "per_column": {},
        "interval_mismatches": [],
        "has_interval_mismatches": False,
        "summary": "",
    }

    if not columns:
        result["summary"] = "No enrichment-data constraints (OHLCV-only) - full history is backtestable."
        return result

    slug = _resolve_asset(asset)
    result["asset"] = slug
    if not slug:
        result["summary"] = (
            f"Unknown asset '{asset}' — cannot verify enrichment availability for "
            f"columns: {', '.join(sorted(columns))}."
        )
        return result

    _ensure_loaded()
    if not _LOOKUP:
        result["summary"] = (
            "Enrichment ranges cache unavailable — cannot verify availability for "
            f"columns: {', '.join(sorted(columns))}."
        )
        return result

    result["resolvable"] = True

    latest_from: Optional[dt.datetime] = None
    earliest_to: Optional[dt.datetime] = None
    start_limiters: list[str] = []
    end_limiters: list[str] = []
    missing: list[str] = []

    for col in sorted(columns):
        entry = _best_entry(slug, col, tf)
        if not entry or entry["points"] == 0:
            missing.append(col)
            continue
        col_from = _parse_ts(entry["from"])
        col_to = _parse_ts(entry["to"])
        result["per_column"][col] = {
            "from": entry["from"],
            "to": entry["to"],
            "points": entry["points"],
            "interval": entry.get("interval"),
        }
        if col_from is not None:
            if latest_from is None or col_from > latest_from:
                latest_from, start_limiters = col_from, [col]
            elif col_from == latest_from:
                start_limiters.append(col)
        if col_to is not None:
            if earliest_to is None or col_to < earliest_to:
                earliest_to, end_limiters = col_to, [col]
            elif col_to == earliest_to:
                end_limiters.append(col)

    result["limiting_start_columns"] = start_limiters
    result["limiting_end_columns"] = end_limiters
    if missing:
        result["missing_columns"] = missing

    # Interval-resolution mismatches: metrics whose native collection interval is
    # coarser than the strategy's timeframe will be forward-filled for most bars.
    # This is informational — not a blocker — but the agent needs to know.
    interval_mismatches = _compute_interval_mismatches(result["per_column"], tf)
    result["interval_mismatches"] = interval_mismatches
    result["has_interval_mismatches"] = bool(interval_mismatches)

    if latest_from is None:
        result["usable"] = False
        result["summary"] = (
            f"None of the referenced columns ({', '.join(sorted(columns))}) have data "
            f"for {slug} — backtest cannot use them."
        )
        return result

    result["data_from"] = _iso(latest_from)
    if earliest_to is not None:
        result["data_to"] = _iso(earliest_to)

    # Push the start forward by a warmup so indicators computed on the enrichment
    # columns are warm by the first evaluated bar (capped so long-warmup daily
    # strategies don't shift months past the data start).
    shift_s = min(max(int(warmup_bars), 0) * _tf_seconds(tf), _MAX_WARMUP_SHIFT_DAYS * 86400)
    start_ts = latest_from + dt.timedelta(seconds=shift_s)
    result["start"] = _iso(start_ts)
    if earliest_to is not None:
        result["end"] = _iso(earliest_to)

    # Non-overlapping availability (e.g. a metric that stopped before another began).
    if earliest_to is not None and start_ts >= earliest_to:
        result["usable"] = False
        result["summary"] = (
            f"WARNING: cannot backtest {slug}/{tf}: referenced columns have non-overlapping "
            f"availability. Earliest usable start {result['start']} is at/after the "
            f"earliest column end {result['data_to']} "
            f"(start limited by {', '.join(start_limiters)}; "
            f"end limited by {', '.join(end_limiters)})."
        )
        return result

    span_days = None
    if earliest_to is not None:
        span_days = int((earliest_to - start_ts).total_seconds() // 86400)

    parts = [
        f"Backtestable on {slug}/{tf} from {result['start']}"
        + (f" to {result['end']}" if result["end"] else " onward")
        + (f" (~{span_days} days)" if span_days is not None else "")
        + "."
    ]
    parts.append(
        "Start limited by "
        + ", ".join(f"{c} (data from {result['per_column'][c]['from'][:10]})" for c in start_limiters)
        + "."
    )
    if end_limiters and result["data_to"]:
        # Only call out the end when something actually stopped before "now-ish".
        parts.append(
            "End limited by "
            + ", ".join(f"{c} (data to {result['per_column'][c]['to'][:10]})" for c in end_limiters)
            + "."
        )
    if missing:
        parts.append(
            "No range data for: " + ", ".join(missing) + " (treated as unconstrained)."
        )
    result["summary"] = " ".join(parts)
    # interval_mismatches is informational context — kept in the structured dict
    # only so callers can surface it at their own discretion. It is intentionally
    # NOT included in the summary so it doesn't read as an error to the agent.
    return result


# ---------------------------------------------------------------------------
# Backtest entry points (backwards-compatible)
# ---------------------------------------------------------------------------

def auto_trim_start_date(
    strategy_type: Optional[str] = None,
    params: Optional[dict] = None,
    strategy_code: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    default_start: str = "2024-01-01T00:00:00Z",
    warmup_bars: int = _DEFAULT_WARMUP_BARS,
) -> Optional[str]:
    """Trimmed start date aligned to enrichment availability, or None if no trim
    is warranted (OHLCV-only, no data, or the default start is already safe)."""
    if not symbol or not timeframe:
        return None
    avail = compute_data_availability(
        asset=symbol, timeframe=timeframe,
        strategy_type=strategy_type, params=params, strategy_code=strategy_code,
        warmup_bars=warmup_bars,
    )
    start = avail.get("start")
    if not start:
        return None
    default_ts = _parse_ts(default_start)
    start_ts = _parse_ts(start)
    if default_ts is not None and start_ts is not None and start_ts <= default_ts:
        return None  # default already covers the data
    log.info(
        "Auto-trim: start=%s for %s/%s (cols: %s)",
        start, symbol, timeframe, avail.get("columns"),
    )
    return start


def maybe_trim_start_date(
    strategy_type: str,
    params: dict,
    symbol: str,
    timeframe: str,
    explicit_start: Optional[str] = None,
    strategy_code: Optional[str] = None,
) -> Optional[str]:
    """Honour an explicit start; otherwise auto-trim to enrichment availability."""
    if explicit_start:
        return explicit_start
    return auto_trim_start_date(
        strategy_type=strategy_type,
        params=params,
        strategy_code=strategy_code,
        symbol=symbol,
        timeframe=timeframe,
    )


def maybe_select_window(
    strategy_type: str,
    params: dict,
    symbol: str,
    timeframe: str,
    explicit_start: Optional[str] = None,
    explicit_end: Optional[str] = None,
    strategy_code: Optional[str] = None,
    warmup_bars: int = _DEFAULT_WARMUP_BARS,
) -> tuple[Optional[str], Optional[str], dict]:
    """Resolve a (start, end) window from enrichment availability.

    Explicit caller dates always win. Returns ``(start, end, availability)`` so
    the backtester can both apply the window and surface the report. When the
    availability is unusable, the recommended dates are NOT applied (start/end
    fall back to the explicit values / None) and the caller can read
    ``availability['usable']`` to warn.
    """
    avail = compute_data_availability(
        asset=symbol, timeframe=timeframe,
        strategy_type=strategy_type, params=params, strategy_code=strategy_code,
        warmup_bars=warmup_bars,
    )
    start = explicit_start
    end = explicit_end
    if avail.get("usable"):
        if not start and avail.get("start"):
            default_ts = _parse_ts("2024-01-01T00:00:00Z")
            start_ts = _parse_ts(avail["start"])
            if default_ts is None or start_ts is None or start_ts > default_ts:
                start = avail["start"]
        if not end and avail.get("end"):
            # Only cap the end when data genuinely stopped collecting — i.e., the
            # last known point is meaningfully in the past. Active columns have
            # avail["end"] ≈ now (hours old), which is not a real constraint and
            # would cause load_backtest_candles to return the full parquet history
            # instead of the last N bars.
            end_ts = _parse_ts(avail["end"])
            now = dt.datetime.now(dt.timezone.utc)
            if end_ts is not None and end_ts < now - dt.timedelta(days=3):
                end = avail["end"]
    return start, end, avail
