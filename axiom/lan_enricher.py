"""LAN Metrics Enricher — calls the local metrics API at enrich() time.

Two enrichment paths:
- Backtest (live=False): /features/matrix with day-granularity parquet cache.
  Historical days cache forever; today's cache expires after 5 minutes.
- Live scanner (live=True): /metrics/latest with staleness filter.
  Skips metrics older than 5× their native collection interval.

Configure the API base URL via the LAN_METRICS_URL environment variable
(default: http://192.168.0.210:8001).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_BASE_URL_DEFAULT = "http://192.168.0.210:8001"
_TODAY_TTL_SECONDS = 300  # 5 min TTL for today's cache
_MAX_STALENESS_MULT = 5   # live path: skip metric if age > 5× collection_interval
_DEFAULT_COLLECTION_INTERVAL_S = 3600  # fallback when interval can't be parsed


def _interval_to_seconds(value: object) -> int:
    """Parse a collection-interval value into seconds.

    The metrics API reports collection_interval as a string like "5m", "1h",
    "1d" (not seconds). Accepts ints/floats (already seconds) for resilience.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 0 else _DEFAULT_COLLECTION_INTERVAL_S
    text = str(value or "").strip().lower()
    if not text:
        return _DEFAULT_COLLECTION_INTERVAL_S
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = text[-1]
    if unit in units:
        try:
            qty = float(text[:-1] or "1")
        except ValueError:
            return _DEFAULT_COLLECTION_INTERVAL_S
        return max(int(qty * units[unit]), 1)
    try:
        return max(int(float(text)), 1)
    except ValueError:
        return _DEFAULT_COLLECTION_INTERVAL_S

# Columns the LAN API returns that duplicate what Axiom already has.
# These are dropped from the matrix response before merging.
_SKIP_COLS: frozenset[str] = frozenset({
    "open", "high", "low", "close", "volume", "price",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume",
    "mark_price", "is_incomplete", "asset",
})

# Axiom perp-symbol → LAN API asset slug.
# Both "BTCUSDT" and "BTC/USDT" forms are mapped.
_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT": "bitcoin",
    "BTC/USDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "ETH/USDT": "ethereum",
    "SOLUSDT": "solana",
    "SOL/USDT": "solana",
    "BNBUSDT": "binance-coin",
    "BNB/USDT": "binance-coin",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return os.environ.get("LAN_METRICS_URL", _BASE_URL_DEFAULT).rstrip("/")


def _data_root() -> Path:
    from axiom.data import data_root as _dr
    return _dr()


def _cache_path(asset: str, interval: str, date_str: str) -> Path:
    """Parquet cache: <data_root>/lan_cache/{asset}/{interval}/{YYYY-MM-DD}.parquet"""
    return _data_root() / "lan_cache" / asset / interval / f"{date_str}.parquet"


def _cache_valid(path: Path, date_str: str) -> bool:
    if not path.exists():
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if date_str == today:
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        return age < _TODAY_TTL_SECONDS
    return True  # historical days never expire


def _ensure_utc_ts(df: pd.DataFrame) -> pd.DataFrame:
    """Return copy with 'timestamp' as UTC-aware datetime64."""
    df = df.copy()
    if "timestamp" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            # Handles any DatetimeIndex name (e.g. "t" after merge_asof clobbers "timestamp").
            orig_name = df.index.name
            df = df.reset_index()
            if orig_name != "timestamp":
                df = df.rename(columns={orig_name: "timestamp"})
        else:
            return df
    ts = df["timestamp"]
    if pd.api.types.is_integer_dtype(ts):
        df["timestamp"] = pd.to_datetime(ts, unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(ts, utc=True)
    return df


def _get_session():
    """Lazily import requests and return a session."""
    import requests
    s = requests.Session()
    s.headers["Accept"] = "application/json"
    return s


# ---------------------------------------------------------------------------
# Backtest path helpers
# ---------------------------------------------------------------------------

def _parse_matrix_response(data: object) -> Optional[pd.DataFrame]:
    """Parse a /features/matrix JSON response into a normalised DataFrame."""
    if not data:
        return None
    try:
        rows = data.get("data", data) if isinstance(data, dict) else data
        if not rows:
            return None
        df = pd.DataFrame(rows)
        if "date" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"date": "timestamp"})
        if df.empty or "timestamp" not in df.columns:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    except Exception as exc:
        log.debug("LAN matrix parse error: %s", exc)
        return None


