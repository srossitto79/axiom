"""Natural-language -> rule_engine spec generation.

Turns a plain-English strategy description into a declarative rule spec that the
no-code engine can execute, using the project's shared async LLM client
(:func:`forven.ai.call_ai`). The model is constrained — via a system prompt built
from the live indicator registry — to only reference indicators, operators and
columns the engine actually supports, and the result is always validated with
``validate_rule_spec`` before being returned, so a draft accepted here cannot
explode at backtest time.

Degrades gracefully: if no LLM provider is configured the endpoint returns a
clear, actionable error rather than raising.
"""
from __future__ import annotations

import json
import re

from forven.strategies import indicators as _indicators
from forven.strategies.builtin.rule_engine import (
    _OPERATORS,
    _RAW_COLUMNS,
    validate_rule_spec,
)


def _build_system_prompt() -> str:
    lines: list[str] = []
    for meta in _indicators.metadata():
        param_parts: list[str] = []
        for p in meta["params"]:
            pmin = p.get("min")
            pmax = p.get("max")
            pdef = p.get("default")
            if pmin is not None and pmax is not None:
                # Show as int when the value is whole, float otherwise (e.g. BB std dev)
                fmt = lambda v: str(int(v)) if float(v) == int(float(v)) else str(round(float(v), 3))
                param_parts.append(f"{p['key']} [{fmt(pmin)}–{fmt(pmax)}, default {fmt(pdef)}]")
            else:
                param_parts.append(p["key"])
        params = ", ".join(param_parts) or "none"
        suffixes = meta["output_suffixes"]
        out_desc = ", ".join((f"<id>{s}" if s else "<id>") for s in suffixes)
        lines.append(
            f"- {meta['kind']} [{meta['category']}] params: {params}; series: {out_desc} — {meta['description']}"
        )
    indicator_catalog = "\n".join(lines)
    operators = ", ".join(sorted(_OPERATORS))
    raw_cols = ", ".join(sorted(_RAW_COLUMNS))

    return f"""You are a quantitative trading assistant. Convert the user's plain-English \
strategy idea into a STRICT JSON rule spec for an automated backtest engine.

Output ONLY the JSON object — no prose, no markdown fences.

JSON shape:
{{
  "indicators": [{{"id": "rsi", "kind": "rsi", "params": {{"length": 14}}}}],
  "params": {{"oversold": 30, "overbought": 70}},
  "entry_long":  {{"logic": "and", "conditions": [
      {{"left": "rsi", "op": "<", "right": {{"param": "oversold"}}}},
      {{"left": "close", "op": ">", "right": "ema"}}]}},
  "exit_long":   {{"logic": "or",  "conditions": [
      {{"left": "rsi", "op": ">", "right": {{"param": "overbought"}}}}]}},
  "entry_short": null,
  "exit_short":  null
}}

Rules:
- Use ONLY these indicator kinds (give each instance a short lowercase id, then \
reference its output series by that id plus the listed suffix). \
Each param shows its allowed range [min–max] and default — STAY WITHIN THESE RANGES:
{indicator_catalog}
- Reference an indicator output, or a raw data column, as a bare string. Raw columns: {raw_cols}.
- A {{"param": "name"}} operand reads an editable knob from "params"; a bare number is a constant.
- Operators allowed: {operators}. Use crosses_above/crosses_below for crossovers.
- Provide at least one entry side (entry_long or entry_short). Set unused sides to null.
- Prefer putting tunable thresholds (e.g. 30, 70) into "params" and referencing them, \
so the user can adjust them later.
- CRITICAL: All period/length/lookback params must be ≤ 200. The backtest engine \
requires more bars than the longest lookback, so 210+ period indicators will fail at runtime.
- Keep ids and column names lowercase. Do NOT invent indicator kinds or operators."""


def _extract_json(text: str) -> dict:
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start != -1 and end != -1 and end > start:
        body = body[start : end + 1]
    return json.loads(body)


def _normalize_spec(obj: object) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("AI response was not a JSON object")
    return {
        "indicators": obj.get("indicators") if isinstance(obj.get("indicators"), list) else [],
        "params": obj.get("params") if isinstance(obj.get("params"), dict) else {},
        "entry_long": obj.get("entry_long") or None,
        "exit_long": obj.get("exit_long") or None,
        "entry_short": obj.get("entry_short") or None,
        "exit_short": obj.get("exit_short") or None,
    }


async def nl_to_rule_spec(*, description: str, symbol: str = "BTC", timeframe: str = "1h") -> dict:
    """Return ``{valid, spec, errors, warnings, provider}``.

    ``spec`` is a normalized rule-engine spec (may still carry validation
    ``errors`` for the UI to surface); ``valid`` is True only when
    ``validate_rule_spec`` passes.
    """
    from forven import ai

    description = str(description or "").strip()
    if not description:
        return {"valid": False, "spec": None, "errors": ["Describe a strategy first."], "warnings": [], "provider": None}

    provider = ai.resolve_available_provider()
    try:
        has_creds = ai._provider_has_credentials(provider)
    except Exception:
        has_creds = False
    if not has_creds:
        return {
            "valid": False, "spec": None,
            "errors": ["No AI provider is configured. Add an LLM API key in Settings to use the natural-language builder."],
            "warnings": [], "provider": None,
        }

    system = _build_system_prompt()
    user = (
        f"Asset: {symbol}. Timeframe: {timeframe}.\n"
        f"Strategy idea:\n{description}\n\n"
        "Return ONLY the JSON rule spec."
    )
    try:
        raw = await ai.call_ai(provider, prompt=user, system=system, max_tokens=1600, temperature=0.1)
    except Exception as exc:  # noqa: BLE001 — surface a friendly message to the UI
        return {"valid": False, "spec": None, "errors": [f"AI request failed: {exc}"], "warnings": [], "provider": provider}

    try:
        spec = _normalize_spec(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001
        return {
            "valid": False, "spec": None,
            "errors": [f"Could not parse a rule spec from the AI response: {exc}"],
            "warnings": [], "provider": provider, "raw": str(raw)[:2000],
        }

    errors = validate_rule_spec(spec)
    return {"valid": len(errors) == 0, "spec": spec, "errors": errors, "warnings": [], "provider": provider}


__all__ = ["nl_to_rule_spec"]
