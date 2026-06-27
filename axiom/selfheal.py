"""Self-healing code validator: lint, fix, and test agent-generated code."""

import logging
import re

from axiom.sandbox import lint_code, run_code
from axiom.sandbox.ast_guard import scan_source

log = logging.getLogger("axiom.selfheal")

MAX_FIX_ROUNDS = 3

# Regex patterns for pandas 2.x APIs removed in pandas 3.0.
# Applied in normalize_generated_strategy_code so the harness subprocess always
# runs pandas-3-safe code regardless of which model generated the strategy.
_PANDAS3_FIXUPS: list[tuple[re.Pattern[str], str]] = [
    # .fillna(method='ffill') / .fillna(method='pad') → .ffill()
    (re.compile(r"\.fillna\(\s*method\s*=\s*['\"](?:ffill|pad)['\"]\s*\)"), ".ffill()"),
    # .fillna(method='bfill') / .fillna(method='backfill') → .bfill()
    (re.compile(r"\.fillna\(\s*method\s*=\s*['\"](?:bfill|backfill)['\"]\s*\)"), ".bfill()"),
    # pd.options.mode.chained_assignment = ... — option removed in pandas 3.0 (CoW)
    (re.compile(r"^[^\S\n]*pd\.options\.mode\.chained_assignment\s*=.*$", re.MULTILINE), ""),
]


def _fix_pandas3_compat(code: str) -> str:
    """Replace pandas 2.x patterns that raise in pandas 3.0 with their modern equivalents."""
    for pattern, replacement in _PANDAS3_FIXUPS:
        new_code = pattern.sub(replacement, code)
        if new_code != code:
            log.debug("pandas-3 compat fixup applied: %s", pattern.pattern[:60])
            code = new_code
    return code


def normalize_generated_strategy_code(code: str) -> str:
    """Normalize agent-generated strategy modules before lint/import checks."""
    normalized = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    future_imports: list[str] = []
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from __future__ import "):
            if stripped not in future_imports:
                future_imports.append(stripped)
            continue
        body_lines.append(line)

    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)

    if not future_imports:
        return _fix_pandas3_compat(normalized)

    body = "\n".join(body_lines).rstrip()
    result = "\n".join([*future_imports, "", body]).rstrip() + "\n"
    return _fix_pandas3_compat(result)


