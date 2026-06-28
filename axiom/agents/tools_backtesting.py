"""Backtesting and code execution tool handlers."""

import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from axiom.db import get_db
from axiom.verdict_engine import build_strategy_verdict_blob

from .context import _current_agent_id_var, _current_strategy_id_var, _current_task_display_id_var
from .tool_registry import register_tool

log = logging.getLogger("axiom.agents.runner")



def _get_hypothesis_tf(hypothesis_id: str) -> str | None:
    if not hypothesis_id:
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT target_timeframes FROM hypotheses WHERE id = ?",
                (hypothesis_id,),
            ).fetchone()
        if row:
            raw = row[0]
            tfs = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(tfs, list):
                for tf in tfs:
                    cleaned = str(tf or "").strip().lower()
                    if cleaned and cleaned != "unspecified":
                        return cleaned
    except Exception:
        pass
    return None


def _with_hypothesis_timeframe(params: object, hypothesis_id: str | None) -> object:
    """Carry a hypothesis timeframe into custom strategy intake defaults."""
    if not isinstance(params, dict):
        return params
    if str(params.get("_timeframe") or "").strip():
        return params
    timeframe = _get_hypothesis_tf(str(hypothesis_id or "").strip())
    if not timeframe:
        return params
    enriched = dict(params)
    enriched["_timeframe"] = timeframe
    return enriched


def _get_strategy_or_hypothesis_tf(strategy_id: str | None) -> str | None:
    normalized = str(strategy_id or "").strip()
    if not normalized:
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT timeframe, hypothesis_id FROM strategies WHERE id = ?",
                (normalized,),
            ).fetchone()
        if not row:
            return None
        hypothesis_tf = _get_hypothesis_tf(str(row["hypothesis_id"] or "").strip())
        if hypothesis_tf:
            return hypothesis_tf
        strategy_tf = str(row["timeframe"] or "").strip().lower()
        return strategy_tf or None
    except Exception:
        return None


