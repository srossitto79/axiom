"""Tests for build_agent_context() data schema injection."""
from datetime import date
from unittest.mock import patch


def _make_read_workspace_mock(schema_content: str | None):
    """Return a mock for read_workspace that returns schema_content for DATA_SCHEMA.md."""
    def _mock(filename: str, optional: bool = False) -> str | None:
        if filename == "DATA_SCHEMA.md":
            return schema_content
        # Return None for all other files so parts list stays short
        return None
    return _mock


def test_compact_data_schema_injected_when_present():
    """build_agent_context includes bounded DATA SCHEMA guidance when DATA_SCHEMA.md exists."""
    schema = "## Core Columns\n- timestamp\n- close"
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(schema)):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            # Patch at the source (axiom.db.get_db) not Axiom.context.get_db — context.py
            # imports get_db locally inside the function body, so there is no module-level
            # name to intercept at Axiom.context.get_db.
            with patch("axiom.db.get_db", side_effect=Exception("no db")):
                from axiom.context import build_agent_context
                result = build_agent_context(
                    agent_id="quant-researcher",
                    role_md="You are a quant researcher.",
                )
    assert "# DATA SCHEMA" in result
    assert "Always-present OHLCV columns" in result
    assert "Full reference remains in `DATA_SCHEMA.md`" in result
    assert "## Core Columns" not in result


def test_data_schema_compact_block_present_even_when_file_absent():
    """The compact schema (incl. the always-guard rule) is injected unconditionally.

    The column-guard rule is the most important safety guidance for any agent that
    writes strategy code, so it must reach workers even on a fresh workspace that has
    not created DATA_SCHEMA.md yet.
    """
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            # See first test for why we patch axiom.db.get_db and not Axiom.context.get_db
            with patch("axiom.db.get_db", side_effect=Exception("no db")):
                from axiom.context import build_agent_context
                result = build_agent_context(
                    agent_id="quant-researcher",
                    role_md="You are a quant researcher.",
                )
    assert "# DATA SCHEMA" in result
    assert "if 'col' in df.columns" in result
    # Original role block still present
    assert "# YOUR ROLE" in result


def test_build_agent_context_includes_compact_policy_blocks():
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            with patch("axiom.db.get_db", side_effect=Exception("no db")):
                from axiom.context import build_agent_context
                result = build_agent_context(
                    agent_id="risk-manager",
                    role_md="You manage risk.",
                )

    assert "# EXTERNAL / UNTRUSTED CONTENT" in result
    assert "# OPERATING RULES" in result
    assert "# CURRENT RISK POLICY (enforced by code)" in result


def test_build_agent_context_preserves_load_bearing_worker_rules():
    """Guard the worker behavioral contract that replaced the full IDENTITY.md dump.

    IDENTITY.md is no longer injected into worker context (only the compact blocks
    are). These specific lines are the load-bearing pieces a worker acts on — if a
    future edit drops them, fail here instead of silently shipping degraded agents.
    """
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            with patch("axiom.db.get_db", side_effect=Exception("no db")):
                from axiom.context import build_agent_context
                result = build_agent_context(
                    agent_id="risk-manager",
                    role_md="You manage risk.",
                )

    # Escalation mechanism must be named, not just implied.
    assert "request_fix" in result
    assert "Never silently retry or work around it" in result
    # Active risk limits must be surfaced.
    assert "Drawdown kill-switch" in result
    assert "Max risk per trade" in result


def test_build_agent_context_still_includes_chroma_recall_when_task_present():
    """Non-research agent context should keep Chroma recall behavior."""
    chroma_recall = "# RELEVANT PRIOR RESEARCH (from ChromaDB)\n- prior result"
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch("axiom.context._get_chroma_recall", return_value=chroma_recall):
            with patch("axiom.db.get_db", side_effect=Exception("no db")):
                from axiom.context import build_agent_context
                result = build_agent_context(
                    agent_id="quant-researcher",
                    role_md="You are a quant researcher.",
                    task_description="Investigate funding dislocations",
                )

    assert "# YOUR ROLE" in result
    assert chroma_recall in result


def test_build_agent_context_includes_strategy_diversity_guard_when_saturated():
    with patch("axiom.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            with patch(
                "axiom.context.render_strategy_diversity_guard",
                return_value="# STRATEGY DIVERSITY GUARD\n- RSI is cooled down.",
            ):
                with patch("axiom.db.get_db", side_effect=Exception("no db")):
                    from axiom.context import build_agent_context

                    result = build_agent_context(
                        agent_id="strategy-developer",
                        role_md="You create strategies.",
                        task_description="Generate a new strategy",
                    )

    assert "# STRATEGY DIVERSITY GUARD" in result
    assert "RSI is cooled down" in result


def test_build_agent_context_uses_utc_daily_memory_paths_when_enabled():
    """Daily memory reads should align with the UTC-based filenames used by the runner."""
    requested_files: list[str] = []

    def _mock_read_workspace(filename: str, optional: bool = False) -> str | None:
        requested_files.append(filename)
        return None

    with patch("axiom.context.read_workspace", side_effect=_mock_read_workspace):
        with patch("axiom.context._get_chroma_recall", return_value=""):
            with patch("axiom.context._utc_today", return_value=date(2026, 4, 14)):
                with patch("axiom.context._utc_yesterday", return_value=date(2026, 4, 13)):
                    with patch("axiom.db.get_db", side_effect=Exception("no db")):
                        from axiom.context import build_agent_context
                        build_agent_context(
                            agent_id="quant-researcher",
                            role_md="You are a quant researcher.",
                            include_daily_memory=True,
                        )

    assert "agents/quant-researcher/memory/2026-04-14.md" in requested_files
    assert "agents/quant-researcher/memory/2026-04-13.md" in requested_files
