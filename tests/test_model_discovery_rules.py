"""Discovery belong-rules for auto-updating model lists.

Covers the provider /models → catalog pipeline added for Anthropic, Groq, and
Gemini so new releases (e.g. a newer Claude Opus) surface without a catalog
edit. These are pure functions — no network — exercising the id extraction and
the per-provider "belongs to this provider" filter.
"""

from __future__ import annotations

from forven import api_core as ac


def test_gemini_models_prefix_is_stripped() -> None:
    # Gemini's compat /models returns "models/<id>"; the chat endpoint wants bare.
    assert ac._normalize_model_id("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert ac._normalize_model_id("gemini-2.5-flash") == "gemini-2.5-flash"
    assert ac._normalize_model_id("  claude-opus-4-8  ") == "claude-opus-4-8"


def test_anthropic_belong_rule() -> None:
    assert ac._discovery_model_should_belong("anthropic", "claude-opus-4-8")
    assert ac._discovery_model_should_belong("anthropic", "claude-sonnet-4-6")
    assert not ac._discovery_model_should_belong("anthropic", "text-embedding-3")


def test_groq_belong_rule_rejects_display_names_and_non_chat() -> None:
    # Real callable ids (lowercase slugs, optional vendor/ prefix) are kept.
    assert ac._discovery_model_should_belong("groq", "llama-3.3-70b-versatile")
    assert ac._discovery_model_should_belong("groq", "openai/gpt-oss-120b")
    assert ac._discovery_model_should_belong("groq", "moonshotai/kimi-k2-instruct")
    # Display names (spaces / uppercase) that Groq also returns are rejected.
    assert not ac._discovery_model_should_belong("groq", "Llama 3.3 70B")
    assert not ac._discovery_model_should_belong("groq", "GPT OSS 120B")
    # Non-chat modalities are rejected.
    assert not ac._discovery_model_should_belong("groq", "whisper-large-v3")
    assert not ac._discovery_model_should_belong("groq", "canopylabs/orpheus-v1-english")
    assert not ac._discovery_model_should_belong("groq", "meta-llama/llama-guard-4-12b")


def test_gemini_belong_rule_keeps_chat_drops_other_modalities() -> None:
    assert ac._discovery_model_should_belong("gemini", "gemini-2.5-flash")
    assert ac._discovery_model_should_belong("gemini", "gemini-3-pro-preview")
    # Non-text modalities / different APIs are dropped.
    assert not ac._discovery_model_should_belong("gemini", "gemini-embedding-001")
    assert not ac._discovery_model_should_belong("gemini", "gemini-2.5-flash-image")
    assert not ac._discovery_model_should_belong("gemini", "gemini-3.1-flash-live-preview")
    assert not ac._discovery_model_should_belong("gemini", "gemini-robotics-er-1.5-preview")
    assert not ac._discovery_model_should_belong("gemini", "text-embedding-004")


def test_openai_belong_rule_auto_accepts_new_chat_models() -> None:
    # New chat/reasoning models auto-pass without a code change...
    for m in ("gpt-5.5", "gpt-6", "o3", "o4-mini", "o3-mini", "o5",
              "chatgpt-4o-latest", "codex-5.3", "gpt-4o-mini"):
        assert ac._discovery_model_should_belong("openai", m), m
    # ...while non-chat modalities are still dropped.
    for m in ("text-embedding-3-large", "whisper-1", "tts-1", "dall-e-3",
              "gpt-image-1", "omni-moderation-latest", "gpt-4o-realtime-preview",
              "gpt-4o-audio-preview", "gpt-4o-transcribe", "davinci-002", "babbage-002"):
        assert not ac._discovery_model_should_belong("openai", m), m


def test_deepseek_belong_rule() -> None:
    assert ac._discovery_model_should_belong("deepseek", "deepseek-chat")
    assert ac._discovery_model_should_belong("deepseek", "deepseek-reasoner")
    assert not ac._discovery_model_should_belong("deepseek", "gpt-4o")


def test_unwired_provider_still_returns_false() -> None:
    # Providers without a belong-rule fall back to the static catalog.
    assert not ac._discovery_model_should_belong("openrouter", "anthropic/claude-sonnet-4")


def test_extract_anthropic_models_payload() -> None:
    payload = {
        "data": [
            {"type": "model", "id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"},
            {"type": "model", "id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
        ],
        "has_more": False,
    }
    assert ac._extract_discovery_models(payload, "anthropic") == [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    ]


def test_extract_groq_payload_drops_display_name_pollution() -> None:
    # Groq returns both a callable id and a human display name; only the id stays.
    payload = {
        "data": [
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
            {"id": "openai/gpt-oss-120b", "name": "GPT OSS 120B"},
        ]
    }
    assert ac._extract_discovery_models(payload, "groq") == [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
    ]
