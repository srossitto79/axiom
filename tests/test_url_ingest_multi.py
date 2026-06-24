"""Combine several source URLs into ONE crucible (POST /api/hypotheses/from_urls).

The single-URL path creates one crucible per URL; the combine path attaches
every successfully-extracted source to a single crucible and enqueues one
research task spanning all of them.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from forven.api import app
from forven.control_plane import ops as control_plane_ops
from forven.db import get_db
from forven.hypotheses import list_hypothesis_artifacts

_PREVIEWS = {
    "https://youtube.com/watch?v=aaa": {
        "ok": True,
        "source_type": "youtube",
        "url": "https://youtube.com/watch?v=aaa",
        "title": "Video A",
        "content": "transcript A about order blocks",
        "content_bytes": 31,
    },
    "https://youtube.com/watch?v=bbb": {
        "ok": True,
        "source_type": "youtube",
        "url": "https://youtube.com/watch?v=bbb",
        "title": "Video B",
        "content": "transcript B about liquidity sweeps",
        "content_bytes": 35,
    },
    "https://youtube.com/watch?v=ccc": {
        "ok": False,
        "source_type": "youtube",
        "error_code": "transcript_unavailable",
        "error": "no transcript",
    },
}


def _fake_fetch_preview(url: str):
    return _PREVIEWS[url]


def test_from_urls_combines_into_one_crucible(forven_db):
    control_plane_ops.update_system_mode("auto")
    with patch("forven.api_domains.hypotheses.fetch_preview", side_effect=_fake_fetch_preview):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_urls",
            json={
                "urls": [
                    "https://youtube.com/watch?v=aaa",
                    "https://youtube.com/watch?v=bbb",
                    "https://youtube.com/watch?v=ccc",  # fails extraction
                ]
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    hid = body["hypothesis"]["id"]

    # ONE hypothesis, TWO artifacts (the failed source attaches nothing).
    artifacts = list_hypothesis_artifacts(hid)
    assert len(artifacts) == 2
    assert {a["source_ref"] for a in artifacts} == {
        "https://youtube.com/watch?v=aaa",
        "https://youtube.com/watch?v=bbb",
    }

    # Per-URL outcome is echoed back for the UI.
    by_url = {s["url"]: s for s in body["sources"]}
    assert by_url["https://youtube.com/watch?v=aaa"]["ok"] is True
    assert by_url["https://youtube.com/watch?v=bbb"]["ok"] is True
    assert by_url["https://youtube.com/watch?v=ccc"]["ok"] is False
    assert by_url["https://youtube.com/watch?v=ccc"]["error_code"] == "transcript_unavailable"

    # Exactly ONE research task, spanning both successful sources.
    assert body["task"]["task_id"] is not None
    with get_db() as conn:
        rows = conn.execute(
            "SELECT agent_id, type, input_data FROM agent_tasks WHERE id = ?",
            (body["task"]["task_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "strategy-developer"
    assert rows[0]["type"] == "research"
    input_data = json.loads(rows[0]["input_data"])
    assert input_data["origin_mode"] == "operator_url_paste"
    assert input_data["hypothesis_id"] == hid
    assert len(input_data["sources"]) == 2
    # Back-compat scalars still present (primary source).
    assert input_data["source_type"] == "youtube"
    assert input_data["source_url"] == "https://youtube.com/watch?v=aaa"


def test_from_urls_dedupes_identical_urls(forven_db):
    control_plane_ops.update_system_mode("auto")
    with patch("forven.api_domains.hypotheses.fetch_preview", side_effect=_fake_fetch_preview):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_urls",
            json={
                "urls": [
                    "https://youtube.com/watch?v=aaa",
                    "https://youtube.com/watch?v=aaa",  # duplicate
                ]
            },
        )

    body = r.json()
    assert body["ok"] is True
    artifacts = list_hypothesis_artifacts(body["hypothesis"]["id"])
    assert len(artifacts) == 1


def test_from_urls_all_fail_creates_nothing(forven_db):
    def _all_fail(url: str):
        return {
            "ok": False,
            "source_type": "youtube",
            "error_code": "transcript_unavailable",
            "error": "no transcript",
        }

    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM hypotheses").fetchone()["n"]

    with patch("forven.api_domains.hypotheses.fetch_preview", side_effect=_all_fail):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_urls",
            json={"urls": ["https://youtube.com/watch?v=ccc"]},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "all_sources_failed"
    assert body["sources"][0]["ok"] is False

    with get_db() as conn:
        after = conn.execute("SELECT COUNT(*) AS n FROM hypotheses").fetchone()["n"]
    assert after == before


def test_from_urls_empty_list_rejected(forven_db):
    client = TestClient(app)
    r = client.post("/api/hypotheses/from_urls", json={"urls": []})
    assert r.status_code == 400
