"""Strategy extrapolation.

Turns a source artifact (a podcast/YouTube/Reddit/forum snippet where a trader
shares *bits and pieces*) into a STRUCTURED, testable strategy spec — instead of
the old "single under-specified LLM prompt". Every field is tagged
``stated`` (the source literally said it) vs ``inferred`` (we reconstructed it),
with a confidence, plus an explicit assumptions list. Inferred fields can be fed
to ``record_data_gap`` so the test loop knows exactly what was guessed.

The LLM call is injectable (``call_llm``) so the parsing / tagging / validation
scaffolding is unit-testable without a provider.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

# Fields we try to reconstruct into a testable spec.
SPEC_FIELDS = ("indicators", "entry", "exit", "timeframe", "instruments", "params", "regime")
_VALID_BASIS = {"stated", "inferred"}

_SYSTEM = (
    "You are a quantitative strategy reconstructor. A source (podcast/video/post) "
    "describes a trading approach, often only in fragments. Reconstruct the most "
    "likely TESTABLE strategy. Return ONLY a JSON object with keys: "
    "indicators, entry, exit, timeframe, instruments, params, regime — each a "
    "{\"value\": <any>, \"basis\": \"stated\"|\"inferred\", \"confidence\": 0..1} object "
    "(use 'stated' only when the source literally says it; otherwise 'inferred') — "
    "plus 'assumptions' (list of strings: the guesses you made) and 'claimed_edge' "
    "(one sentence: what the source claims works). If a field is unknown, still "
    "provide your best inferred value with low confidence."
)


def _default_call_llm(prompt: str) -> str:
    from forven.ai import call_ai_sync, resolve_available_provider

    return call_ai_sync(
        provider=resolve_available_provider(),
        prompt=prompt,
        max_tokens=1024,
        temperature=0.2,
        system=_SYSTEM,
        fallback=False,
    )


def _extract_json(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _normalize_field(raw: Any) -> dict[str, Any]:
    """Coerce a field into {value, basis, confidence}. Unknown shapes -> inferred/low."""
    if isinstance(raw, dict) and "value" in raw:
        basis = str(raw.get("basis") or "inferred").strip().lower()
        if basis not in _VALID_BASIS:
            basis = "inferred"
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.3
        confidence = max(0.0, min(1.0, confidence))
        return {"value": raw.get("value"), "basis": basis, "confidence": confidence}
    # A bare value (the LLM didn't tag it) -> treat as inferred, low confidence.
    return {"value": raw, "basis": "inferred", "confidence": 0.3}


def extrapolate_strategy_spec(
    artifact_text: str,
    *,
    title: str | None = None,
    call_llm: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Reconstruct a structured, tagged StrategySpec from source text.

    Returns {ok, spec?, inferred_fields?, assumptions?, claimed_edge?, error_code?}.
    Never raises.
    """
    text = str(artifact_text or "").strip()
    if not text:
        return {"ok": False, "error_code": "empty_artifact"}

    caller = call_llm or _default_call_llm
    prompt = (
        f"# Source{f' — {title}' if title else ''}\n{text[:8000]}\n\n"
        "Reconstruct the testable strategy as the JSON object described in the system prompt."
    )
    try:
        raw = caller(prompt)
    except Exception as exc:  # pragma: no cover — provider failure
        log.warning("extrapolation LLM call failed: %s", exc)
        return {"ok": False, "error_code": "llm_unavailable", "error": str(exc)}

    try:
        parsed = json.loads(_extract_json(raw))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error_code": "parse_failed", "error": str(exc), "raw": raw}
    if not isinstance(parsed, dict):
        return {"ok": False, "error_code": "parse_failed", "raw": raw}

    spec: dict[str, dict[str, Any]] = {}
    for field in SPEC_FIELDS:
        if field in parsed:
            spec[field] = _normalize_field(parsed[field])

    assumptions = parsed.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = []
    inferred_fields = sorted(name for name, meta in spec.items() if meta["basis"] == "inferred")

    return {
        "ok": True,
        "spec": spec,
        "inferred_fields": inferred_fields,
        "assumptions": [str(a) for a in assumptions],
        "claimed_edge": str(parsed.get("claimed_edge") or "").strip(),
    }


def record_extrapolation_gaps(
    hypothesis_id: str,
    extrapolation: dict[str, Any],
    *,
    confidence_floor: float = 0.5,
) -> list[str]:
    """Record low-confidence inferred fields as data gaps so the test loop knows
    what was guessed. Returns the field names recorded. Best-effort."""
    if not extrapolation.get("ok"):
        return []
    spec = extrapolation.get("spec") or {}
    recorded: list[str] = []
    try:
        from forven.hypotheses import record_data_gap
    except Exception:
        return []
    for field, meta in spec.items():
        if meta.get("basis") != "inferred" or float(meta.get("confidence") or 0.0) >= confidence_floor:
            continue
        try:
            record_data_gap(
                title=f"Confirm inferred '{field}' for {hypothesis_id}",
                category="extrapolation",
                missing_dataset=f"reconstructed:{field}",
                linked_hypothesis_id=hypothesis_id,
                missing_fields=[field],
                why_it_matters=(
                    f"'{field}' was inferred (not stated by the source) at "
                    f"{meta.get('confidence')} confidence — confirm before trusting the test."
                ),
                requested_by_model="extrapolation",
            )
            recorded.append(field)
        except Exception:
            log.debug("could not record extrapolation gap for %s.%s", hypothesis_id, field, exc_info=True)
    return recorded