def _parse_json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _format_strategy_validation_failure(result: dict, original_code: str) -> str:
    execution = result.get("execution_result") if isinstance(result, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    lint_issues = result.get("lint_issues") if isinstance(result, dict) else []
    lint_issues = lint_issues if isinstance(lint_issues, list) else []
    stdout = str(execution.get("stdout") or "").strip()
    stderr = str(execution.get("stderr") or "").strip()
    returncode = execution.get("returncode")
    timed_out = bool(execution.get("timed_out"))

    lines = ["Validation failed:"]
    if lint_issues:
        lines.append(f"Lint issues: {'; '.join(str(item) for item in lint_issues[:5])}")
    if returncode not in (None, 0):
        lines.append(f"Exit code: {returncode}")
    if timed_out:
        lines.append("Execution timed out.")
    if stdout:
        lines.append(f"Harness output: {stdout[:1200]}")
    if stderr:
        lines.append(f"Error: {stderr[:1200]}")
    if result.get("code") and result["code"] != original_code:
        lines.append("Auto-fixed/normalized code is available. Try again with the corrected version.")
    if len(lines) == 1:
        lines.append("No validation details were returned by the sandbox.")
    return "\n".join(lines)


def _current_candidate_provenance(crucible_id: str) -> dict[str, str | None]:
    agent_id = str(_current_agent_id_var.get() or "").strip()
    task_display_id = str(_current_task_display_id_var.get() or "").strip()
    if not agent_id:
        return {
            "origin_crucible_id": None,
            "origin_agent_id": None,
            "origin_task_id": None,
            "origin_model": None,
        }
    origin_model = None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT model, model_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
        if row:
            origin_model = str(row["model_id"] or row["model"] or "").strip() or None
    except Exception:
        origin_model = None
    return {
        "origin_crucible_id": str(crucible_id or "").strip() or None,
        "origin_agent_id": agent_id,
        "origin_task_id": task_display_id or None,
        "origin_model": origin_model,
    }


def _persist_strategy_provenance(strategy_id: str, provenance: dict[str, str | None]) -> None:
    normalized_strategy_id = str(strategy_id or "").strip()
    if not normalized_strategy_id or not provenance.get("origin_agent_id"):
        return
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET origin_crucible_id = ?,
                origin_agent_id = ?,
                origin_task_id = ?,
                origin_model = ?
            WHERE id = ?
            """,
            (
                provenance.get("origin_crucible_id"),
                provenance.get("origin_agent_id"),
                provenance.get("origin_task_id"),
                provenance.get("origin_model"),
                normalized_strategy_id,
            ),
        )


def _load_strategy_context(strategy_id: str) -> tuple[dict, dict]:
    from axiom.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, type, symbol, timeframe, metrics, verdict FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        return {}, {}
    payload = dict(row)
    return payload, _parse_json_object(payload.get("metrics"))


def _persist_agent_backtest(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    timeframe: str,
    params: dict,
    result: dict,
    fitness: float,
) -> tuple[bool, str | None, list[str]]:
    substep_warnings: list[str] = []
    strategy_row, merged_metrics = _load_strategy_context(strategy_id)
    if not strategy_row:
        return False, None, [
            f"strategy container '{strategy_id}' not found — backtest results were "
            "NOT persisted (no promotion, no later retrieval). Pass the strategy_id "
            "returned by create_custom_strategy/register_strategy."
        ]

    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    merged_metrics.update(metrics)
    merged_metrics["fitness"] = float(fitness)

    now_iso = datetime.now(timezone.utc).isoformat()
    job_id = f"agent_bt_{uuid4().hex[:12]}"
    result_id = f"{strategy_id}-{str(asset or '').lower()}-{int(time.time() * 1000)}"
    symbol = str(asset or strategy_row.get("symbol") or "").strip().upper()
    resolved_timeframe = str(timeframe or strategy_row.get("timeframe") or "1h").strip() or "1h"
    strategy_name = str(strategy_row.get("name") or strategy_id).strip() or strategy_id
    config_payload = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "strategy": strategy_id,
        "symbol": symbol,
        "timeframe": resolved_timeframe,
        "params": params if isinstance(params, dict) else {},
        "job_id": job_id,
        "source": "agent_tool",
        "tool": "run_backtest",
    }

    from axiom.api_core import _persist_backtest_result_row, _write_backtest_result_artifacts
    from axiom.strategies.backtest import _sync_strategy_metrics_and_promote_if_eligible

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="backtest",
        symbol=symbol,
        timeframe=resolved_timeframe,
        start_date=str(result.get("start_date") or "").strip() or None,
        end_date=str(result.get("end_date") or "").strip() or None,
        metrics=merged_metrics,
        config=config_payload,
        created_at=now_iso,
    )

    try:
        from axiom.vectordb import store_backtest_result

        store_backtest_result(
            strategy_id=strategy_id,
            asset=symbol,
            strategy_type=str(strategy_type or strategy_row.get("type") or "").strip(),
            params=params if isinstance(params, dict) else {},
            metrics=merged_metrics,
            fitness=float(fitness),
            result_id=result_id,
            job_id=job_id,
            strategy_name=strategy_name,
            lifecycle_strategy_id=strategy_id,
            config=config_payload,
            result_type="backtest",
        )
    except Exception as exc:
        log.warning("Vector store of agent backtest failed for %s: %s", strategy_id, exc)
        substep_warnings.append(f"semantic-memory (vectordb) store failed: {exc}")

    try:
        _write_backtest_result_artifacts(
            result_id, job_id, result.get("trades"),
            equity_curve=result.get("equity_curve"),
            benchmark_curve=result.get("benchmark_curve"),
        )
    except Exception as exc:
        log.warning("Artifact write for agent backtest failed for %s: %s", result_id, exc)
        substep_warnings.append(f"trade/equity artifact write failed: {exc}")

    _sync_strategy_metrics_and_promote_if_eligible(
        strategy_id,
        merged_metrics,
        promotion_reason="Agent backtest completed",
    )

    # Guardrail #2: Verify ChromaDB persistence
    from axiom.db import verify_chroma_persistence
    persisted, error_msg = verify_chroma_persistence(result_id)
    if not persisted:
        log.warning(f"ChromaDB persistence check failed: {error_msg}")
        substep_warnings.append(f"ChromaDB persistence verification failed: {error_msg}")

    # The primary row write succeeded (it raises otherwise, caught by the caller).
    # substep_warnings carry any non-fatal degradations so the agent knows the
    # result is persisted but, e.g., not yet searchable in semantic memory.
    return True, result_id, substep_warnings


def _persist_agent_verdict(strategy_id: str, verdict_result: dict) -> bool:
    strategy_row, metrics = _load_strategy_context(strategy_id)
    if not strategy_row:
        return False

    raw_tests = verdict_result.get("tests")
    if not isinstance(raw_tests, dict):
        return False

    verdict_tests = _parse_json_object(metrics.get("verdict_tests"))
    normalized_tests, verdict_blob = build_strategy_verdict_blob(verdict_result)
    merged_tests = dict(verdict_tests)
    merged_tests.update(normalized_tests)
    metrics["verdict_tests"] = merged_tests
    verdict_blob["tests"] = merged_tests
    updated_at = datetime.now(timezone.utc).isoformat()

    from axiom.db import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ?, verdict = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(metrics),
                json.dumps(verdict_blob),
                updated_at,
                strategy_id,
            ),
        )
    return True


@register_tool(
    name="run_backtest",
    description=(
        "Run a strategy backtest. Any strategy family and params are accepted — composite strategies "
        "mixing multiple indicator families work seamlessly. Returns trades and "
        "metrics (Sharpe, win rate, profit factor, max drawdown, fitness score)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "asset": {"type": "string", "description": "Coin symbol: BTC, ETH, SOL, or other valid dataset symbol"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "strategy_type": {
                "type": "string",
                "description": (
                    "Strategy family name — any pre-built or novel composite family. "
                    "Composite strategies mixing indicators are encouraged."
                ),
            },
            "params": {"type": "object", "description": "Strategy parameters dict — any params your strategy needs"},
            "bars": {"type": "integer", "description": "Number of bars to backtest against (default 8760 = 365 days of 1h). ALWAYS use at least 8760 bars (1 year) for reliable results."},
            "strategy_id": {"type": "string", "description": "Strategy container ID returned by create_custom_strategy or register_strategy. Required to persist results and trigger promotion. If omitted, falls back to task context."},
        },
        "required": ["asset", "strategy_type", "params"],
    },
)
def _tool_run_backtest(params: dict) -> str:
    """Run a strategy backtest."""
    try:
        from axiom.strategies.backtest import backtest_strategy
        from axiom.strategies.fitness import score_strategy

        if not isinstance(params, dict):
            return "Backtest error: invalid parameters payload"

        asset = params.get("asset")
        strategy_type = params.get("strategy_type")
        backtest_params = params.get("params")
        if not asset or not strategy_type or not isinstance(backtest_params, dict):
            return "Backtest error: asset, strategy_type, and params are required"

        # Prefer an explicit strategy_id (e.g. from create_custom_strategy), then
        # fall back to the task-context var, then agent id.
        sid = str(params.get("strategy_id") or "").strip() or _current_strategy_id_var.get()
        if not sid:
            sid = _current_agent_id_var.get() or "agent-backtest"

        result = backtest_strategy(
            strategy_id=sid,
            asset=asset,
            strategy_type=strategy_type,
            params=backtest_params,
            bars=params.get("bars"),
            timeframe=params.get("timeframe", "1h"),
            persist_legacy_run=False,
            regime_gate=False,
        )

        if result.get("error"):
            return f"Backtest error: {result['error']}"

        metrics = result.get("metrics", {})
        fitness = score_strategy(metrics)
        persisted = False
        result_id = None
        persist_error: str | None = None
        persist_warnings: list[str] = []
        try:
            persisted, result_id, persist_warnings = _persist_agent_backtest(
                strategy_id=str(sid),
                asset=str(asset),
                strategy_type=str(strategy_type),
                timeframe=str(params.get("timeframe", "1h")),
                params=backtest_params,
                result=result,
                fitness=fitness,
            )
            # A clean run that simply couldn't find its container still carries a
            # reason in persist_warnings — promote it to the persist_error slot so
            # the agent sees WHY nothing was saved.
            if not persisted and persist_warnings:
                persist_error = "; ".join(persist_warnings)
        except Exception as exc:
            log.exception("Agent backtest persistence failed for %s", sid)
            persist_error = f"{type(exc).__name__}: {exc}"

        payload = {
            "result_id": result_id,
            "persisted": persisted,
            "total_trades": metrics.get("total_trades", 0),
            "win_rate": f"{metrics.get('win_rate', 0):.1%}",
            "sharpe": metrics.get("sharpe", 0),
            "profit_factor": metrics.get("profit_factor", 0),
            "max_drawdown": f"{metrics.get('max_drawdown_pct', 0):.2%}",
            "total_return": f"{metrics.get('total_return_pct', 0):.2%}",
            "fitness": fitness,
            "avg_bars_held": metrics.get("avg_bars_held", 0),
        }
        # Surface data-load / enrichment degradations so the agent doesn't read a
        # 0-trade run as a logic bug when the real cause was an upstream data
        # failure, plus any non-fatal persistence sub-step failures (e.g. the
        # result saved but isn't searchable in semantic memory yet).
        all_warnings: list[str] = []
        data_warnings = result.get("warnings") if isinstance(result, dict) else None
        if data_warnings:
            all_warnings.extend(str(w) for w in data_warnings)
        if persisted and persist_warnings:
            all_warnings.extend(str(w) for w in persist_warnings)
        if all_warnings:
            payload["warnings"] = all_warnings
        if not persisted:
            payload["persist_error"] = (
                persist_error
                or "Results were NOT persisted — they will not be available for "
                "promotion or later retrieval. Check strategy_id is a valid container."
            )
        return json.dumps(payload, indent=2)
    except Exception as e:
        return f"Backtest failed: {e}"

@register_tool(
    name="run_code",
    description="Execute Python code in a sandboxed subprocess with resource limits (30s CPU, 512MB RAM). Use for testing strategy logic or data analysis. No network access.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute"},
        },
        "required": ["code"],
    },
)
def _tool_run_code(code: str) -> str:
    """Execute Python code in sandbox with self-healing validation.

    If the code looks like a strategy class, run it through the self-healer first
    (lint + auto-fix + test harness). Otherwise, execute directly in sandbox.
    """
    # Check if this looks like strategy code
    is_strategy_code = "BaseStrategy" in code or "generate_signal" in code
    selfheal_fallback_note = ""

    if is_strategy_code:
        try:
            from axiom.selfheal import validate_strategy_code
            validation = validate_strategy_code(code)
            if validation["valid"]:
                return f"Strategy code validated successfully.\n{validation['execution_result']['stdout']}"
            else:
                output = "Strategy validation FAILED:\n"
                if validation["lint_issues"]:
                    output += f"Lint issues: {'; '.join(validation['lint_issues'][:5])}\n"
                exec_r = validation["execution_result"]
                if exec_r["stderr"]:
                    output += f"Error: {exec_r['stderr'][:500]}\n"
                if validation["code"] != code:
                    output += "\nAuto-fixed code available (lint issues resolved)."
                return output
        except Exception as e:
            # Don't silently downgrade strategy validation to a plain exec — the
            # agent must know its code was NOT validated against the strategy
            # harness (signal API, vectorized path, AST guard), otherwise it reads
            # a passing direct-exec as a passing validation.
            log.warning("Self-heal validation unavailable, falling back to direct execution: %s", e)
            selfheal_fallback_note = (
                f"\n\n[WARNING] Strategy self-heal validation could not run "
                f"({type(e).__name__}: {e}). The code below was executed directly "
                "WITHOUT strategy validation — passing output here does NOT mean it "
                "passes registration. Re-run register_strategy/create_custom_strategy "
                "to validate properly."
            )

    # Direct sandbox execution
    from axiom.sandbox import run_code
    result = run_code(code)
    output = result["stdout"]
    if result["stderr"]:
        output += f"\nSTDERR: {result['stderr']}"
    if result["timed_out"]:
        output += "\n(TIMED OUT)"
    if result["returncode"] != 0:
        output += f"\nExit code: {result['returncode']}"
    output = output or "(no output)"
    return output + selfheal_fallback_note

@register_tool(
    name="register_strategy",
    description=(
        "Validate and register a custom strategy type. Two modes:\n"
        "  • NEW: provide 'code' + 'type_name' — writes the module to custom/ then registers it.\n"
        "  • EXISTING: provide 'module_name' — registers a file already in custom/ WITHOUT overwriting it. "
        "Use this when the operator has already written and certified the file.\n"
        "Code must extend BaseStrategy, export STRATEGY_CLASS and TYPE_NAME, and implement "
        "generate_signal(df) returning a scalar Signal. Agent-generated strategies must include "
        "hypothesis_id so the container is registered against its parent hypothesis.\n"
        "SIGNAL API: Signal is a dataclass — Signal(entry_signal=bool, exit_signal=bool, "
        "price=float, direction='long'|'short'). Sentinels: Signal.LONG/SHORT/HOLD/EXIT. "
        "No SignalType enum, no self.position state (generate_signal is stateless), use "
        "'direction' not 'side'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Full Python source code of the strategy module (new strategies only). Must import and extend BaseStrategy, implement generate_signal(df) returning a scalar Signal, and export STRATEGY_CLASS and TYPE_NAME."},
            "type_name": {"type": "string", "description": "Unique type name (e.g., 'fisher_momentum'). Required when providing 'code'. Alphanumeric and underscores only."},
            "module_name": {"type": "string", "description": "Name of an existing .py file in custom/ (without the .py extension). Use when the operator's file is already in place — the file is NOT overwritten."},
            "hypothesis_id": {"type": "string", "description": "Parent hypothesis ID for the strategy container that will be registered from this module."},
            "crucible_id": {"type": "string", "description": "Planner-approved crucible/hypothesis ID for this candidate."},
        },
        "required": ["hypothesis_id"],
    },
    permissions={"role:strategy-developer", None},
)
def _tool_register_strategy(params: dict) -> str:
    """Validate, save to custom/ directory (if new), and register a strategy type."""
    from axiom.crucible_tasks import validate_candidate_strategy_creation

    code = str(params.get("code") or "").strip()
    type_name = str(params.get("type_name") or "").strip()
    module_name = str(params.get("module_name") or "").strip()
    crucible_id = str(params.get("crucible_id") or params.get("hypothesis_id") or "").strip()
    hypothesis_id = str(params.get("hypothesis_id") or crucible_id).strip()

    if not hypothesis_id:
        return "Error: 'hypothesis_id' is required"

    using_existing_file = bool(module_name) and not code

    if not using_existing_file:
        if not code or not type_name:
            return "Error: provide either 'module_name' (existing file) or both 'code' and 'type_name' (new file)"
        if not type_name.replace("_", "").isalnum():
            return "Error: type_name must be alphanumeric with underscores only"

    validation = validate_candidate_strategy_creation(
        crucible_id,
        str(_current_agent_id_var.get() or "").strip(),
        str(_current_task_display_id_var.get() or "").strip(),
        hypothesis_id,
    )
    if not validation.allowed:
        return f"Error: {validation.reason}"
    crucible_id = str(validation.crucible_id or crucible_id).strip()
    hypothesis_id = str(validation.hypothesis_id or hypothesis_id).strip()
    provenance = _current_candidate_provenance(crucible_id)

    import os
    custom_dir = os.path.join(os.path.dirname(__file__), "..", "strategies", "custom")

    if using_existing_file:
        # Register an existing operator-written file without overwriting it.
        filepath = os.path.join(custom_dir, f"{module_name}.py")
        if not os.path.exists(filepath):
            return f"Error: module '{module_name}' not found at {filepath}"
        try:
            from axiom.strategies.registry import reset, discover, _TYPE_MAP
            from axiom.strategies.intake import register_custom_strategy_file
            reset()

            registration = register_custom_strategy_file(
                file_path=filepath,
                source="agent_register",
                hypothesis_id=hypothesis_id,
                default_params_override=_with_hypothesis_timeframe({}, hypothesis_id),
                origin_task_id=provenance.get("origin_task_id"),
            )
            registered_type_name = str(registration.get("type_name") or module_name).strip()
            discover()
            if registered_type_name not in _TYPE_MAP:
                return (
                    f"Warning: file registered but type '{registered_type_name}' not found in "
                    f"registry. Ensure the module exports TYPE_NAME and STRATEGY_CLASS."
                )

            registered_strategy_id = str(registration.get("strategy_id") or "").strip()
            current_strategy_id = str(_current_strategy_id_var.get() or "").strip()
            target_strategy_id = registered_strategy_id or current_strategy_id
            if target_strategy_id:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE strategies SET runtime_type = ?, updated_at = ? WHERE id = ?",
                        (registered_type_name, datetime.now(timezone.utc).isoformat(), target_strategy_id),
                    )
                _persist_strategy_provenance(target_strategy_id, provenance)
            if registered_strategy_id:
                return (
                    f"Existing strategy file '{module_name}.py' registered successfully as "
                    f"{registered_strategy_id} (type='{registered_type_name}') for hypothesis {hypothesis_id}."
                )
            return (
                f"Existing strategy file '{module_name}.py' registered (type='{registered_type_name}') "
                f"for hypothesis {hypothesis_id}, but no strategy container id was returned."
            )
        except Exception as e:
            return f"Registration of existing file failed: {e}"

    # --- New-file path: write code then register ---

    # Validate strategy code via self-healer (lint + sandbox test harness)
    try:
        from axiom.selfheal import validate_strategy_code
        result = validate_strategy_code(code)
        if not result["valid"]:
            return _format_strategy_validation_failure(result, code)
        # Use the (possibly auto-fixed) code
        final_code = result.get("code") or code
    except Exception as e:
        return f"Validation error: {e}"

    # Save to custom/ directory
    os.makedirs(custom_dir, exist_ok=True)

    # Ensure __init__.py exists
    init_path = os.path.join(custom_dir, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w", encoding="utf-8") as f:
            f.write('"""Custom strategies — agent-generated modules."""\n')

    filepath = os.path.join(custom_dir, f"{type_name}.py")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(final_code)

    # Targeted intake must import/register the just-written file before a full
    # custom discovery pass. If discovery sees it first, TYPE_NAME is already in
    # the runtime map and targeted DB registration rejects it as a duplicate.
    try:
        from axiom.strategies.registry import reset, discover, _TYPE_MAP
        from axiom.strategies.intake import register_custom_strategy_file
        reset()

        registration = register_custom_strategy_file(
            file_path=filepath,
            source="agent_register",
            hypothesis_id=hypothesis_id,
            # Write the origin task atomically with the strategy row so a crash
            # between creation and the _persist_strategy_provenance backfill below
            # can't orphan the develop_candidate task from its strategy.
            default_params_override=_with_hypothesis_timeframe({}, hypothesis_id),
            origin_task_id=provenance.get("origin_task_id"),
        )
        discover()
        if type_name not in _TYPE_MAP:
            return f"Warning: file saved to {filepath} but type '{type_name}' not found in registry. Ensure the module exports TYPE_NAME = '{type_name}' and STRATEGY_CLASS."

        registered_strategy_id = str(registration.get("strategy_id") or "").strip()
        current_strategy_id = str(_current_strategy_id_var.get() or "").strip()
        target_strategy_id = registered_strategy_id or current_strategy_id
        if target_strategy_id:
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE strategies
                    SET runtime_type = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (type_name, datetime.now(timezone.utc).isoformat(), target_strategy_id),
                )
            _persist_strategy_provenance(target_strategy_id, provenance)
        if registered_strategy_id:
            return (
                f"Strategy type '{type_name}' registered successfully as "
                f"{registered_strategy_id} for hypothesis {hypothesis_id}."
            )
        return (
            f"Strategy type '{type_name}' registered successfully for hypothesis {hypothesis_id}, "
            "but no strategy container id was returned."
        )
    except Exception as e:
        return f"File saved but registry reload failed: {e}. The strategy may still work on next restart."


@register_tool(
    name="create_custom_strategy",
    description=(
        "Write a custom BaseStrategy Python module, validate it via the self-healing "
        "sandbox, register it in the strategy type map, and return the strategy_id. "
        "Use this when the hypothesis mechanism uses enrichment columns "
        "(liq_short_volume, l2_imbalance_*, ls_ratio, open_interest, funding_rate, etc.) "
        "or needs non-standard logic that no built-in template family supports.\n\n"
        "The code MUST:\n"
        "  • subclass BaseStrategy from axiom.strategies.base\n"
        "  • export TYPE_NAME = '<type_name>' and STRATEGY_CLASS = <ClassName>\n"
        "  • implement generate_signal(self, df) returning a scalar Signal or dict\n"
        "  • guard optional enrichment columns with: if 'col' in df.columns:\n\n"
        "SIGNAL API (this is the ONLY signal interface — do NOT invent others):\n"
        "  from axiom.strategies.base import BaseStrategy, Signal\n"
        "  Signal is a dataclass: Signal(entry_signal: bool, exit_signal: bool,\n"
        "    price: float, direction: 'long'|'short', confidence: float=0..1).\n"
        "  Entry example:  return Signal(entry_signal=True, direction='short', price=float(df['close'].iloc[-1]))\n"
        "  Exit example:   return Signal(exit_signal=True, price=float(df['close'].iloc[-1]))\n"
        "  Sentinels:      return Signal.LONG / Signal.SHORT / Signal.HOLD / Signal.EXIT\n"
        "  There is NO SignalType enum, NO Signal.BUY/SELL/EXIT_SHORT semantics beyond those aliases,\n"
        "  and NO self.position state — generate_signal is STATELESS (decide from df only).\n"
        "  Use 'direction', not 'side'. Read params via self.params or self.p.\n\n"
        "Use a NEW type_name each time you change logic (e.g. liq_z_v1, liq_z_v2). "
        "Parameter-only changes do NOT require a new type_name — just re-run the backtest."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Full Python source of the strategy module. Must define a class "
                    "subclassing BaseStrategy and export STRATEGY_CLASS and TYPE_NAME."
                ),
            },
            "type_name": {
                "type": "string",
                "description": (
                    "Unique type identifier for this strategy, alphanumeric + underscores, ≤64 chars. "
                    "Must match TYPE_NAME exported by the module. Use versioned names (v1, v2) "
                    "each time the logic changes — bytecode caching prevents in-place edits."
                ),
            },
            "hypothesis_id": {
                "type": "string",
                "description": "Parent hypothesis ID to link this strategy to. Strongly recommended.",
            },
            "params": {
                "type": "object",
                "description": "Optional default_params override dict.",
            },
        },
        "required": ["code", "type_name"],
    },
    permissions={"role:strategy-developer", "role:quant-researcher", None},
    category="strategy",
)
def _tool_create_custom_strategy(
    code: str,
    type_name: str,
    hypothesis_id: str | None = None,
    params: dict | None = None,
) -> str:
    """Write, validate, and register a custom BaseStrategy module."""
    import os
    import re

    code = str(code or "").strip()
    type_name = str(type_name or "").strip()
    hypothesis_id = str(hypothesis_id or "").strip() or None

    if not code:
        return "Error: 'code' is required"
    if not type_name:
        return "Error: 'type_name' is required"
    if not re.match(r"^[A-Za-z0-9_]{1,64}$", type_name):
        return "Error: type_name must be alphanumeric with underscores only, ≤64 chars"

    custom_dir = os.path.join(os.path.dirname(__file__), "..", "strategies", "custom")
    filepath = os.path.join(custom_dir, f"{type_name}.py")

    if os.path.exists(filepath):
        return (
            f"Error: type_name '{type_name}' already exists at {filepath}. "
            "Use a new versioned name (e.g. append _v2) for logic changes, or "
            "call run_backtest with the existing strategy_id for param-only changes."
        )

    # Validate via self-healing sandbox (lint + AST guard + execution harness)
    try:
        from axiom.selfheal import validate_strategy_code
        result = validate_strategy_code(code)
        if not result["valid"]:
            return _format_strategy_validation_failure(result, code)
        final_code = result.get("code") or code
    except Exception as exc:
        return f"Validation error: {exc}"

    # Write file
    os.makedirs(custom_dir, exist_ok=True)
    init_path = os.path.join(custom_dir, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w", encoding="utf-8") as f:
            f.write('"""Custom strategies — agent-generated modules."""\n')

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(final_code)

    # Register and reload
    try:
        from axiom.strategies.registry import reset, discover, _TYPE_MAP
        from axiom.strategies.intake import register_custom_strategy_file

        reset()
        registration = register_custom_strategy_file(
            file_path=filepath,
            source="agent_create_custom",
            hypothesis_id=hypothesis_id,
            default_params_override=_with_hypothesis_timeframe(params, hypothesis_id),
            origin_task_id=str(_current_task_display_id_var.get() or "").strip() or None,
        )
        discover()

        if type_name not in _TYPE_MAP:
            return (
                f"Warning: file saved to {filepath} but type '{type_name}' not found "
                f"in registry. Ensure the module exports TYPE_NAME = '{type_name}' "
                "and STRATEGY_CLASS."
            )

        registered_strategy_id = str(registration.get("strategy_id") or "").strip()
        current_strategy_id = str(_current_strategy_id_var.get() or "").strip()
        target_strategy_id = registered_strategy_id or current_strategy_id
        if target_strategy_id:
            provenance = _current_candidate_provenance(type_name)
            with get_db() as conn:
                conn.execute(
                    "UPDATE strategies SET runtime_type = ?, updated_at = ? WHERE id = ?",
                    (type_name, datetime.now(timezone.utc).isoformat(), target_strategy_id),
                )
            _persist_strategy_provenance(target_strategy_id, provenance)

        hyp_note = f" for hypothesis {hypothesis_id}" if hypothesis_id else ""
        if registered_strategy_id:
            return (
                f"Custom strategy '{type_name}' registered as {registered_strategy_id}{hyp_note}. "
                f"Call run_backtest(strategy_id='{registered_strategy_id}', asset=...) to evaluate it."
            )
        return (
            f"Custom strategy '{type_name}' registered{hyp_note} "
            "(no strategy container id returned — check intake logs)."
        )
    except Exception as exc:
        return f"File saved to {filepath} but registry reload failed: {exc}. May work after restart."


@register_tool(
    name="lint_code",
    description="Lint Python code with ruff and return issues. Also attempts auto-fix.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to lint"},
        },
        "required": ["code"],
    },
)
def _tool_lint_code(code: str) -> str:
    """Lint Python code with ruff."""
    from axiom.sandbox import lint_code
    result = lint_code(code)
    if result["passed"]:
        return "Code passed linting (no issues)."
    issues = "\n".join(result["issues"][:20])
    output = f"Lint issues found:\n{issues}"
    if result["fixed_code"]:
        output += f"\n\nAuto-fixed code:\n```python\n{result['fixed_code'][:3000]}\n```"
    return output

@register_tool(
    name="optimize_strategy",
    description="Run parameter optimization (grid search + WFA) on a strategy. Returns best params and validation status.",
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to optimize"},
            "asset": {"type": "string", "description": "Coin symbol (optional, auto-detected)"},
            "strategy_type": {"type": "string", "description": "Strategy type (optional, auto-detected)"},
        },
        "required": ["strategy_id"],
    },
)
def _tool_optimize_strategy(params: dict) -> str:
    """Run parameter optimization on a strategy."""
    from axiom.strategies.optimizer import optimize_strategy
    result = optimize_strategy(
        strategy_id=params["strategy_id"],
        asset=params.get("asset"),
        strategy_type=params.get("strategy_type"),
    )
    if result.get("error"):
        return f"Optimization error: {result['error']}"
    return json.dumps({
        "best_params": result["best_params"],
        "best_fitness": result["best_fitness"],
        "wfa_verdict": result["wfa_verdict"],
        "validated": result["validated"],
        "top_results": [
            {"params": r["params"], "fitness": r["fitness"]}
            for r in result.get("top_results", [])
        ],
    }, indent=2)


# Axiom availability cache — avoid repeated health checks that waste time
_backtesting_available: bool | None = None
_backtesting_checked_at: float = 0
_BACKTESTING_SUCCESS_CACHE_TTL = 90  # seconds
_BACKTESTING_FAILURE_CACHE_TTL = 8   # seconds


def _check_backtesting_available() -> bool:
    """Check Axiom availability with short failure TTL for quick recovery."""
    global _backtesting_available, _backtesting_checked_at
    import time as _time
    now = _time.monotonic()
    ttl = (
        _BACKTESTING_SUCCESS_CACHE_TTL
        if _backtesting_available
        else _BACKTESTING_FAILURE_CACHE_TTL
    )
    if _backtesting_available is not None and (now - _backtesting_checked_at) < ttl:
        return _backtesting_available
    from axiom.backtesting import is_available
    _backtesting_available = is_available()
    _backtesting_checked_at = now
    return _backtesting_available


_BACKTESTING_FALLBACK_MSG = (
    "axiom Backtesting is not reachable. Do NOT debug connectivity — "
    "use your local tools instead: run_backtest, optimize_strategy, search_chroma, "
    "list_local_datasets. These provide equivalent backtesting capabilities."
)




# Certified strategy families that don't require rule-blob configuration
CERTIFIED_STRATEGY_FAMILIES = {
    "stochastic", "stoch", "williams_r", "wr", "rsi", "rsi_momentum",
    "ema_cross", "ema", "macd", "bb", "bollinger", "atr", "adx", "orb"
}


def _is_certified_strategy_family(strategy_type: str, strategy_name: str = "") -> bool:
    """Check if strategy type or name belongs to a certified family.
    
    Certified families use built-in indicators and don't require rule-blob
    configuration (indicators, entry_conditions, exit_conditions, filters).
    """
    strategy_type_lower = str(strategy_type or "").lower().strip()
    strategy_name_lower = str(strategy_name or "").lower().strip()
    
    # Check if type or name matches any certified family
    for family in CERTIFIED_STRATEGY_FAMILIES:
        if family in strategy_type_lower or family in strategy_name_lower:
            return True
    return False

def _tool_backtesting(tool_name: str, params: dict) -> str:
    """Execute a Axiom Backtesting tool. Routes to the backtesting client."""
    from axiom.backtesting import get_client

    if not _check_backtesting_available():
        return _BACKTESTING_FALLBACK_MSG

    client = get_client()

    try:
        if tool_name == "AXIOM_list_datasets":
            result = client.list_datasets(
                symbol_filter=params.get("symbol_filter", ""),
                timeframe_filter=params.get("timeframe_filter", ""),
            )
        elif tool_name == "AXIOM_create_strategy":
            from axiom.crucible_tasks import validate_candidate_strategy_creation

            strategy_type = params.get("strategy_type") or params.get("type", "backtest")
            strategy_name = params.get("name", "")
            crucible_id = str(params.get("crucible_id") or params.get("hypothesis_id") or "").strip()
            hypothesis_id = str(params.get("hypothesis_id") or crucible_id).strip()
            if not hypothesis_id:
                return json.dumps({"error": "hypothesis_id is required for all new strategies"})
            validation = validate_candidate_strategy_creation(
                crucible_id,
                str(_current_agent_id_var.get() or "").strip(),
                str(_current_task_display_id_var.get() or "").strip(),
                hypothesis_id,
            )
            if not validation.allowed:
                return json.dumps({"error": validation.reason})
            crucible_id = str(validation.crucible_id or crucible_id).strip()
            hypothesis_id = str(validation.hypothesis_id or hypothesis_id).strip()
            provenance = _current_candidate_provenance(crucible_id)
            hypothesis_timeframe = _get_hypothesis_tf(hypothesis_id)
            
            # Check if this is a certified strategy family that doesn't need rule-blobs
            if _is_certified_strategy_family(strategy_type, strategy_name):
                # Certified families: only send core fields, NOT rule-blobs
                result = client.create_strategy(
                    name=params["name"],
                    type=strategy_type,
                    hypothesis_id=hypothesis_id,
                    notes=params.get("notes", ""),
                    params=params.get("params"),
                    symbol=params.get("symbol", ""),
                    timeframe=hypothesis_timeframe or params.get("timeframe") or "1h",
                )
            else:
                # Custom strategies: send full rule-blob configuration
                result = client.create_strategy(
                    name=params["name"],
                    type=strategy_type,
                    hypothesis_id=hypothesis_id,
                    indicators=params.get("indicators"),
                    entry_conditions=params.get("entry_conditions"),
                    exit_conditions=params.get("exit_conditions"),
                    filters=params.get("filters"),
                    notes=params.get("notes", ""),
                    params=params.get("params"),
                    symbol=params.get("symbol", ""),
                    timeframe=hypothesis_timeframe or params.get("timeframe") or "1h",
                )
            # Ensure consistent ID return format for backward compatibility
            if isinstance(result, dict) and "id" not in result and "strategy_id" in result:
                result["id"] = result["strategy_id"]
            if isinstance(result, dict):
                _persist_strategy_provenance(str(result.get("id") or result.get("strategy_id") or ""), provenance)
        elif tool_name == "AXIOM_run_backtest":
            result = client.run_backtest(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                parameters=params.get("parameters"),
                fee_bps=params.get("fee_bps", 4.5),
                slippage_bps=params.get("slippage_bps", 2.0),
                timeframe=(
                    _get_hypothesis_tf(params.get("hypothesis_id", ""))
                    or params.get("timeframe")
                    or _get_strategy_or_hypothesis_tf(params.get("strategy_id"))
                ),
                request_source="agent_tool",
                origin_agent_id=str(_current_agent_id_var.get() or "").strip() or None,
                origin_task_id=str(_current_task_display_id_var.get() or "").strip() or None,
            )
        elif tool_name == "AXIOM_run_optimization":
            parameter_ranges = params.get("parameter_ranges")
            if not isinstance(parameter_ranges, dict):
                parameter_ranges = {}
            result = client.run_optimization(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                parameter_ranges=parameter_ranges,
                objective=params.get("objective", "sharpe_ratio"),
                n_trials=params.get("n_trials", 50),
            )
        elif tool_name == "AXIOM_run_verdict":
            result = client.run_verdict(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                tests=params.get("tests"),
            )
            try:
                persisted = _persist_agent_verdict(
                    str(params["strategy_id"]),
                    result if isinstance(result, dict) else {},
                )
                if isinstance(result, dict):
                    result["persisted_strategy_metrics"] = bool(persisted)
            except Exception as exc:
                log.warning(
                    "Agent verdict persistence failed for %s: %s",
                    params.get("strategy_id"),
                    exc,
                )
        elif tool_name == "AXIOM_get_results":
            result = client.get_results(
                result_id=params["result_id"],
                include_trades=params.get("include_trades", False),
                include_equity_curve=params.get("include_equity_curve", False),
            )
        else:
            return f"Unknown backtesting tool: {tool_name}"

        # Truncate large results
        output = json.dumps(result, indent=2)
        if len(output) > 8000:
            output = output[:8000] + "\n... (truncated)"
        return output

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        detail = ""
        if e.response is not None:
            try:
                payload = e.response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("detail") or "").strip()
                elif payload is not None:
                    detail = str(payload).strip()
            except Exception:
                detail = str(e.response.text or "").strip()
        if detail:
            return f"Backtesting tool error ({tool_name}): HTTP {status_code} - {detail}"
        return f"Backtesting tool error ({tool_name}): HTTP {status_code}"
    except Exception as e:
        return f"Backtesting tool error ({tool_name}): {e}"


# ── Register Axiom Backtesting tools (routed through _tool_backtesting) ──

def _make_jbt_handler(name: str):
    """Create a handler that delegates to _tool_backtesting with a fixed tool name."""
    def handler(params: dict) -> str:
        return _tool_backtesting(name, params)
    return handler


register_tool(
    name="AXIOM_list_datasets",
    description=(
        "List available backtesting datasets on Axiom Backtesting. Returns datasets with "
        "symbol, timeframe, row count, and date ranges. Use to discover what data is available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol_filter": {"type": "string", "description": "Filter by symbol e.g. 'BTC'"},
            "timeframe_filter": {"type": "string", "description": "Filter by timeframe e.g. '1h', '4h'"},
        },
        "required": [],
    },
)(_make_jbt_handler("AXIOM_list_datasets"))

register_tool(
    name="AXIOM_create_strategy",
    description=(
        "Create a tradable strategy on Axiom Backtesting. Any strategy family and params are accepted — "
        "composite strategies mixing multiple indicator families are encouraged and can run in paper/live."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique strategy name"},
            "hypothesis_id": {"type": "string", "description": "Parent hypothesis ID for this strategy."},
            "crucible_id": {"type": "string", "description": "Planner-approved crucible/hypothesis ID for this candidate."},
            "strategy_type": {
                "type": "string",
                "description": (
                    "Strategy family name. Prefer executable Axiom families such as orb, "
                    "macd, rsi_momentum, ema_cross, bollinger, stochastic, and williams_r; "
                    "the API may route unsupported rule blobs to research_only."
                ),
            },
            "symbol": {"type": "string", "description": "Trading symbol, e.g. BTC/USDT"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d"},
            "params": {"type": "object", "description": "Strategy parameters dict — any params your strategy needs"},
            "notes": {"type": "string", "description": "Notes explaining the strategy logic"},
        },
        "required": ["name", "hypothesis_id", "strategy_type", "symbol", "params"],
    },
    permissions={"role:strategy-developer", None},
)(_make_jbt_handler("AXIOM_create_strategy"))

register_tool(
    name="AXIOM_run_backtest",
    description=(
        "Run a backtest on Axiom Backtesting with realistic fees (4.5 bps) and slippage (2 bps)."
        "Returns full performance metrics: Sharpe, Sortino, win rate, profit factor, max drawdown, "
        "total return, trade count."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to test"},
            "dataset_id": {"type": "string", "description": "Dataset ID to test on"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "parameters": {"type": "object", "description": "Optional parameter overrides"},
            "fee_bps": {"type": "number", "description": "Fee in basis points (default 4.5, Hyperliquid taker)"},
            "slippage_bps": {"type": "number", "description": "Slippage in basis points (default 2.0)"},
        },
        "required": ["strategy_id", "dataset_id"],
    },
)(_make_jbt_handler("AXIOM_run_backtest"))

register_tool(
    name="AXIOM_run_optimization",
    description=(
        "Run parameter optimization on Axiom Backtesting. Finds optimal parameter values using Optuna. "
        "Max 200 trials. Returns best parameters and metric improvement."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to optimize"},
            "dataset_id": {"type": "string", "description": "Dataset ID"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "parameter_ranges": {"type": "object", "description": "Param ranges e.g. {'sma_window': [10, 50]}"},
            "objective": {"type": "string", "description": "Metric: sharpe_ratio, total_return, sortino_ratio, calmar_ratio"},
            "n_trials": {"type": "integer", "description": "Number of trials (default 50, max 200)"},
        },
        "required": ["strategy_id", "dataset_id", "parameter_ranges"],
    },
)(_make_jbt_handler("AXIOM_run_optimization"))

register_tool(
    name="AXIOM_run_verdict",
    description=(
        "Run the backtesting verdict engine to validate a strategy. Tests: sample_size, "
        "statistical_significance, walk_forward, monte_carlo, parameter_stability, "
        "cost_stress, regime_performance. Returns pass/warn/fail per test."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to validate"},
            "dataset_id": {"type": "string", "description": "Dataset ID"},
            "tests": {"type": "array", "description": "Specific tests to run (default all)", "items": {"type": "string"}},
        },
        "required": ["strategy_id", "dataset_id"],
    },
)(_make_jbt_handler("AXIOM_run_verdict"))

register_tool(
    name="AXIOM_get_results",
    description="Get detailed results from a Axiom backtest, including optional trade list and equity curve.",
    input_schema={
        "type": "object",
        "properties": {
            "result_id": {"type": "string", "description": "Result ID to retrieve"},
            "include_trades": {"type": "boolean", "description": "Include individual trades (default false)"},
            "include_equity_curve": {"type": "boolean", "description": "Include equity curve (default false)"},
        },
        "required": ["result_id"],
    },
)(_make_jbt_handler("AXIOM_get_results"))
