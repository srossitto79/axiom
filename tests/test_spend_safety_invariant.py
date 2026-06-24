"""End-to-end spend-safety invariant at the real call boundary.

Proves the chokepoint in ai._call_single fails closed: when enforcement is on,
a (provider, model) that is not connected+selected raises UnconfiguredRouteError
BEFORE any token resolution or HTTP request — so the bot never spends on a model
the operator did not connect and select.
"""

from __future__ import annotations

import asyncio

import pytest

from forven import ai
from forven import model_selection as ms
from forven.db import kv_set

_MSG = [{"role": "user", "content": "hi"}]


def _no_http_guard(monkeypatch):
    # If execution ever reaches token resolution, the call would proceed to HTTP
    # — fail the test loudly instead.
    def _boom(_provider):
        raise AssertionError("reached get_token/HTTP path for an unselected model")

    monkeypatch.setattr(ai, "get_token", _boom)


def test_call_single_fails_closed_when_nothing_connected(forven_db, monkeypatch):
    ms.enable_enforcement()
    kv_set(ms._CONNECTED_PROVIDERS_KEY, [])
    kv_set(ms._SETTINGS_STORAGE_KEY, {"agent_model_keys": []})
    _no_http_guard(monkeypatch)
    with pytest.raises(ms.UnconfiguredRouteError):
        asyncio.run(ai._call_single("openai", "gpt-5.2", _MSG, 16, 0.7, None))


def test_call_single_blocks_other_provider_when_one_connected(forven_db, monkeypatch):
    ms.enable_enforcement()
    kv_set(ms._CONNECTED_PROVIDERS_KEY, ["gemini"])
    kv_set(ms._SETTINGS_STORAGE_KEY, {"agent_model_keys": ["gemini:gemini-2.5-flash-lite"]})
    monkeypatch.setattr(ms, "_provider_has_token", lambda p: p == "gemini")
    _no_http_guard(monkeypatch)
    # A stray openai default must NOT be callable just because gemini is connected.
    with pytest.raises(ms.UnconfiguredRouteError):
        asyncio.run(ai._call_single("openai", "gpt-5.2", _MSG, 16, 0.7, None))


def test_enforcement_off_does_not_block(forven_db, monkeypatch):
    # Un-migrated process (enforcement off): the chokepoint is a no-op so the
    # call proceeds to token resolution (which we intercept here).
    ms.disable_enforcement()
    kv_set(ms._CONNECTED_PROVIDERS_KEY, [])
    reached = {"token": False}

    def _mark(_provider):
        reached["token"] = True
        raise RuntimeError("stop after chokepoint")

    monkeypatch.setattr(ai, "get_token", _mark)
    with pytest.raises(RuntimeError):
        asyncio.run(ai._call_single("openai", "gpt-5.2", _MSG, 16, 0.7, None))
    assert reached["token"] is True  # chokepoint allowed passage when off
