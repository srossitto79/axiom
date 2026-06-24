"""Runtime provider-health signal — reflects what providers actually DID.

Distinct from ``agents/provider_health.py`` (which detects the static
"agent pinned to a provider with no credentials" config drift). This store
records what happened at call time — a provider got rate-limited, quota/spend
exhausted, returned auth errors, or a call silently fell back to another
provider — so the UI can fail LOUDLY and immediately instead of leaving the
operator to discover a degraded provider from server logs.

Written from the runner's error-classification branches (and on success) keyed
by the ACTUAL provider the call used. Read by the provider-health endpoint, the
global ConnectionHealthBanner, and the health-monitor's AI-provider check.
"""

from __future__ import annotations

import time

from forven.db import kv_get, kv_set

_KEY = "forven:provider-health-runtime"

# state: "ok" | "degraded" | "down"
# kind:  "ok" | "rate_limit" | "quota" | "auth" | "transient" | "fallback"
_DOWN_KINDS = {"quota", "auth"}
_DEGRADED_KINDS = {"rate_limit", "transient", "fallback"}


def _load() -> dict:
    store = kv_get(_KEY, {})
    return store if isinstance(store, dict) else {}


def record_provider_event(
    provider: str,
    kind: str,
    message: str = "",
    *,
    fallback_to: str | None = None,
) -> None:
    """Record a runtime health event for ``provider``.

    ``kind`` is classified into a ``state`` (down/degraded/ok). Keep this keyed
    on the provider that actually ran (or was attempted), not an agent's
    configured model string, so the surfaced message names the right provider.
    """
    p = str(provider or "").strip().lower()
    if not p:
        return
    if kind == "ok":
        state = "ok"
    elif kind in _DOWN_KINDS:
        state = "down"
    else:
        state = "degraded"

    # Best-effort observability: this store is now written from the hot call path
    # (the runner tool loop and call_ai), so a backing-store error (e.g. an
    # uninitialised DB in a unit test, or a transient KV failure) must NEVER
    # propagate and break the LLM call that invoked it.
    try:
        now = time.time()
        store = _load()
        prev = store.get(p) or {}
        store[p] = {
            "provider": p,
            "state": state,
            "kind": kind,
            "message": str(message or "")[:300],
            "fallback_to": fallback_to,
            # 'since' marks when the current state began (sticky across same-state events).
            "since": prev.get("since", now) if prev.get("state") == state else now,
            "last_event_at": now,
            "last_ok_at": now if state == "ok" else prev.get("last_ok_at"),
        }
        kv_set(_KEY, store)
    except Exception:  # pragma: no cover — never break a call path on a health write
        pass


def record_provider_ok(provider: str) -> None:
    """Mark a provider healthy again after a successful call."""
    record_provider_event(provider, "ok")


def record_call_failure(provider: str, error: BaseException) -> None:
    """Classify an LLM-call exception and record it, keyed on the provider that ran.

    Shared by the agent/brain tool loop and the simple-completion path (call_ai)
    so EVERY provider failure — not just the agent runner's — lights the
    provider-health surface (banner / Health tab / Discord critical), instead of
    an entire class of Brain/auxiliary failures staying invisible.
    """
    p = str(provider or "").strip().lower()
    if not p:
        return
    kind = "transient"
    try:
        from forven.ai import (
            _is_quota_exhausted,
            _is_rate_limit_exception,
            is_transient_provider_exception,
        )
        from forven.model_selection import UnconfiguredRouteError

        if isinstance(error, UnconfiguredRouteError) or error.__class__.__name__ == "CredentialError":
            kind = "auth"
        elif _is_quota_exhausted(error):
            kind = "quota"
        elif _is_rate_limit_exception(error):
            kind = "rate_limit"
        elif is_transient_provider_exception(error):
            kind = "transient"
    except Exception:  # pragma: no cover — health recording must never break a call path
        pass
    try:
        msg = str(error) or error.__class__.__name__
    except Exception:  # pragma: no cover — pathological __str__ must not escape
        msg = error.__class__.__name__
    record_provider_event(p, kind, msg)


def get_provider_health_runtime() -> list[dict]:
    """All recorded per-provider runtime health entries (most-degraded first)."""
    order = {"down": 0, "degraded": 1, "ok": 2}
    entries = list(_load().values())
    entries.sort(key=lambda e: order.get(str(e.get("state")), 3))
    return entries


def clear_provider_health(provider: str | None = None) -> None:
    """Clear runtime health for one provider (or all)."""
    if provider is None:
        kv_set(_KEY, {})
        return
    store = _load()
    store.pop(str(provider).strip().lower(), None)
    kv_set(_KEY, store)
