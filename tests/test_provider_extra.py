"""Cerebras / Mistral / xAI / Together / OpenCode provider wiring (OpenAI-compatible)."""

from __future__ import annotations

import forven.ai as ai
from forven import api_core as ac
from forven import model_routing as mr
from forven.agents.providers import (
    CerebrasProvider,
    MistralProvider,
    OpenAIProvider,
    OpenCodeGoProvider,
    OpenCodeZenProvider,
    TogetherProvider,
    XAIProvider,
    get_provider,
)


def test_factory_resolves_new_providers():
    assert isinstance(get_provider("cerebras"), CerebrasProvider)
    assert isinstance(get_provider("mistral"), MistralProvider)
    assert isinstance(get_provider("xai"), XAIProvider)
    assert isinstance(get_provider("together"), TogetherProvider)
    assert isinstance(get_provider("opencode-zen"), OpenCodeZenProvider)
    assert isinstance(get_provider("opencode-go"), OpenCodeGoProvider)
    for cls in (
        CerebrasProvider, MistralProvider, XAIProvider, TogetherProvider,
        OpenCodeZenProvider, OpenCodeGoProvider,
    ):
        assert issubclass(cls, OpenAIProvider)


def test_endpoints_and_defaults():
    assert ai.ENDPOINTS["cerebras"] == "https://api.cerebras.ai/v1/chat/completions"
    assert ai.ENDPOINTS["mistral"] == "https://api.mistral.ai/v1/chat/completions"
    assert ai.ENDPOINTS["xai"] == "https://api.x.ai/v1/chat/completions"
    assert ai.ENDPOINTS["together"] == "https://api.together.xyz/v1/chat/completions"
    assert ai.ENDPOINTS["opencode-zen"] == "https://opencode.ai/zen/v1/chat/completions"
    assert ai.ENDPOINTS["opencode-go"] == "https://opencode.ai/zen/go/v1/chat/completions"
    for p in ("cerebras", "mistral", "xai", "together", "opencode-zen", "opencode-go"):
        assert p in mr._SUPPORTED_PROVIDERS
        assert mr.get_default_model_for_provider(p)


def test_gateway_providers_not_hijacked_by_model_name():
    # OpenCode Zen/GO are gateways serving many model families. An EXPLICIT
    # provider must never be re-routed by model NAME — regression: a "glm"/
    # "minimax" model id rewrote opencode-go to zai/minimax, which corrupted
    # both the enable-list key and the runtime route on every save+reload.
    assert ai.normalize_provider_and_model("opencode-go", "glm-5.2") == ("opencode-go", "glm-5.2")
    assert ai.normalize_provider_and_model("opencode-zen", "glm-4.6") == ("opencode-zen", "glm-4.6")
    assert ai.normalize_provider_and_model("opencode-go", "minimax-m3") == ("opencode-go", "minimax-m3")
    assert ac._normalize_agent_model_key("opencode-go:glm-5.2") == "opencode-go:glm-5.2"
    # The legacy heuristic still self-corrects a genuinely misconfigured pair
    # (provider says openai but the model is unmistakably a Z.AI GLM model).
    assert ai.normalize_provider_and_model("openai", "glm-4.6") == ("zai", "glm-4.6")


def test_opencode_base_urls():
    # The adapter's default base drives both providers.py (live agent path) and
    # the api_core discovery/endpoint config; pin them so a typo can't drift.
    assert OpenCodeZenProvider.DEFAULT_BASE_URL == "https://opencode.ai/zen/v1"
    assert OpenCodeGoProvider.DEFAULT_BASE_URL == "https://opencode.ai/zen/go/v1"


def test_discovery_belong_rules():
    assert ac._discovery_model_should_belong("cerebras", "llama-3.3-70b")
    assert ac._discovery_model_should_belong("mistral", "mistral-large-latest")
    assert not ac._discovery_model_should_belong("mistral", "mistral-embed")
    assert not ac._discovery_model_should_belong("mistral", "mistral-moderation-latest")
    assert ac._discovery_model_should_belong("xai", "grok-3-mini")
    assert not ac._discovery_model_should_belong("xai", "grok-2-image-1212")
    # Together is curated (no live discovery) -> belong-rule returns False.
    assert not ac._discovery_model_should_belong("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
    # OpenCode Zen is live-discovered; GO has no /models route so it is curated.
    assert ac._discovery_model_should_belong("opencode-zen", "grok-code")
    assert "opencode-zen" in ac._MODEL_DISCOVERY_ALT_ENDPOINTS
    assert "opencode-go" not in ac._MODEL_DISCOVERY_ALT_ENDPOINTS
    assert not ac._discovery_model_should_belong("opencode-go", "glm-5.2")
