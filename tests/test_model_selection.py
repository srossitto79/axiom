"""Keystone tests: fail-closed connect + select gates and route resolution.

These lock the spend-safety invariant: only providers explicitly CONNECTED
in-app are usable, an env-var key alone never authorizes spend, and a route
that isn't connected+selected fails closed instead of substituting a default.
"""

from __future__ import annotations

import pytest

from forven import model_selection as ms
from forven.db import get_db, kv_set


def _connect(*providers: str) -> None:
    kv_set(ms._CONNECTED_PROVIDERS_KEY, sorted(providers))


def _set_enabled(*keys: str) -> None:
    kv_set(ms._SETTINGS_STORAGE_KEY, {"agent_model_keys": list(keys)})


def _all_tokens_present(monkeypatch) -> None:
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: True)


def test_env_only_key_is_not_connected(forven_db, monkeypatch):
    # Token resolves (e.g. from a stray env var) but the provider was never
    # connected in-app -> NOT connected, cannot authorize spend.
    _all_tokens_present(monkeypatch)
    _connect()  # nothing connected in-app
    assert ms.provider_is_connected("openai") is False


def test_connected_requires_both_record_and_token(forven_db, monkeypatch):
    _connect("gemini")
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: provider == "gemini")
    assert ms.provider_is_connected("gemini") is True
    # Marked connected but token gone -> not connected.
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: False)
    assert ms.provider_is_connected("gemini") is False


def test_mark_and_unmark(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    ms.mark_provider_connected("groq")
    assert "groq" in ms.list_connected_providers()
    assert ms.provider_is_connected("groq") is True
    ms.unmark_provider_connected("groq")
    assert "groq" not in ms.list_connected_providers()
    assert ms.provider_is_connected("groq") is False


def test_allowed_pairs_intersect_connected_and_selected(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    _connect("gemini")  # only gemini connected
    _set_enabled("gemini:gemini-2.5-flash-lite", "openai:gpt-5.2")
    allowed = ms.allowed_pairs()
    assert ("gemini", "gemini-2.5-flash-lite") in allowed
    # openai enabled but NOT connected -> excluded.
    assert ("openai", "gpt-5.2") not in allowed


def test_keyless_local_provider_usable_without_token(forven_db, monkeypatch):
    # LM Studio is a local OpenAI-compatible server — keyless. get_token() returns
    # "" by design, so a token requirement would make it permanently
    # unconnectable/uncallable. A configured profile (base URL) counts as usable.
    import forven.auth.store as store

    monkeypatch.setattr(store, "get_token", lambda provider: "")
    monkeypatch.setattr(
        store,
        "get_profile",
        lambda provider: {"base_url": "http://localhost:1234"} if provider == "lmstudio" else None,
    )
    ms.enable_enforcement()
    _connect("lmstudio", "openai")
    _set_enabled("lmstudio:local-model")
    assert ms.provider_is_connected("lmstudio") is True
    assert ms.is_callable("lmstudio", "local-model") is True
    # A PAID provider with no usable token is still NOT connected, even if marked.
    assert ms.provider_is_connected("openai") is False


def test_model_match_is_case_insensitive(forven_db, monkeypatch):
    # The operator enabled the catalog's canonical casing (MiniMax-M2.7) but a
    # stored agent/brain model id arrives lowercased (minimax-m2.7). Same model —
    # the gate must NOT reject it on a mere case mismatch ("minimax connected but
    # model not selected" false negative).
    _all_tokens_present(monkeypatch)
    ms.enable_enforcement()
    _connect("minimax")
    _set_enabled("minimax:MiniMax-M2.7")
    assert ms.is_callable("minimax", "minimax-m2.7") is True
    assert ms.is_callable("minimax", "MINIMAX-M2.7") is True
    ms.assert_callable("minimax", "minimax-m2.7", slot="call_ai:minimax")  # must not raise
    # A genuinely different, unselected model is still blocked (not just a case
    # variant of an enabled one).
    assert ms.is_callable("minimax", "not-a-real-minimax-model") is False


def test_resolve_route_fails_closed_when_nothing_selected(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    ms.enable_enforcement()
    _connect()  # nothing connected
    _set_enabled()
    with pytest.raises(ms.UnconfiguredRouteError):
        ms.resolve_route("agent:x", "openai", "gpt-5.2")


def test_resolve_route_drops_unallowed_primary_and_fallbacks(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    ms.enable_enforcement()
    _connect("gemini")
    _set_enabled("gemini:gemini-2.5-flash-lite")
    # Primary openai is not connected -> dropped; gemini fallback survives.
    route = ms.resolve_route(
        "agent:x", "openai", "gpt-5.2",
        fallbacks=[("gemini", "gemini-2.5-flash-lite"), ("openai", "gpt-4o-mini")],
    )
    assert route == [("gemini", "gemini-2.5-flash-lite")]


def test_resolve_route_passthrough_when_enforcement_off(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    ms.disable_enforcement()
    _connect()  # nothing connected, but enforcement off -> passthrough
    route = ms.resolve_route("agent:x", "openai", "gpt-5.2")
    assert route == [("openai", "gpt-5.2")]


def test_assert_callable_noop_when_enforcement_off(forven_db, monkeypatch):
    ms.disable_enforcement()
    _connect()
    ms.assert_callable("openai", "gpt-5.2", slot="t")  # must not raise


def test_assert_callable_blocks_unconnected_and_unselected(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    ms.enable_enforcement()
    _connect("gemini")
    _set_enabled("gemini:gemini-2.5-flash-lite")
    # Unconnected provider -> blocked.
    with pytest.raises(ms.UnconfiguredRouteError):
        ms.assert_callable("openai", "gpt-5.2", slot="t")
    # Connected provider but model not selected -> blocked.
    with pytest.raises(ms.UnconfiguredRouteError):
        ms.assert_callable("gemini", "gemini-3-pro-preview", slot="t")
    # Connected + selected -> allowed (no raise).
    ms.assert_callable("gemini", "gemini-2.5-flash-lite", slot="t")


def test_agent_row_selection_is_allowed_when_connected(forven_db, monkeypatch):
    _all_tokens_present(monkeypatch)
    _connect("openrouter")
    _set_enabled()  # not in enable list, but it's the agent's explicit selection
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, role, model, model_id, enabled) "
            "VALUES ('a1','A','r','openrouter','nvidia/nemotron-3-ultra-550b-a55b:free',1)"
        )
    assert ms.is_callable("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free") is True


def test_migrate_seeds_connected_from_profiles(forven_db, monkeypatch):
    monkeypatch.setattr(
        ms, "load_auth",
        lambda: {"profiles": {"gemini:default": {}, "groq:default": {}}},
        raising=False,
    )
    # load_auth is imported lazily inside the function; patch the source module.
    import forven.auth.store as store
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {"gemini:default": {}, "groq:default": {}}})
    seeded = ms.migrate_connected_from_profiles()
    assert {"gemini", "groq"} <= seeded
