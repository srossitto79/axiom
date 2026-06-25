#!/usr/bin/env python
"""READ-ONLY backfill harness: how many existing strategies would pass the
promotion gates under each pipeline stance preset (relaxed / default / strict).

This script does NOT write to the DB, promote anything, or mutate KV. It only
*reads* strategies and *re-evaluates* the gate functions from forven.policy
against three resolved preset configs, then prints a summary table.

For every strategy it runs the gate relevant to its CURRENT stage:
    quick_screen stage -> _evaluate_quick_screen_gate   (-> gauntlet)
    gauntlet     stage -> _evaluate_gauntlet_gate        (-> paper)
    paper        stage -> _evaluate_paper_gate           (-> live, real money)

It ALSO runs _evaluate_gauntlet_gate (the gauntlet->paper gate) across EVERY
strategy regardless of stage — that is the headline "how many can reach paper"
number — and _evaluate_paper_gate across every strategy for the "how many can
reach live" number.

Run from the repo root so the `forven` package imports:
    .venv\\Scripts\\python.exe scripts\\backfill_gate_passrates.py   (Windows)
    .venv/bin/python scripts/backfill_gate_passrates.py             (POSIX)
"""

from __future__ import annotations

import os
import sys
import traceback
from collections import Counter

# Defensive sys.path bootstrap so the script also works if launched from inside
# the scripts/ directory rather than the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PRESETS = ["relaxed", "default", "strict"]

# How the per-stage gate is chosen. Stages are the canonical values produced by
# forven.util.normalize_stage (get_strategies already normalizes s["stage"]).
STAGE_TO_GATE = {
    "quick_screen": "quick_screen",
    "gauntlet": "gauntlet",
    "paper": "paper",
}

# Truncate a rejection reason to a stable prefix so near-identical reasons
# (which embed strategy-specific numbers) group together for the top-3 tally.
_REASON_PREFIX_LEN = 60


