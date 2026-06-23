"""Model routing policy shared by all inference paths."""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Iterable

from forven.db import kv_get, kv_set

log = logging.getLogger("forven.model_routing")

_SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "minimax",
    "lmstudio",
    "zai",
    "openrouter",
    "anthropic",
    "deepseek",
    "groq",
    "gemini",
)
_MODEL_ROUTING_STORAGE_KEY = "forven:model-routing"
_LEGACY_MODEL_ALIASES: dict[str, dict[str, str]] = {}
_LEGACY_PROVIDER_PRIORITY = ["openai", "minimax", "lmstudio", "zai"]
_ZAI_PRIMARY_PROVIDER_PRIORITY = [
    "zai",
    "openai",
    "minimax",
    "lmstudio",
    "openrouter",
    "anthropic",
    "deepseek",
    # Free-tier providers default to the bottom of the priority order: their
    # rate limits are aggressive, so they shouldn't outrank paid providers on
    # the hot path unless the operator reorders them.
    "groq",
    "gemini",
]

# Auxiliary task kinds — small/cheap helper models that run *outside* the
# primary Brain reasoning path. Compression and recall favor a fast/cheap
# model; skill extraction and post-mortem need stronger reasoning, so they
# default to a Sonnet-class model.
AUXILIARY_TASK_KINDS: tuple[str, ...] = (
    "compression",
    "recall",
    "skill_extraction",
    "post_mortem",
    "approval",
)

_DEFAULT_AUXILIARY_ROUTING: dict[str, dict[str, str | None]] = {
    "compression": {
        "provider": "openrouter",
        "model_id": "openai/gpt-4o-mini",
        "base_url": None,
        "api_key": None,
    },
    "recall": {
        "provider": "openrouter",
        "model_id": "openai/gpt-4o-mini",
        "base_url": None,
        "api_key": None,
    },
    "skill_extraction": {
        "provider": "openrouter",
        "model_id": "anthropic/claude-3-5-sonnet",
        "base_url": None,
        "api_key": None,
    },
    "post_mortem": {
        "provider": "openrouter",
        "model_id": "anthropic/claude-3-5-sonnet",
        "base_url": None,
        "api_key": None,
    },
    "approval": {
        # Smart-approval classifier (Phase 5 / P5-T03): cheap + fast model is
        # appropriate. Output is a tiny JSON blob so even a small model can
        # nail the schema. Hard-coded escalations in smart_approval.py protect
        # against classifier mistakes on high-stakes categories.
        "provider": "openrouter",
        "model_id": "openai/gpt-4o-mini",
        "base_url": None,
        "api_key": None,
    },
}

