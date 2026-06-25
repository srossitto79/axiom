from __future__ import annotations

import pytest
from fastapi import HTTPException

from forven.api_domains import data as data_domain


class _LanHealthResponse:
    def __init__(self, payload, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error
        return None

    def json(self):
        return self._payload


def test_get_data_ingestion_runs_local_merges_and_normalizes(monkeypatch):
    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (False, ""))
    monkeypatch.setattr(
        "forven.data.get_active_ingestion_runs",
        lambda: [{"id": "run-1", "symbol": "BTC-USD", "status": "running", "started_at": "2026-03-06T00:05:00+00:00"}],
    )
    monkeypatch.setattr(
        "forven.data.scan_datasets",
        lambda: [{"symbol": "ETH-USD", "timeframe": "1h", "source": "local", "row_count": 120, "start_ts": "2026-03-05T00:00:00+00:00", "end_ts": "2026-03-06T00:00:00+00:00"}],
    )

    payload = data_domain.get_data_ingestion_runs()

    assert payload[0]["symbol"] == "BTC/USD"
    assert payload[1]["symbol"] == "ETH/USD"


def test_get_data_ingestion_runs_remote_delegates(monkeypatch):
    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (True, "http://remote"))
    monkeypatch.setattr(
        data_domain,
        "_fetch_remote_ingestion_runs",
        lambda remote_url, **kwargs: [{"id": "remote-run", "symbol": "BTC/USD"}],
    )

    payload = data_domain.get_data_ingestion_runs(symbol="BTC/USD")

    assert payload == [{"id": "remote-run", "symbol": "BTC/USD"}]


def test_get_datasets_stub_preserves_market_metadata(monkeypatch):
    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (False, ""))
    monkeypatch.setattr(
        "forven.data.scan_datasets",
        lambda: [
            {
                "symbol": "AAPL",
                "timeframe": "1h",
                "source": "polygon",
                "row_count": 240,
                "start_ts": "2026-03-01T00:00:00+00:00",
                "end_ts": "2026-03-06T00:00:00+00:00",
                "asset_class": "stock",
                "market_type": "equity",
            }
        ],
    )

    payload = data_domain.get_datasets_stub()

    assert payload == [
        {
            "id": "dataset-0-AAPL-1h",
            "symbol": "AAPL",
            "timeframe": "1h",
            "source": "polygon",
            "start_ts": "2026-03-01T00:00:00+00:00",
            "end_ts": "2026-03-06T00:00:00+00:00",
            "row_count": 240,
            "asset_class": "stock",
            "market_type": "equity",
        }
    ]


def test_get_data_health_includes_stream_freshness(monkeypatch):
    monkeypatch.setattr(
        "forven.data.compute_data_health",
        lambda: {"ok": True, "datasets": [], "coverage": {}},
    )
    monkeypatch.setattr(
        "forven.data_manager.data_manager_stats",
        lambda: {
            "funding": {
                "total_calls": 1,
                "total_errors": 0,
                "last_run_ts": "2026-04-23T00:00:00+00:00",
                "last_success_ts": "2026-04-23T00:00:00+00:00",
                "total_rows": 10,
            }
        },
    )
    monkeypatch.setattr("forven.data_manager._now_iso", lambda: "2026-04-23T00:01:00+00:00")

    payload = data_domain.get_data_health()

    assert payload["ok"] is True
    assert payload["generated_at"] == "2026-04-23T00:01:00+00:00"
    assert "streams" in payload
    assert payload["streams"]["funding"]["last_success_ts"] == "2026-04-23T00:00:00+00:00"
    assert payload["streams"]["oi"] == {"status": "never_ran"}


