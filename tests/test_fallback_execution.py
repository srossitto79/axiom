"""Per-slot fallback chains actually EXECUTE at runtime.

Covers: call_ai(route=...) iterating an explicit chain, get_auxiliary_routing
exposing its configured aux fallbacks, and the agent tool-call chain using the
per-agent (agent:<id>) fallback list instead of the per-provider chain.
"""

from __future__ import annotations

import asyncio

from forven import ai
from forven import model_routing as mr


def test_call_ai_route_iterates_explicit_chain(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_call_single(provider, model, *a, **k):
        calls.append((provider, model))
        if provider == "gemini":
            raise RuntimeError("gemini down")
        return f"OK::{provider}/{model}"

    monkeypatch.setattr(ai, "_call_single", fake_call_single)
    out = asyncio.run(
        ai.call_ai("gemini", "g1", prompt="hi", route=[("gemini", "g1"), ("openai", "o1")])
    )
    assert out == "OK::openai/o1"
    assert calls == [("gemini", "g1"), ("openai", "o1")]  # tried in order


def test_call_ai_empty_route_raises(monkeypatch):
    monkeypatch.setattr(ai, "_call_single", lambda *a, **k: "x")
    try:
        asyncio.run(ai.call_ai("gemini", "g", prompt="hi", route=[]))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_get_auxiliary_routing_exposes_configured_fallbacks(forven_db):
    mr.update_model_routing({
        "auxiliary": {"recall": {"provider": "gemini", "model_id": "gemini-2.5-flash-lite",
                                 "base_url": None, "api_key": None}},
        "fallback_chains": {"aux:recall": [{"provider": "groq", "model_id": "llama-3.3-70b-versatile"}]},
    })
    routing = mr.get_auxiliary_routing("recall")
    assert routing["fallbacks"] == [("groq", "llama-3.3-70b-versatile")]


def test_tool_call_chain_uses_per_agent_fallbacks(forven_db):
    from forven.agents.runner import _resolve_tool_call_chain

    mr.update_model_routing({
        "fallback_chains": {
            "agent:strategy-developer": [
                {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
            ],
        },
    })
    chain = _resolve_tool_call_chain("groq", "llama-3.3-70b-versatile", "strategy-developer")
    # The agent's model first, then ITS configured fallback (not a per-provider chain).
    assert chain[0] == ("groq", "llama-3.3-70b-versatile")
    assert ("gemini", "gemini-2.5-flash-lite") in chain
    # The per-provider seed chain for groq (which used to include openai) is NOT used.
    assert ("openai", "gpt-5.2") not in chain