_DEFAULT_MODEL_ROUTING = {
    "provider_priority": list(_ZAI_PRIMARY_PROVIDER_PRIORITY),
    "default_models": {
        "openai": "gpt-5.2",
        "minimax": "MiniMax-M2.5",
        "lmstudio": "local-model",
        "zai": "glm-5.1",
        "openrouter": "openai/gpt-4o-mini",
        "anthropic": "claude-sonnet-4-6",
        "deepseek": "deepseek-chat",
        "groq": "llama-3.3-70b-versatile",
        # Cheapest Gemini model that still runs the agent tool-loop reliably
        # (~$0.10/$0.40 per 1M tokens, free tier available). Step up to
        # gemini-2.5-flash if strategy quality looks weak.
        "gemini": "gemini-2.5-flash-lite",
    },
    "fallback_chains": {
        "openai": [
            {"provider": "openai", "model_id": "gpt-5.2"},
            {"provider": "minimax", "model_id": "MiniMax-M2.5"},
        ],
        "minimax": [
            {"provider": "minimax", "model_id": "MiniMax-M2.5"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
        "lmstudio": [
            {"provider": "lmstudio", "model_id": "local-model"},
            {"provider": "openai", "model_id": "gpt-5.2"},
            {"provider": "minimax", "model_id": "MiniMax-M2.5"},
        ],
        "zai": [
            {"provider": "zai", "model_id": "glm-5.1"},
            {"provider": "openai", "model_id": "gpt-5.2"},
            {"provider": "minimax", "model_id": "MiniMax-M2.5"},
        ],
        "openrouter": [
            {"provider": "openrouter", "model_id": "openai/gpt-4o-mini"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
        "anthropic": [
            {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
        "deepseek": [
            {"provider": "deepseek", "model_id": "deepseek-chat"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
        "groq": [
            {"provider": "groq", "model_id": "llama-3.3-70b-versatile"},
            # Groq's free tier has a tight per-minute token budget; fall back to
            # Gemini (free, large context) before any paid provider so a request
            # too large for Groq still completes for free.
            {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
        "gemini": [
            {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
            {"provider": "openai", "model_id": "gpt-5.2"},
        ],
    },
    "auxiliary": copy.deepcopy(_DEFAULT_AUXILIARY_ROUTING),
}


def get_model_routing_snapshot() -> dict:
    """Return a validated copy of the persisted model routing policy."""
    return get_model_routing()


def _default_model_routing() -> dict:
    return copy.deepcopy(_DEFAULT_MODEL_ROUTING)


def _zai_env_configured() -> bool:
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ZAI_API_KEY", "ZAI_BASE_URL"):
        if str(os.environ.get(key) or "").strip():
            return True
    return False


def _coerce_provider(value: object) -> str | None:
    provider = str(value or "").strip().lower()
    if provider in _SUPPORTED_PROVIDERS:
        return provider
    return None


def _coerce_model_id(value: object) -> str | None:
    model_id = str(value or "").strip()
    return model_id or None


def _coerce_model_for_provider(provider: str | None, value: object) -> str | None:
    model_id = _coerce_model_id(value)
    if not model_id:
        return None
    provider_key = _coerce_provider(provider)
    if not provider_key:
        return model_id
    return _LEGACY_MODEL_ALIASES.get(provider_key, {}).get(model_id.lower(), model_id)


def _coerce_chain_entry(entry: object) -> tuple[str, str] | None:
    if isinstance(entry, str):
        if ":" not in entry:
            return None
        raw_provider, raw_model = entry.split(":", 1)
    elif isinstance(entry, dict):
        raw_provider = entry.get("provider")
        raw_model = entry.get("model_id")
    else:
        return None

    provider = _coerce_provider(raw_provider)
    model_id = _coerce_model_for_provider(provider, raw_model)
    if not provider or not model_id:
        return None
    return provider, model_id


def _coerce_fallback_chain(raw_chain: object, fallback_to: list[tuple[str, str]]) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    if isinstance(raw_chain, Iterable) and not isinstance(raw_chain, (str, bytes, dict)):
        for entry in raw_chain:
            coerced = _coerce_chain_entry(entry)
            if not coerced:
                continue
            normalized.append(coerced)

    if not normalized:
        normalized = list(fallback_to)

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for provider, model_id in normalized:
        key = (provider, model_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _coerce_model_routing(raw: object) -> dict:
    if not isinstance(raw, dict):
        return _default_model_routing()

    base = _default_model_routing()
    raw_priority = raw.get("provider_priority")
    if isinstance(raw_priority, Iterable) and not isinstance(raw_priority, (str, bytes, dict)):
        priority: list[str] = []
        seen: set[str] = set()
        for provider in raw_priority:
            normalized = _coerce_provider(provider)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            priority.append(normalized)
        base["provider_priority"] = priority or list(_SUPPORTED_PROVIDERS)

    raw_defaults = raw.get("default_models")
    if isinstance(raw_defaults, dict):
        for key, value in raw_defaults.items():
            provider = _coerce_provider(key)
            model_id = _coerce_model_for_provider(provider, value)
            if not provider or not model_id:
                continue
            base["default_models"][provider] = model_id

    raw_chains = raw.get("fallback_chains")
    if isinstance(raw_chains, dict):
        for provider_key, entries in raw_chains.items():
            provider = _coerce_provider(provider_key)
            if not provider:
                continue
            fallback = base["fallback_chains"].get(provider, [])
            base_fallback = _coerce_fallback_chain(entries, fallback)
            base["fallback_chains"][provider] = [
                {"provider": p, "model_id": m}
                for p, m in base_fallback
            ]
    else:
        # Ensure each chain at least has a sane fallback
        for provider, fallback in list(base["fallback_chains"].items()):
            base["fallback_chains"][provider] = [
                {"provider": p, "model_id": m}
                for p, m in _coerce_fallback_chain(fallback, [])
            ]

    raw_auxiliary = raw.get("auxiliary")
    if isinstance(raw_auxiliary, dict):
        for task_kind, entry in raw_auxiliary.items():
            if task_kind not in AUXILIARY_TASK_KINDS:
                continue
            coerced = _coerce_auxiliary_entry(entry)
            if coerced is None:
                # Bad shape — keep the default we already seeded.
                continue
            base["auxiliary"][task_kind] = coerced

    return base


def _coerce_auxiliary_entry(entry: object) -> dict[str, str | None] | None:
    """Validate one ``auxiliary[<task>]`` row. Returns None on bad shape.

    Required: ``provider`` (must be a supported provider) and ``model_id``.
    Optional: ``base_url``, ``api_key`` — pass-through strings or None.
    """
    if not isinstance(entry, dict):
        return None
    provider = _coerce_provider(entry.get("provider"))
    if not provider:
        return None
    model_id = _coerce_model_id(entry.get("model_id"))
    if not model_id:
        return None
    base_url = entry.get("base_url")
    api_key = entry.get("api_key")
    return {
        "provider": provider,
        "model_id": model_id,
        "base_url": str(base_url).strip() or None if base_url is not None else None,
        "api_key": str(api_key).strip() or None if api_key is not None else None,
    }


def _should_migrate_legacy_zai_priority(raw: object, policy: dict) -> bool:
    if not _zai_env_configured():
        return False
    if not isinstance(raw, dict):
        return False

    raw_priority = raw.get("provider_priority")
    if not isinstance(raw_priority, Iterable) or isinstance(raw_priority, (str, bytes, dict)):
        return False

    normalized_priority: list[str] = []
    for provider in raw_priority:
        normalized = _coerce_provider(provider)
        if normalized:
            normalized_priority.append(normalized)

    if normalized_priority != _LEGACY_PROVIDER_PRIORITY:
        return False
    return policy.get("provider_priority") == _LEGACY_PROVIDER_PRIORITY


def get_model_routing() -> dict:
    raw = kv_get(_MODEL_ROUTING_STORAGE_KEY, _default_model_routing())
    policy = _coerce_model_routing(raw)
    if _should_migrate_legacy_zai_priority(raw, policy):
        migrated = copy.deepcopy(policy)
        migrated["provider_priority"] = list(_ZAI_PRIMARY_PROVIDER_PRIORITY)
        kv_set(_MODEL_ROUTING_STORAGE_KEY, migrated)
        return migrated
    return policy


def update_model_routing(raw_policy: object) -> dict:
    policy = _coerce_model_routing(raw_policy)
    kv_set(_MODEL_ROUTING_STORAGE_KEY, policy)
    return policy


def get_default_model_for_provider(provider: str | None) -> str:
    normalized = _coerce_provider(provider) or "openai"
    policy = get_model_routing()
    default = policy["default_models"].get(normalized)
    if default:
        return default
    return _DEFAULT_MODEL_ROUTING["default_models"][normalized]


def get_primary_provider_model() -> tuple[str, str]:
    policy = get_model_routing()
    for provider in policy.get("provider_priority", []) or []:
        model_id = _coerce_model_id(policy.get("default_models", {}).get(provider))
        if model_id:
            return provider, model_id

    for provider in _SUPPORTED_PROVIDERS:
        model_id = _coerce_model_id(policy.get("default_models", {}).get(provider))
        if model_id:
            return provider, model_id

    return "openai", _DEFAULT_MODEL_ROUTING["default_models"]["openai"]


def get_active_config() -> dict[str, str]:
    """Backward-compatible active provider/model view for legacy callers."""
    provider, model = get_primary_provider_model()
    return {"provider": provider, "model": model}


def _provider_has_credentials(provider: str) -> bool:
    """Whether ``provider`` has resolvable credentials (so a call could succeed).

    Mirrors the auth check the actual call performs (same intent as
    ``forven.ai._provider_has_credentials``). Lazy import to avoid a cycle —
    ``forven.ai`` imports this module at load time.
    """
    try:
        from forven.auth.store import get_token

        get_token(provider)
        return True
    except Exception:
        return False


def _degrade_aux_entry_to_credentialed(entry: dict[str, str | None], policy: dict) -> dict[str, str | None]:
    """Divert an auxiliary routing entry to a provider that can actually run.

    The seeded auxiliary defaults route to 'openrouter', but an operator may
    only have keys for e.g. openai/minimax — in that case every auxiliary
    feature (recall re-rank/synthesis, smart-approval classifier, skill
    extraction, post-mortem) would silently die on "no auth profile". When the
    routed provider has no usable credentials AND the entry carries no explicit
    per-task ``api_key``, fall back to the first credentialed provider in the
    configured priority order, using that provider's default model.

    If NOTHING is credentialed, return the entry unchanged so the eventual
    error names the provider the policy asked for. An explicitly configured
    ``api_key`` is always honored — never diverted.
    """
    provider = str(entry.get("provider") or "")
    if entry.get("api_key") or _provider_has_credentials(provider):
        return entry

    candidates: list[str] = list(policy.get("provider_priority") or _SUPPORTED_PROVIDERS)
    default_models = policy.get("default_models") or {}
    for candidate in candidates:
        if candidate == provider:
            continue
        if not _provider_has_credentials(candidate):
            continue
        model_id = _coerce_model_id(default_models.get(candidate)) or (
            _DEFAULT_MODEL_ROUTING["default_models"].get(candidate)
        )
        if not model_id:
            continue
        log.info(
            "auxiliary routing: provider %r has no usable credentials; "
            "falling back to %s/%s",
            provider, candidate, model_id,
        )
        return {"provider": candidate, "model_id": model_id, "base_url": None, "api_key": None}
    return entry


def get_auxiliary_routing(task_kind: str) -> dict[str, str | None]:
    """Return ``{provider, model_id, base_url, api_key}`` for an auxiliary task.

    ``task_kind`` is one of :data:`AUXILIARY_TASK_KINDS`. If the configured
    auxiliary block is missing the requested key (e.g. legacy policy that
    pre-dates this field), fall back to the seeded default for that kind. If
    the kind itself isn't recognized, fall back to the primary provider's
    default model with no overrides.

    Resilience: when the routed provider has no usable credentials (and the
    entry has no explicit ``api_key``), the result is diverted to the first
    credentialed provider in priority order (see
    :func:`_degrade_aux_entry_to_credentialed`) so auxiliary features keep
    running instead of silently failing on every call.
    """
    policy = get_model_routing()
    aux = policy.get("auxiliary") or {}

    entry = aux.get(task_kind)
    if not isinstance(entry, dict) or not entry.get("provider") or not entry.get("model_id"):
        # Try the seeded default for this kind.
        default_entry = _DEFAULT_AUXILIARY_ROUTING.get(task_kind)
        if default_entry:
            resolved = {
                "provider": default_entry["provider"],
                "model_id": default_entry["model_id"],
                "base_url": default_entry.get("base_url"),
                "api_key": default_entry.get("api_key"),
            }
        else:
            # Unknown kind: degrade to provider_priority[0] with its default model.
            priority = policy.get("provider_priority") or list(_SUPPORTED_PROVIDERS)
            provider = (priority[0] if priority else "openai")
            resolved = {
                "provider": provider,
                "model_id": get_default_model_for_provider(provider),
                "base_url": None,
                "api_key": None,
            }
    else:
        resolved = {
            "provider": entry["provider"],
            "model_id": entry["model_id"],
            "base_url": entry.get("base_url"),
            "api_key": entry.get("api_key"),
        }

    return _degrade_aux_entry_to_credentialed(resolved, policy)


def get_fallback_chain(provider: str) -> list[tuple[str, str]]:
    normalized = _coerce_provider(provider) or "openai"
    policy = get_model_routing()
    chain = policy.get("fallback_chains", {}).get(normalized, [])
    resolved = [
        (entry.get("provider", ""), entry.get("model_id", ""))
        for entry in chain
        if _coerce_provider(entry.get("provider")) and _coerce_model_id(entry.get("model_id"))
    ]
    if not resolved:
        return [
            (normalized, get_default_model_for_provider(normalized)),
            ("minimax" if normalized == "openai" else "openai",
             get_default_model_for_provider("minimax" if normalized == "openai" else "openai")),
        ]

    # Ensure cross-provider fallback still exists for primary providers.
    if normalized == "openai":
        minimax = ("minimax", get_default_model_for_provider("minimax"))
        if minimax not in resolved:
            resolved.append(minimax)
    elif normalized == "minimax":
        openai = ("openai", get_default_model_for_provider("openai"))
        if openai not in resolved:
            resolved.append(openai)
    elif normalized == "lmstudio":
        openai = ("openai", get_default_model_for_provider("openai"))
        if openai not in resolved:
            resolved.append(openai)
        minimax = ("minimax", get_default_model_for_provider("minimax"))
        if minimax not in resolved:
            resolved.append(minimax)

    # Dedupe while preserving order.
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in resolved:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)

    return deduped