def _fetch_matrix_range(
    asset: str, interval: str, start_date: date, end_date: date
) -> Optional[pd.DataFrame]:
    """Fetch /features/matrix for a date range in a single HTTP call."""
    url = f"{_base_url()}/features/matrix"
    params = {
        "asset": asset,
        "interval": interval,
        "start": f"{start_date.strftime('%Y-%m-%d')}T00:00:00",
        "end": f"{end_date.strftime('%Y-%m-%d')}T23:59:59",
        "shift": 1,  # API built-in look-ahead prevention
        "clean": "false",
        "include_incomplete": "false",
    }
    try:
        r = _get_session().get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.debug("LAN /features/matrix %s %s %s→%s: %s", asset, interval, start_date, end_date, exc)
        return None
    return _parse_matrix_response(data)


def _cache_range(asset: str, interval: str, df: pd.DataFrame) -> None:
    """Split a multi-day matrix DataFrame and write one parquet per calendar day."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return
    df = df.copy()
    df["_date"] = df["timestamp"].dt.date
    for day, group in df.groupby("_date"):
        date_str = day.strftime("%Y-%m-%d")
        path = _cache_path(asset, interval, date_str)
        if _cache_valid(path, date_str):
            continue  # already cached (e.g. today hit by another call)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            group.drop(columns=["_date"]).to_parquet(path, index=False)
        except Exception as exc:
            log.debug("LAN cache write failed %s: %s", path, exc)


def _get_matrix_day(asset: str, interval: str, date_str: str) -> Optional[pd.DataFrame]:
    """Return cached-or-fresh matrix for one calendar day (single-day fallback)."""
    path = _cache_path(asset, interval, date_str)

    if _cache_valid(path, date_str):
        try:
            return pd.read_parquet(path)
        except Exception:
            pass  # corrupt cache — re-fetch

    df = _fetch_matrix_range(
        asset, interval,
        datetime.strptime(date_str, "%Y-%m-%d").date(),
        datetime.strptime(date_str, "%Y-%m-%d").date(),
    )
    if df is not None and not df.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_parquet(path, index=False)
        except Exception as exc:
            log.debug("LAN cache write failed %s: %s", path, exc)

    return df


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------

def _merge_lan(df: pd.DataFrame, lan_df: pd.DataFrame) -> pd.DataFrame:
    """Left-merge LAN enrichment onto df via merge_asof on timestamp."""
    had_ts_index = isinstance(df.index, pd.DatetimeIndex)
    original_index_name = df.index.name if had_ts_index else None

    left = _ensure_utc_ts(df)
    lan_df = lan_df.copy()
    # Pin both sides to ns UTC so merge_asof doesn't raise "incompatible merge keys"
    # when the LAN API returns us-precision timestamps and the candle frame is ns.
    lan_df["timestamp"] = pd.to_datetime(lan_df["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    lan_df = lan_df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    # shift=1 in _fetch_matrix_range already converts close→open timestamps
    # at the API level, making the PREVIOUS period's data available at each
    # bar open — no manual timestamp shift needed.

    # Drop OHLCV and other protected columns from LAN BEFORE the coverage check.
    # _SKIP_COLS are columns the LAN API returns that Axiom already owns.
    # Filtering here prevents the coverage check from ever dropping them from
    # `left` — if we let the coverage check run first and LAN has more rows
    # (e.g. because the backtest frame was tail-trimmed), it would drop OHLCV
    # from `left` and then _SKIP_COLS would also remove them from `lan_df`,
    # leaving the merged frame with no OHLCV at all.
    lan_df = lan_df.drop(columns=[c for c in _SKIP_COLS if c in lan_df.columns], errors="ignore")

    # For duplicate columns, keep whichever source has more non-null values.
    # This ensures a sparse existing column (e.g. OI with 1% coverage) gets
    # replaced by a richer LAN series rather than silently discarding it.
    existing = set(left.columns) - {"timestamp"}
    drop_from_lan = []
    drop_from_left = []
    for col in lan_df.columns:
        if col not in existing:
            continue
        left_coverage = int(left[col].notna().sum())
        lan_coverage = int(lan_df[col].notna().sum())
        if left_coverage >= lan_coverage:
            drop_from_lan.append(col)
        else:
            drop_from_left.append(col)
            log.debug("LAN column %s preferred over existing (LAN %d vs existing %d non-null)", col, lan_coverage, left_coverage)
    if drop_from_lan:
        lan_df = lan_df.drop(columns=drop_from_lan)
    if drop_from_left:
        left = left.drop(columns=drop_from_left)

    if lan_df.columns.tolist() == ["timestamp"]:
        # Nothing left to join.
        return df

    # Pin left timestamp to ns UTC (matches the right side pinned above).
    left["timestamp"] = left["timestamp"].astype("datetime64[ns, UTC]")

    # Preserve original row order across sort.
    left["_row_idx"] = range(len(left))
    left_sorted = left.sort_values("timestamp")
    lan_sorted = lan_df.sort_values("timestamp")

    try:
        merged = pd.merge_asof(
            left_sorted,
            lan_sorted,
            on="timestamp",
            direction="backward",
            allow_exact_matches=True,
        )
    except Exception as exc:
        log.warning("LAN merge_asof failed: %s", exc)
        return df

    merged = merged.sort_values("_row_idx").drop(columns=["_row_idx"])

    # Forward-fill (then backward-fill) new LAN columns so bars before the first
    # LAN timestamp and any gaps in coverage don't stay NaN.
    _new_lan_cols = [c for c in merged.columns if c not in set(left_sorted.columns) - {"_row_idx"}]
    if _new_lan_cols:
        merged[_new_lan_cols] = merged[_new_lan_cols].ffill().bfill()

    merged = merged.reset_index(drop=True)

    if had_ts_index:
        merged = merged.set_index("timestamp")
        if original_index_name is not None:
            merged.index.name = original_index_name

    return merged


# ---------------------------------------------------------------------------
# Enrichment paths
# ---------------------------------------------------------------------------

def _enrich_historical(df: pd.DataFrame, asset: str, interval: str) -> pd.DataFrame:
    """Stitch day-cached /features/matrix rows onto df.

    Cached days are read directly from parquet (zero HTTP calls on warm cache).
    Uncached days are collected into contiguous ranges and fetched in a single
    HTTP call per gap, then split by day and written to cache before reading.
    """
    left = _ensure_utc_ts(df)
    ts = left["timestamp"] if "timestamp" in left.columns else pd.Series([], dtype="datetime64[ns, UTC]")
    if ts.empty:
        return df

    start_date = ts.min().date()
    end_date = ts.max().date()

    # Partition the date range into cached days and contiguous uncached gaps.
    all_dates: list[date] = []
    cur: date = start_date
    while cur <= end_date:
        all_dates.append(cur)
        cur += timedelta(days=1)

    uncached_gaps: list[tuple[date, date]] = []  # (gap_start, gap_end)
    gap_start: Optional[date] = None
    for d in all_dates:
        path = _cache_path(asset, interval, d.strftime("%Y-%m-%d"))
        if not _cache_valid(path, d.strftime("%Y-%m-%d")):
            if gap_start is None:
                gap_start = d
        else:
            if gap_start is not None:
                uncached_gaps.append((gap_start, d - timedelta(days=1)))
                gap_start = None
    if gap_start is not None:
        uncached_gaps.append((gap_start, all_dates[-1]))

    # Fetch each contiguous uncached gap with one HTTP call and cache by day.
    for gap_s, gap_e in uncached_gaps:
        log.debug("LAN batch fetch %s/%s %s → %s", asset, interval, gap_s, gap_e)
        gap_df = _fetch_matrix_range(asset, interval, gap_s, gap_e)
        if gap_df is not None and not gap_df.empty:
            _cache_range(asset, interval, gap_df)

    # Read all days from cache (now warm).
    frames: list[pd.DataFrame] = []
    for d in all_dates:
        date_str = d.strftime("%Y-%m-%d")
        path = _cache_path(asset, interval, date_str)
        if path.exists():
            try:
                day_df = pd.read_parquet(path)
                if day_df is not None and not day_df.empty:
                    frames.append(day_df)
            except Exception:
                pass  # corrupt cache day — skip

    if not frames:
        return df

    lan_df = pd.concat(frames, ignore_index=True)
    lan_df = lan_df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    return _merge_lan(df, lan_df)


def _list_asset_metrics(asset: str) -> list[str]:
    """Return the metric names the API exposes for an asset (excludes _SKIP_COLS)."""
    url = f"{_base_url()}/assets/{asset}/metrics"
    try:
        r = _get_session().get(url, timeout=10)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        log.debug("LAN /assets/%s/metrics: %s", asset, exc)
        return []
    items = payload.get("metrics", payload) if isinstance(payload, dict) else payload
    names: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("metric_name") or item.get("metric") or item.get("name") or ""
        else:
            continue
        if name and name not in _SKIP_COLS and name not in names:
            names.append(name)
    return names


def _enrich_live(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Merge the freshest non-stale metrics from /metrics/latest onto df.

    The endpoint requires an explicit ``metric`` list (array) and returns
    ``{"asset": ..., "metrics": {name: {datetime, value, collection_interval}}}``.
    Metrics older than 5× their collection interval are skipped as dead data.
    """
    metrics = _list_asset_metrics(asset)
    if not metrics:
        return df

    url = f"{_base_url()}/metrics/latest"
    try:
        r = _get_session().get(url, params={"asset": asset, "metric": metrics}, timeout=10)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        log.debug("LAN /metrics/latest %s: %s", asset, exc)
        return df

    # New shape: {"metrics": {name: {datetime, value, collection_interval}}}.
    # Tolerate the legacy list-of-rows shape for resilience.
    entries = payload.get("metrics", payload) if isinstance(payload, dict) else payload
    if not entries:
        return df
    if isinstance(entries, list):
        entries = {
            row.get("metric"): row
            for row in entries
            if isinstance(row, dict) and row.get("metric")
        }
    if not isinstance(entries, dict):
        return df

    now = datetime.now(timezone.utc)
    fresh: dict[str, object] = {}
    ts_map: dict[str, datetime] = {}

    for metric, info in entries.items():
        if not metric or metric in _SKIP_COLS or not isinstance(info, dict):
            continue

        raw_dt = info.get("datetime") or info.get("timestamp")
        if not raw_dt:
            continue
        try:
            metric_dt = pd.to_datetime(raw_dt, utc=True).to_pydatetime()
        except Exception:
            continue

        interval_s = _interval_to_seconds(info.get("collection_interval") or info.get("interval_seconds"))
        if (now - metric_dt) > timedelta(seconds=interval_s * _MAX_STALENESS_MULT):
            log.debug("LAN skip stale metric %s (last: %s)", metric, metric_dt)
            continue

        value = info.get("value")
        if value is not None:
            fresh[metric] = value
            ts_map[metric] = metric_dt

    if not fresh:
        return df

    latest_ts = max(ts_map.values())
    lan_df = pd.DataFrame([{"timestamp": latest_ts, **fresh}])
    lan_df["timestamp"] = pd.to_datetime(lan_df["timestamp"], utc=True)
    return _merge_lan(df, lan_df)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class LanEnricher:
    """External metrics enricher using the local LAN metrics API.

    Backtest path (live=False): calls /features/matrix per calendar day,
    caching results to parquet under <data_root>/lan_cache/.

    Live scanner path (live=True): calls /metrics/latest and applies a
    staleness filter — metrics not updated within 5× their collection
    interval are skipped to prevent joining dead data.
    """

    def enrich(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        *,
        live: bool = False,
    ) -> pd.DataFrame:
        if df is None or df.empty:
            return df

        asset = _SYMBOL_MAP.get(symbol)
        if not asset:
            log.debug("LanEnricher: no asset mapping for %s", symbol)
            return df

        # LAN API supports all intervals directly (confirmed: 5m has all 47 enrichment columns).
        lan_interval = timeframe

        try:
            if live:
                return _enrich_live(df, asset)
            return _enrich_historical(df, asset, lan_interval)
        except Exception as exc:
            log.warning("LanEnricher skipped for %s/%s: %s", symbol, timeframe, exc)
            return df

    def available_metrics(
        self,
        symbol: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[str]:
        """Return metric names available for the asset, optionally filtered by window.

        Used at strategy-generation time to inject the actual available column
        list into the agent's context so it can reason about data availability.
        """
        asset = _SYMBOL_MAP.get(symbol)
        if not asset:
            return []
        url = f"{_base_url()}/assets/{asset}/metrics"
        try:
            r = _get_session().get(url, timeout=10)
            r.raise_for_status()
            items = r.json()
        except Exception as exc:
            log.debug("LAN /assets/%s/metrics: %s", asset, exc)
            return []

        # API returns either a list directly or {"asset": ..., "metrics": [...]}
        if isinstance(items, dict):
            items = items.get("metrics") or []

        result: list[str] = []
        for item in items:
            if isinstance(item, str):
                name = item
                first_ts = last_ts = None
            elif isinstance(item, dict):
                name = item.get("metric") or item.get("metric_name") or item.get("name") or ""
                first_ts = item.get("first_timestamp") or item.get("first_ts")
                last_ts = item.get("last_timestamp") or item.get("last_ts")
            else:
                continue

            if not name or name in _SKIP_COLS:
                continue

            if start is not None and end is not None and first_ts and last_ts:
                try:
                    first_dt = pd.to_datetime(first_ts, utc=True)
                    last_dt = pd.to_datetime(last_ts, utc=True)
                    start_aware = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                    end_aware = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
                    if first_dt > end_aware or last_dt < start_aware:
                        continue
                except Exception:
                    pass

            result.append(name)

        return result


_enricher = LanEnricher()


def get_lan_enricher() -> LanEnricher:
    return _enricher