def _reason_prefix(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        return "(empty reason)"
    if len(text) > _REASON_PREFIX_LEN:
        return text[:_REASON_PREFIX_LEN].rstrip() + "..."
    return text


def main() -> int:
    # Imports are inside main() so an import failure is reported cleanly rather
    # than crashing at module load with a bare traceback.
    try:
        from forven.db import get_strategies
        from forven.policy import (
            _evaluate_gauntlet_gate,
            _evaluate_paper_gate,
            _evaluate_quick_screen_gate,
            _normalize_pipeline_config,
        )
    except Exception:
        print("FATAL: failed to import forven backend modules.", file=sys.stderr)
        print(
            "Run this from the repo root with the project's python so the "
            "`forven` package is importable, e.g.:",
            file=sys.stderr,
        )
        print(r"  .venv\Scripts\python.exe scripts\backfill_gate_passrates.py", file=sys.stderr)
        traceback.print_exc()
        return 2

    # Enumerate all strategies once (read-only). get_strategies() returns dicts
    # with a normalized "stage" field and an "id".
    try:
        strategies = get_strategies()
    except Exception:
        print("FATAL: failed to enumerate strategies from the DB.", file=sys.stderr)
        traceback.print_exc()
        return 3

    total_strategies = len(strategies)

    # Stage distribution (informational).
    stage_counts: Counter[str] = Counter(
        str(s.get("stage") or "quick_screen") for s in strategies
    )

    gate_fns = {
        "quick_screen": _evaluate_quick_screen_gate,
        "gauntlet": _evaluate_gauntlet_gate,
        "paper": _evaluate_paper_gate,
    }

    # Per-preset results accumulate here for the final table.
    rows: list[dict] = []

    for preset in PRESETS:
        try:
            config = _normalize_pipeline_config({"pipeline_preset": preset})
        except Exception:
            print(f"  ! could not resolve config for preset '{preset}':", file=sys.stderr)
            traceback.print_exc()
            rows.append(
                {
                    "preset": preset,
                    "evaluated": 0,
                    "stage_pass": 0,
                    "stage_fail": 0,
                    "gauntlet_pass": 0,
                    "paper_pass": 0,
                    "errors": total_strategies,
                    "top_reasons": [("config resolution failed", total_strategies)],
                }
            )
            continue

        # Counters for this preset.
        stage_pass = 0          # passed the gate for its OWN current stage
        stage_fail = 0          # failed the gate for its own current stage
        stage_skipped = 0       # terminal/side-lane stages with no forward gate
        stage_errors = 0        # exceptions running the per-stage gate

        gauntlet_pass = 0       # would pass gauntlet->paper (headline: reach paper)
        gauntlet_errors = 0

        paper_pass = 0          # would pass paper->live (real money)
        paper_errors = 0

        reason_counter: Counter[str] = Counter()

        for s in strategies:
            sid = s.get("id")
            stage = str(s.get("stage") or "quick_screen")

            # --- 1. Gate for the strategy's OWN current stage ---------------
            gate_key = STAGE_TO_GATE.get(stage)
            if gate_key is None:
                # research_only / archived / rejected / backtest_failed /
                # live_graduated have no forward promotion gate to evaluate.
                stage_skipped += 1
            else:
                try:
                    ok, reason = gate_fns[gate_key](sid, config)
                    if ok:
                        stage_pass += 1
                    else:
                        stage_fail += 1
                        reason_counter[_reason_prefix(reason)] += 1
                except Exception as exc:  # one bad row must not abort the run
                    stage_errors += 1
                    reason_counter[f"[ERROR] {type(exc).__name__}"] += 1

            # --- 2. Gauntlet->paper across ALL strategies (headline) -------
            try:
                gok, _ = _evaluate_gauntlet_gate(sid, config)
                if gok:
                    gauntlet_pass += 1
            except Exception:
                gauntlet_errors += 1

            # --- 3. Paper->live across ALL strategies (real-money reach) ---
            try:
                pok, _ = _evaluate_paper_gate(sid, config)
                if pok:
                    paper_pass += 1
            except Exception:
                paper_errors += 1

        rows.append(
            {
                "preset": preset,
                "evaluated": total_strategies,
                "stage_pass": stage_pass,
                "stage_fail": stage_fail,
                "stage_skipped": stage_skipped,
                "stage_errors": stage_errors,
                "gauntlet_pass": gauntlet_pass,
                "gauntlet_errors": gauntlet_errors,
                "paper_pass": paper_pass,
                "paper_errors": paper_errors,
                "top_reasons": reason_counter.most_common(3),
            }
        )

    # ---------------------------------------------------------------- output
    print("=" * 88)
    print("FORVEN GATE PASS-RATE BACKFILL  (READ-ONLY - no DB/KV writes, no promotions)")
    print("=" * 88)
    print(f"Total strategies enumerated: {total_strategies}")
    if total_strategies:
        dist = ", ".join(f"{k}={v}" for k, v in sorted(stage_counts.items()))
        print(f"Stage distribution: {dist}")
    print()
    print(
        "Per-stage gate = the forward gate for each strategy's CURRENT stage "
        "(quick_screen->gauntlet, gauntlet->paper, paper->live)."
    )
    print(
        "Gauntlet->paper PASS = headline 'how many can reach paper' (gauntlet "
        "gate run across ALL strategies)."
    )
    print(
        "Paper->live PASS = real-money gate run across ALL strategies."
    )
    print()

    # Table header.
    header = (
        f"{'PRESET':<9} {'#STRAT':>6} {'STAGE-PASS':>10} {'G->PAPER':>9} "
        f"{'P->LIVE':>8} {'ERRS':>5}  TOP-3 REJECT REASONS"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        errs = (
            int(r.get("stage_errors", 0))
            + int(r.get("gauntlet_errors", 0))
            + int(r.get("paper_errors", 0))
        )
        reasons = r.get("top_reasons") or []
        if reasons:
            first = reasons[0]
            reason_str = f"{first[0]} (x{first[1]})"
        else:
            reason_str = "(none)"
        print(
            f"{r['preset']:<9} {r['evaluated']:>6} {r.get('stage_pass', 0):>10} "
            f"{r.get('gauntlet_pass', 0):>9} {r.get('paper_pass', 0):>8} {errs:>5}  {reason_str}"
        )
        # Print remaining top reasons on indented continuation lines.
        for extra in reasons[1:]:
            pad = " " * (9 + 1 + 6 + 1 + 10 + 1 + 9 + 1 + 8 + 1 + 5 + 2)
            print(f"{pad}{extra[0]} (x{extra[1]})")
    print("-" * len(header))
    print()

    # Detailed per-preset breakdown (stage pass/fail/skip/errors).
    for r in rows:
        print(
            f"[{r['preset']}] stage-gate: pass={r.get('stage_pass', 0)} "
            f"fail={r.get('stage_fail', 0)} skipped(no-fwd-gate)={r.get('stage_skipped', 0)} "
            f"errors={r.get('stage_errors', 0)} | "
            f"gauntlet->paper pass={r.get('gauntlet_pass', 0)} (errs={r.get('gauntlet_errors', 0)}) | "
            f"paper->live pass={r.get('paper_pass', 0)} (errs={r.get('paper_errors', 0)})"
        )
        for reason, cnt in (r.get("top_reasons") or []):
            print(f"        reject: {reason} (x{cnt})")
    print()

    # Local-DB note (always printed so a small/empty local DB is not mistaken
    # for "no strategies would ever pass").
    if total_strategies < 50:
        print(
            "NOTE: the meaningful dataset (the ~334 strategies the bot built) lives on "
            "the machine that ran the bot; run this script there for real pass-rates. "
            "Locally this only proves the harness works."
        )
    else:
        print(
            "NOTE: this run has a substantial local dataset. If this is NOT the machine "
            "that ran the bot, the canonical ~334-strategy results live there; run this "
            "script on that machine for the authoritative pass-rates."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
