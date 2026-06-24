"""Quant Skills Extractor — extracts structured insights from backtest results.

Runs after each backtest to identify patterns, update existing skills,
or stage new hypotheses.  Uses LLM for insight extraction with structured JSON output.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from forven.quant_skills import (
    QuantSkill,
    read_skill,
    store_hypothesis,
    update_skill,
    PROMOTION_THRESHOLD,
)

log = logging.getLogger("forven.quant_skills_extractor")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quant-insight")

# Minimum thresholds for a backtest to be worth analyzing
MIN_TRADES = 10
MIN_FITNESS = -999  # accept all non-error results


EXTRACTION_PROMPT = """\
You are a quantitative research analyst.  Analyze this backtest result and compare
it against existing quant knowledge to extract a structured insight.

## Backtest Result
- Strategy: {strategy_name} ({strategy_type})
- Asset: {asset}
- Regime: {regime}
- Sharpe: {sharpe:.2f}
- Win Rate: {win_rate:.1%}
- Max Drawdown: {max_drawdown:.2%}
- Total Trades: {total_trades}
- Profit Factor: {profit_factor:.2f}
- Parameters: {params}

## Existing Quant Skills (top matches)
{existing_skills_context}

## Instructions
Return ONLY valid JSON with this structure:
{{
  "action": "update_skill" | "new_hypothesis" | "skip",
  "skill_name": "<name of existing skill to update, or null>",
  "pattern": "<short pattern name like 'regime-range-bound-rsi' or 'failure-momentum-high-vol'>",
  "observation": "<one sentence describing what this result teaches us>",
  "what_works": ["<bullet point>", ...],
  "what_doesnt_work": ["<bullet point>", ...]
}}

Rules:
- "update_skill" if this result confirms or refines an existing skill
- "new_hypothesis" if this reveals a genuinely novel pattern not covered by existing skills
- "skip" if this result is unremarkable or too noisy to learn from (e.g., < 20 trades, Sharpe near 0)
- Keep observations concise and specific (include numbers)
- Pattern names must be lowercase with hyphens only
"""


def _parse_insight_json(text: str) -> dict | None:
    """Parse the model's insight JSON, tolerant of fences and surrounding prose.

    The extraction loop was dropping 100% of insights (165/165 one night): the
    model wraps/prefixes its JSON or gets truncated under rate-limit pressure, and
    a bare json.loads raised on every one. Strip markdown fences first; on failure,
    do ONE bounded recovery by slicing the outermost {...} object. No regex
    field-repair and no external deps — a partial dict is still gated downstream by
    the action allowlist in extract_insight.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def extract_insight(
    backtest_result: dict,
    existing_skills: list[QuantSkill],
) -> dict | None:
    """Use LLM to extract a structured insight from a backtest result.

    Returns a dict with keys: action, skill_name, pattern, observation,
    what_works, what_doesnt_work.  Returns None on failure.
    """
    from forven.ai import call_ai_sync
    from forven.model_routing import get_auxiliary_routing

    metrics = backtest_result.get("metrics", backtest_result)
    params = backtest_result.get("params", {})

    # Build existing skills context
    skills_ctx = "None yet." if not existing_skills else ""
    for skill in existing_skills[:5]:
        skills_ctx += f"\n- **{skill.name}** (confidence={skill.confidence:.0%}, n={skill.sample_size}): {skill.description}"

    prompt = EXTRACTION_PROMPT.format(
        strategy_name=backtest_result.get("strategy_name", backtest_result.get("strategy_id", "unknown")),
        strategy_type=backtest_result.get("strategy_type", "unknown"),
        asset=backtest_result.get("asset", "unknown"),
        regime=backtest_result.get("regime", "unknown"),
        sharpe=float(metrics.get("sharpe", 0)),
        win_rate=float(metrics.get("win_rate", 0)),
        max_drawdown=float(metrics.get("max_drawdown_pct", 0)),
        total_trades=int(metrics.get("total_trades", 0)),
        profit_factor=float(metrics.get("profit_factor", 0)),
        params=json.dumps(params, default=str)[:500],
        existing_skills_context=skills_ctx,
    )

    try:
        routing = get_auxiliary_routing("skill_extraction")
        provider = routing.get("provider", "openai")
        model = routing.get("model_id")
        # Execute the configured aux fallback chain; the chokepoint skips any
        # entry that isn't connected+selected. If nothing is callable, the
        # except-block below skips this extraction cleanly.
        route = [(provider, model), *(routing.get("fallbacks") or [])]

        response = call_ai_sync(
            provider=provider,
            model=model,
            prompt=prompt,
            # 512 truncated two free-text bullet lists under load -> unparseable
            # JSON. Larger cap (well under call_ai_sync's 4096 default) so the
            # object closes; combined with tolerant parsing below.
            max_tokens=1024,
            temperature=0.3,
            fallback=False,
            route=route,
        )

        result = _parse_insight_json(response)
        if result is None:
            log.warning("Failed to parse extraction JSON (unrecoverable after fence/brace recovery)")
            return None
        if result.get("action") not in ("update_skill", "new_hypothesis", "skip"):
            log.warning("Invalid action in extraction result: %s", result.get("action"))
            return None
        return result

    except Exception as exc:
        log.warning("Insight extraction failed: %s", exc)
        return None


