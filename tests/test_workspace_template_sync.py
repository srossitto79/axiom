"""Workspace template sync safety tests."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    from axiom import config

    monkeypatch.setattr(config, "WORKSPACE_DIR", ws, raising=False)
    monkeypatch.setattr(config, "LEGACY_WORKSPACE_DIR", ws, raising=False)

    import axiom.workspace as ws_mod

    importlib.reload(ws_mod)
    monkeypatch.setattr(ws_mod, "WORKSPACE_DIR", ws, raising=False)
    monkeypatch.setattr(ws_mod, "LEGACY_WORKSPACE_DIR", ws, raising=False)
    yield ws_mod, ws
    importlib.reload(ws_mod)


def test_sync_templates_dry_run_does_not_write(workspace):
    ws_mod, ws = workspace
    old = (
        '**You ARE Axiom.** Don\'t talk about "reading files", "sessions", '
        '"context windows", "system prompts", or "tokens". You simply know '
        "things because you are Axiom. If something isn't in front of you, "
        'say "I\'m not sure" naturally — never "I don\'t have access to that."'
    )
    (ws / "SOUL.md").write_text(old, encoding="utf-8")

    result = ws_mod.sync_workspace_templates(apply=False)

    assert result["summary"]["patch"] == 1
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == old


def test_sync_templates_applies_safe_global_patches(workspace):
    ws_mod, ws = workspace
    lessons = (
        "# LESSONS.md — Trading Intelligence Insights\n\n"
        "*Last updated: (date)*\n"
        "# S50209 Review Summary (March 27, 2026)\n"
        "stale incident details\n"
    )
    agents = (
        "- Learned a lesson → update `LESSONS.md`. Made a mistake → document it so future-you doesn't repeat it.\n"
        "- Don't run destructive commands without asking. When genuinely in doubt, ask.\n"
    )
    (ws / "LESSONS.md").write_text(lessons, encoding="utf-8")
    (ws / "AGENTS.md").write_text(agents, encoding="utf-8")

    result = ws_mod.sync_workspace_templates(apply=True)

    assert result["summary"]["patch"] == 2
    assert "S50209 Review Summary" not in (ws / "LESSONS.md").read_text(encoding="utf-8")
    synced_agents = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "Do not edit `SOUL.md`" in synced_agents
    assert "acceptable to mention records" in synced_agents


def test_sync_templates_skips_custom_files_without_force(workspace):
    ws_mod, ws = workspace
    (ws / "IDENTITY.md").write_text("# CUSTOM\nKeep me.\n", encoding="utf-8")

    result = ws_mod.sync_workspace_templates(apply=True)

    skipped = [
        item for item in result["results"]
        if item["path"] == "IDENTITY.md" and item["action"] == "skip_custom"
    ]
    assert skipped
    assert (ws / "IDENTITY.md").read_text(encoding="utf-8") == "# CUSTOM\nKeep me.\n"


def test_sync_templates_patches_per_agent_docs_without_force_overwrite(workspace):
    ws_mod, ws = workspace
    agent_dir = ws / "agents" / "risk-manager"
    agent_dir.mkdir(parents=True)
    old_agents = (
        "# Risk Manager — Workspace Guide\n\n"
        "- Don't run destructive commands without asking. When genuinely in doubt, ask.\n"
    )
    (agent_dir / "AGENTS.md").write_text(old_agents, encoding="utf-8")

    result = ws_mod.sync_workspace_templates(apply=True, force=True, include_agents=True)

    agent_items = [
        item for item in result["results"]
        if item["path"] == "agents/risk-manager/AGENTS.md"
    ]
    assert agent_items[0]["action"] == "skip_agent_force_unsupported"
    assert (agent_dir / "AGENTS.md").read_text(encoding="utf-8") == old_agents

    ws_mod.sync_workspace_templates(apply=True, include_agents=True)
    assert "acceptable to mention records" in (agent_dir / "AGENTS.md").read_text(
        encoding="utf-8",
    )
