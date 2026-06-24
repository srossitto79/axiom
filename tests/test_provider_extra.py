"""Cerebras / Mistral / xAI / Together provider wiring (OpenAI-compatible)."""

from __future__ import annotations

import forven.ai as ai
from forven import api_core as ac
from forven import model_routing as mr
from forven.agents.providers import (
    CerebrasProvider,
    MistralProvider,
    OpenAIProvider,
    TogetherProvider,
    XAIProvider,
    get_provider,
)


def test_factory_resolves_new_providers():
    assert isinstance(get_provider("cerebras"), CerebrasProvider)
    assert isinstance(get_provider("mistral"), MistralProvider)
    assert isinstance(get_provider("xai"), XAIProvider)
    assert isinstance(get_provider("together"), TogetherProvider)
    for cls in (CerebrasProvider, MistralProvider, XAIProvider, TogetherProvider):
        assert issubclass(cls, OpenAIProvider)


def test_endpoints_and_defaults():
    assert ai.ENDPOINTS["cerebras"] == "https://api.cerebras.ai/v1/chat/completions"
    assert ai.ENDPOINTS["mistral"] == "https://api.mistral.ai/v1/chat/completions"
    assert ai.ENDPOINTS["xai"] == "https://api.x.ai/v1/chat/completions"
    assert ai.ENDPOINTS["together"] == "https://api.together.xyz/v1/chat/completions"
    for p in ("cerebras", "mistral", "xai", "together"):
        assert p in mr._SUPPORTED_PROVIDERS
        assert mr.get_default_model_for_provider(p)


def test_discovery_belong_rules():
    assert ac._discovery_model_should_belong("cerebras", "llama-3.3-70b")
    assert ac._discovery_model_should_belong("mistral", "mistral-large-latest")
    assert not ac._discovery_model_should_belong("mistral", "mistral-embed")
    assert not ac._discovery_model_should_belong("mistral", "mistral-moderation-latest")
    assert ac._discovery_model_should_belong("xai", "grok-3-mini")
    assert not ac._discovery_model_should_belong("xai", "grok-2-image-1212")
    # Together is curated (no live discovery) -> belong-rule returns False.
    assert not ac._discovery_model_should_belong("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
