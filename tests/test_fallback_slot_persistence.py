"""Per-slot fallback chains (aux:<kind>, backup) must round-trip.

The Routing & Fallbacks UI stores per-slot fallback lists under slot-scoped
keys in fallback_chains. Coercion used to drop any non-provider key (so saved
fallbacks silently vanished on refresh). These tests lock the round-trip on both
the write (update_model_routing) and read (model-policy GET) paths.
"""

from __future__ import annotations

from forven import api_core as ac
from forven import model_routing as mr


def test_slot_fallback_chains_persist_through_update(forven_db):
    mr.update_model_routing({
        "fallback_chains": {
            "aux:recall": [
                {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
                {"provider": "groq", "model_id": "llama-3.3-70b-versatile"},
            ],
            "backup": [{"provider": "openai", "model_id": "gpt-5.2"}],
            # a real provider chain must still persist too
            "gemini": [{"provider": "gemini", "model_id": "gemini-2.5-flash-lite"}],
        },
    })
    chains = mr.get_model_routing()["fallback_chains"]
    assert chains["aux:recall"] == [
        {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
        {"provider": "groq", "model_id": "llama-3.3-70b-versatile"},
    ]
    assert chains["backup"] == [{"provider": "openai", "model_id": "gpt-5.2"}]
    assert chains["gemini"] == [{"provider": "gemini", "model_id": "gemini-2.5-flash-lite"}]


def test_slot_fallback_chains_returned_by_model_policy_get(forven_db):
    mr.update_model_routing({
        "fallback_chains": {
            "aux:skill_extraction": [{"provider": "gemini", "model_id": "gemini-2.5-flash"}],
            "backup": [{"provider": "groq", "model_id": "llama-3.3-70b-versatile"}],
        },
    })
    policy = ac._get_model_policy_compat()
    fc = policy["fallback_chains"]
    assert fc["aux:skill_extraction"] == [{"provider": "gemini", "model_id": "gemini-2.5-flash"}]
    assert fc["backup"] == [{"provider": "groq", "model_id": "llama-3.3-70b-versatile"}]


def test_unknown_slot_key_still_dropped(forven_db):
    mr.update_model_routing({
        "fallback_chains": {"aux:not_a_kind": [{"provider": "openai", "model_id": "gpt-5.2"}],
                            "garbage": [{"provider": "openai", "model_id": "gpt-5.2"}]},
    })
    chains = mr.get_model_routing()["fallback_chains"]
    assert "aux:not_a_kind" not in chains
    assert "garbage" not in chains