def maybe_extract(backtest_result: dict) -> None:
    """Lightweight entry point called after backtest completion.

    Checks if the result is worth analyzing, then runs extraction
    in a background thread to avoid blocking.
    """
    metrics = backtest_result.get("metrics", backtest_result)
    total_trades = int(metrics.get("total_trades", 0))

    if total_trades < MIN_TRADES:
        return

    # Submit to background thread
    _executor.submit(_extract_and_store, backtest_result)


def _extract_and_store(backtest_result: dict) -> None:
    """Background worker: extract insight and store it."""
    try:
        from forven.vectordb import search_quant_skills, upsert_quant_skill

        metrics = backtest_result.get("metrics", backtest_result)
        strategy_type = backtest_result.get("strategy_type", "")
        regime = backtest_result.get("regime", "")

        # Find relevant existing skills
        query = f"{strategy_type} {regime}".strip() or "trading strategy"
        chroma_results = search_quant_skills(query, n_results=5)

        # Load full skill objects for matches
        existing_skills: list[QuantSkill] = []
        for r in chroma_results:
            name = r.get("metadata", {}).get("name", "")
            if name:
                skill = read_skill(name)
                if skill:
                    existing_skills.append(skill)

        # Extract insight
        insight = extract_insight(backtest_result, existing_skills)
        if insight is None or insight.get("action") == "skip":
            return

        evidence_entry = {
            "strategy_id": backtest_result.get("strategy_id", ""),
            "strategy_type": strategy_type,
            "asset": backtest_result.get("asset", ""),
            "regime": regime,
            "sharpe": float(metrics.get("sharpe", 0)),
            "win_rate": float(metrics.get("win_rate", 0)),
            "max_drawdown_pct": float(metrics.get("max_drawdown_pct", 0)),
            "total_trades": int(metrics.get("total_trades", 0)),
            "recorded_at": metrics.get("recorded_at", ""),
        }

        if insight["action"] == "update_skill" and insight.get("skill_name"):
            updated = update_skill(
                insight["skill_name"],
                new_evidence=evidence_entry,
                new_observations={
                    "what_works": insight.get("what_works", []),
                    "what_doesnt_work": insight.get("what_doesnt_work", []),
                },
            )
            if updated:
                upsert_quant_skill(updated)
                log.info("Updated skill %s from backtest", updated.name)

        elif insight["action"] == "new_hypothesis":
            pattern = insight.get("pattern", "unknown-pattern")
            observation = insight.get("observation", "")
            backtest_id = backtest_result.get("strategy_id", "unknown")
            h = store_hypothesis(pattern, observation, backtest_id)

            # Auto-promote if threshold reached
            if h.count >= PROMOTION_THRESHOLD:
                from forven.quant_skills import promote_hypothesis
                promoted = promote_hypothesis(h.id)
                if promoted:
                    upsert_quant_skill(promoted)

    except Exception as exc:
        log.warning("Quant insight extraction/storage failed: %s", exc)