def test_probe_lan_health_uses_latest_rows_and_liquidation_names(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/assets/bitcoin/metrics")
        return _LanHealthResponse({
            "asset": "bitcoin",
            "metrics": [
                {
                    "metric_name": "long_liq_usd",
                    "max_date": data_domain.datetime.now(data_domain.timezone.utc).isoformat(),
                    "collection_interval": "1h",
                },
                {
                    "metric_name": "short_liq_usd",
                    "max_date": data_domain.datetime.now(data_domain.timezone.utc).isoformat(),
                    "collection_interval": "1h",
                },
                {
                    "metric_name": "l2_bid_depth",
                    "max_date": data_domain.datetime.now(data_domain.timezone.utc).isoformat(),
                    "collection_interval": "1h",
                },
                {
                    "metric_name": "active_addresses",
                    "max_date": data_domain.datetime.now(data_domain.timezone.utc).isoformat(),
                    "collection_interval": "1h",
                },
            ],
        })

    monkeypatch.setattr("requests.get", fake_get)

    streams = {row["stream"]: row for row in data_domain._probe_lan_health()}

    assert streams["lan_liquidations"]["status"] == "healthy"
    assert streams["lan_liquidations"]["total_rows"] == 2
    assert streams["lan_orderbook"]["status"] == "healthy"
    assert streams["lan_onchain"]["status"] == "healthy"
    assert streams["lan_sentiment"]["status"] == "never_ran"


def test_probe_lan_health_marks_stale_latest_rows_recovering(monkeypatch):
    def fake_get(url, **kwargs):
        return _LanHealthResponse({
            "asset": "bitcoin",
            "metrics": [
                {
                    "metric_name": "news_sentiment",
                    "max_date": "2000-01-01T00:00:00+00:00",
                    "collection_interval": "1h",
                },
            ],
        })

    monkeypatch.setattr("requests.get", fake_get)

    streams = {row["stream"]: row for row in data_domain._probe_lan_health()}

    assert streams["lan_sentiment"]["status"] == "recovering"
    assert streams["lan_sentiment"]["consecutive_failures"] == 1
    assert streams["lan_sentiment"]["last_error"] == "latest LAN metric is stale"
    assert streams["lan_sentiment"]["total_rows"] == 0


def test_get_dataset_detail_stub_translates_missing_file(monkeypatch):
    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (False, ""))
    monkeypatch.setattr("forven.data.get_dataset_detail", lambda symbol, timeframe: (_ for _ in ()).throw(FileNotFoundError("missing dataset")))

    with pytest.raises(HTTPException) as exc_info:
        data_domain.get_dataset_detail_stub("BTC/USD", "1h")

    assert exc_info.value.status_code == 404


def test_post_upload_csv_normalizes_symbol(monkeypatch):
    monkeypatch.setattr(
        "forven.data.process_csv_upload",
        lambda **kwargs: {"symbol": "BTC-USD", "rows_imported": 42},
    )

    payload = data_domain.post_upload_csv(
        content=b"ts,open,high,low,close,volume\n",
        filename="btc.csv",
        symbol="BTC/USD",
        timeframe="1h",
    )

    assert payload["symbol"] == "BTC/USD"
    assert payload["rows_imported"] == 42


def test_export_and_ohlcv_wrappers_preserve_payload_shape(monkeypatch):
    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (False, ""))
    monkeypatch.setattr(
        "forven.data.export_dataset_bytes",
        lambda **kwargs: (b"csv-data", "text/csv", "btc-1h.csv"),
    )
    monkeypatch.setattr(
        "forven.data.dataset_ohlcv",
        lambda **kwargs: {"symbol": "BTC-USD", "row_count": 2, "data": []},
    )

    exported = data_domain.get_dataset_export("BTC/USD", "1h")
    ohlcv = data_domain.get_dataset_ohlcv("BTC/USD", "1h")

    assert exported == (b"csv-data", "text/csv", "btc-1h.csv")
    assert ohlcv["symbol"] == "BTC/USD"
    assert ohlcv["row_count"] == 2


def test_resolve_existing_remote_data_root_rejects_parent_traversal():
    with pytest.raises(HTTPException) as exc_info:
        data_domain._resolve_existing_remote_data_root_path("../outside")

    assert exc_info.value.status_code == 400
    assert "traversal" in str(exc_info.value.detail).lower()


def test_resolve_existing_remote_data_root_enforces_allowed_root(monkeypatch, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    disallowed_root = tmp_path / "outside"
    disallowed_root.mkdir()

    monkeypatch.setattr(data_domain, "_resolve_remote_data_allowed_root", lambda: str(allowed_root))

    with pytest.raises(HTTPException) as exc_info:
        data_domain._resolve_existing_remote_data_root_path(str(disallowed_root))

    assert exc_info.value.status_code == 403
    assert "outside" in str(exc_info.value.detail).lower()