def validate_strategy_code(code: str) -> dict:
    """Validate agent-generated strategy code through lint + sandbox."""
    current_code = normalize_generated_strategy_code(code)
    all_issues = []

    for round_num in range(MAX_FIX_ROUNDS):
        lint_result = lint_code(current_code)

        if lint_result["passed"]:
            break

        all_issues.extend(lint_result["issues"])

        if lint_result.get("fixed_code"):
            current_code = lint_result["fixed_code"]
            log.debug("Self-heal round %d: applied auto-fix", round_num + 1)
        else:
            break

    final_lint = lint_code(current_code)

    # SECURITY: static-scan the strategy with the AST guard BEFORE executing it.
    # run_code already isolates (secret-stripped env + resource caps), but every
    # other importer (registry/intake/optimizer) runs the guard first; doing the
    # same here removes the one agent path where code reached execution without a
    # scan, and rejects obviously-hostile modules (os/eval/file reads/aliased
    # builtins) before they run at all. We scan the raw strategy, not the wrapped
    # harness (the harness legitimately imports sys/inspect/pandas).
    ast_report = scan_source(current_code)
    if not ast_report.ok:
        findings = "; ".join(
            f"line {f.lineno}: {f.message}" for f in ast_report.findings[:5]
        )
        log.warning("Self-heal validation rejected by AST guard: %s", findings)
        return {
            "valid": False,
            "code": current_code,
            "lint_issues": all_issues,
            "lint_passed": final_lint["passed"],
            "ast_findings": [
                {"lineno": f.lineno, "col": f.col, "kind": f.kind, "message": f.message}
                for f in ast_report.findings
            ],
            "execution_result": {
                "returncode": -1,
                "stdout": "",
                "stderr": f"AST guard blocked execution: {findings}",
                "timed_out": False,
            },
        }

    # Fetch enrichment columns in the MAIN process (avoids a network call inside
    # the subprocess that can timeout and push total wall time >30s with pandas 3.x
    # heavier imports). On failure the harness falls back to its built-in list.
    try:
        from axiom.lan_enricher import get_lan_enricher as _get_lan_enricher
        _main_enrich_cols = _get_lan_enricher().available_metrics("BTCUSDT") or []
    except Exception:
        _main_enrich_cols = []
    test_code = _wrap_with_test_harness(current_code, enrich_cols=_main_enrich_cols)
    exec_result = run_code(test_code, timeout=60)

    valid = (
        final_lint["passed"]
        and exec_result["returncode"] == 0
        and not exec_result["timed_out"]
    )

    if not valid:
        reasons = []
        if not final_lint["passed"]:
            reasons.append(f"lint: {len(final_lint['issues'])} issues")
        if exec_result["returncode"] != 0:
            reasons.append(f"exec: exit code {exec_result['returncode']}")
        if exec_result["timed_out"]:
            reasons.append("exec: timed out")
        if exec_result["stderr"]:
            reasons.append(f"stderr: {exec_result['stderr'][:200]}")
        log.warning("Self-heal validation failed: %s", "; ".join(reasons))

    return {
        "valid": valid,
        "code": current_code,
        "lint_issues": all_issues,
        "lint_passed": final_lint["passed"],
        "execution_result": {
            "returncode": exec_result["returncode"],
            "stdout": exec_result["stdout"][:2000],
            "stderr": exec_result["stderr"][:1000],
            "timed_out": exec_result["timed_out"],
        },
    }


