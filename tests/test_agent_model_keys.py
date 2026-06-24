"""agent_model_keys must survive a save round-trip — including OpenRouter ids.

OpenRouter free models are ``vendor/model:free``, so the option key is
``openrouter:vendor/model:free`` (a colon inside the model_id). A normalizer
that rejected any model_id containing a colon silently dropped these on save,
so the Models-tab checkbox reverted and the model stayed disabled in the picker.
"""

from __future__ import annotations

from forven import api_core as ac


def test_normalize_keeps_openrouter_free_keys():
    cases = {
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free":
            "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter:qwen/qwen3-coder:free":
            "openrouter:qwen/qwen3-coder:free",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b":
            "openrouter:nvidia/nemotron-3-ultra-550b-a55b",
        "gemini:gemini-2.5-flash-lite": "gemini:gemini-2.5-flash-lite",
        "together:meta-llama/Llama-3.3-70B-Instruct-Turbo":
            "together:meta-llama/Llama-3.3-70B-Instruct-Turbo",
    }
    for raw, expected in cases.items():
        assert ac._normalize_agent_model_key(raw) == expected


def test_coerce_preserves_all_valid_keys_incl_colon_models():
    keys = [
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter:qwen/qwen3-coder:free",
        "groq:llama-3.3-70b-versatile",
        "gemini:gemini-2.5-flash",
    ]
    assert ac._coerce_agent_model_keys(keys) == keys


def test_normalize_still_rejects_garbage():
    assert ac._normalize_agent_model_key("") is None
    assert ac._normalize_agent_model_key("no-colon-here") is None
    assert ac._normalize_agent_model_key("provider-only:") is None
