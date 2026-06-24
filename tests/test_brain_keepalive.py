"""The Brain keepalive watchdog re-seeds a cycle when the loop has stalled.

The Brain is driven by an agent-callback chain with no periodic scheduler job;
a timed-out callback deliberately suppresses its retry, which used to strand the
whole AI loop until a manual restart. ensure_brain_keepalive() re-seeds a fresh
non-callback cycle when the Brain has gone silent with nothing pending.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import get_db
from forven import runtime_worker as rw
from forven import system_pause


def _insert_brain_task(status: str, completed_at: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (type, status, created_at, completed_at, source, payload, priority, retry_count) "
            "VALUES ('brain_invoke', ?, ?, ?, 'system', '{}', 0, 0)",
            (status, now, completed_at),
        )


def _nonterminal_brain_count() -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE type='brain_invoke' AND status NOT IN ('done','cancelled','failed')"
        ).fetchone()["n"]


def test_keepalive_seeds_when_brain_silent(forven_db, monkeypatch):
    monkeypatch.setattr(system_pause, "is_autonomy_paused", lambda: False)
    # No brain cycle has ever run -> stranded -> re-seed.
    assert _nonterminal_brain_count() == 0
    assert rw.ensure_brain_keepalive() is True
    assert _nonterminal_brain_count() == 1


def test_keepalive_seeds_after_long_silence(forven_db, monkeypatch):
    monkeypatch.setattr(system_pause, "is_autonomy_paused", lambda: False)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _insert_brain_task("done", old)  # last cycle finished 2h ago
    assert rw.ensure_brain_keepalive() is True
    assert _nonterminal_brain_count() == 1


def test_keepalive_noop_when_recent_cycle(forven_db, monkeypatch):
    monkeypatch.setattr(system_pause, "is_autonomy_paused", lambda: False)
    recent = datetime.now(timezone.utc).isoformat()
    _insert_brain_task("done", recent)  # just ran
    assert rw.ensure_brain_keepalive() is False
    assert _nonterminal_brain_count() == 0


def test_keepalive_noop_when_work_pending(forven_db, monkeypatch):
    monkeypatch.setattr(system_pause, "is_autonomy_paused", lambda: False)
    _insert_brain_task("pending", None)  # a cycle is already queued
    assert rw.ensure_brain_keepalive() is False
    # Still exactly the one we inserted — no duplicate seeded.
    assert _nonterminal_brain_count() == 1


def test_keepalive_respects_autonomy_pause(forven_db, monkeypatch):
    monkeypatch.setattr(system_pause, "is_autonomy_paused", lambda: True)
    # Stranded, but the operator paused autonomy -> do NOT auto-run the Brain.
    assert rw.ensure_brain_keepalive() is False
    assert _nonterminal_brain_count() == 0