def _wrap_with_test_harness(code: str, enrich_cols: list[str] | None = None) -> str:
    """Wrap strategy code with a runtime validation harness.

    ``enrich_cols`` should be pre-fetched by the caller from the LAN enricher in
    the main process to avoid a subprocess network call that can timeout and push
    total wall time over the subprocess deadline on pandas 3.x (heavier imports).
    """
    code = normalize_generated_strategy_code(code)
    future_lines: list[str] = []
    body_lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("from __future__ import "):
            future_lines.append(stripped)
        else:
            body_lines.append(line)
    future_block = "\n".join(dict.fromkeys(future_lines))
    if future_block:
        future_block += "\n"
    code_body = "\n".join(body_lines).strip()

    # Build the enrichment column list to embed directly in the harness script.
    # Baseline fallback covers the columns user strategies most commonly access.
    # The caller supplements this with the live LAN column list from the main process
    # so the dummy DataFrame matches what a real backtest DataFrame looks like.
    _FALLBACK = [
        "funding_rate", "open_interest", "long_short_ratio", "ls_ratio",
        "liq_long_volume", "liq_short_volume", "liq_total_volume", "liq_count",
        "l2_imbalance_5_avg", "l2_spread_bps_avg",
        "l2_large_bid_count_avg", "l2_large_ask_count_avg",
        "taker_buy_sell_ratio",
    ]
    merged_cols = list(_FALLBACK)
    for c in (enrich_cols or []):
        if c not in merged_cols:
            merged_cols.append(c)
    # Embed as a Python list literal so the subprocess never needs to call the LAN API.
    enrich_cols_repr = repr(merged_cols)

    return f'''\
{future_block}
import sys

# Agent-generated code
{code_body}

# Test harness
import inspect
import numpy as np
import pandas as pd

from axiom.strategies.base import BaseStrategy, Signal, DirectionalSignals

index = pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")
close = np.linspace(100.0, 110.0, num=100)
open_ = np.concatenate(([close[0]], close[:-1]))
high = np.maximum(open_, close) + 0.5
low = np.minimum(open_, close) - 0.5
volume = np.linspace(1000.0, 2000.0, num=100)

# Enrichment columns embedded by the caller at harness-generation time.
# No network call is made in the subprocess — the column list was fetched
# from the LAN enricher in the main process before this script was written.
_enrich_cols = {enrich_cols_repr}

_enrich_data = {{col: np.ones(100) for col in _enrich_cols}}
dummy_df = pd.DataFrame(
    {{
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        **_enrich_data,
    }},
    index=index,
)

strategy_classes = []
for name, obj in list(globals().items()):
    if not inspect.isclass(obj):
        continue
    if obj is BaseStrategy:
        continue
    try:
        if issubclass(obj, BaseStrategy):
            strategy_classes.append((name, obj))
    except Exception:
        continue

if not strategy_classes:
    print("ERROR: No BaseStrategy subclass found")
    sys.exit(1)

for cls_name, cls in strategy_classes:
    print(f"Found strategy class: {{cls_name}}")

    required = ["name", "asset", "strategy_type", "default_params"]
    for attr in required:
        if not hasattr(cls, attr):
            print(f"ERROR: Missing required attribute: {{attr}}")
            sys.exit(1)

    try:
        instance = cls("test_id", {{}})
    except Exception as exc:
        print(f"ERROR: Could not instantiate {{cls_name}}: {{exc}}")
        sys.exit(1)

    try:
        signal = instance.generate_signal(dummy_df.copy())
    except Exception as exc:
        print(f"ERROR: generate_signal failed for {{cls_name}}: {{exc}}")
        sys.exit(1)

    if isinstance(signal, Signal):
        signal_payload = signal.to_dict()
    elif isinstance(signal, dict):
        signal_payload = dict(signal)
    else:
        print(f"ERROR: generate_signal returned invalid type for {{cls_name}}: {{type(signal).__name__}}")
        sys.exit(1)

    required_signal_keys = {{"entry_signal", "exit_signal"}}
    missing_signal_keys = sorted(required_signal_keys.difference(signal_payload))
    if missing_signal_keys:
        print(f"ERROR: Missing signal keys for {{cls_name}}: {{', '.join(missing_signal_keys)}}")
        sys.exit(1)

    for key in ("entry_signal", "exit_signal", "price", "confidence", "direction"):
        value = signal_payload.get(key)
        if isinstance(value, (pd.Series, np.ndarray, list, tuple)):
            print(
                f"ERROR: {{key}} must be a scalar value from generate_signal(); "
                "implement generate_signals(df) for vectorized Series output."
            )
            sys.exit(1)

    # Validate the VECTORIZED generate_signals(df) path. The backtester prefers
    # it over the scalar generate_signal() loop, so a malformed vectorized payload
    # (wrong shape, raised exception) passes this validation but blows up at
    # backtest time, leaving the strategy with no metrics and burying it. Mirror
    # the backtester contract (backtest.py: DirectionalSignals | 2/4-tuple | None)
    # here so the codegen retry can repair it in-conversation.
    if cls.generate_signals is not BaseStrategy.generate_signals:
        try:
            vec_payload = instance.generate_signals(dummy_df.copy())
        except NotImplementedError:
            vec_payload = None
        except Exception as exc:
            print(f"ERROR: generate_signals(df) raised for {{cls_name}}: {{exc}}")
            sys.exit(1)
        if vec_payload is not None and not (
            isinstance(vec_payload, DirectionalSignals)
            or (isinstance(vec_payload, (tuple, list)) and len(vec_payload) in (2, 4))
        ):
            print(
                f"ERROR: generate_signals(df) for {{cls_name}} must return "
                "(entry_signals, exit_signals), DirectionalSignals, a 4-series "
                f"payload, or None — got {{type(vec_payload).__name__}}"
            )
            sys.exit(1)

    print(f"Validated {{cls_name}} with direction={{signal_payload.get('direction', 'long')}}")

print("SELFHEAL_OK")
'''
