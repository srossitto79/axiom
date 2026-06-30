# Context Requirements & Budget Plan — Axiom Agent System

> Measured 2026-06-30 from the live `agent_tasks` table in the production
> container. All agents currently run on **DeepSeek V4 Pro / Flash** (1M+
> context). This doc corrects how those numbers must be read, states whether the
> system can run on a bounded (≤64–128K) window, and gives the action plan to
> get there.

---

## TL;DR

- The "562K–1M token" per-agent figures below are **accumulated across up to 25
  tool rounds** — a *sum* recorded by `_accum()` in `runner.py`, **not** the size
  of any single prompt and **not** a context-window requirement.
- The real constraint is the **peak single-round window**:
  `static_base + tool_schemas + accumulated_message_history` on the final round.
- The tool loop has **no eviction** — `messages` only grows. For heavy agents
  (Strategy Developer, Brain, Quant) that in-loop growth, not the static base, is
  the dominant driver. Trimming static files is necessary but **not sufficient**.
- **Verdict: running on 64–128K is achievable, but it requires bounding the
  working set per round (architectural), not "compressing" 1M into 64K.** Plan
  for **128K as the supported floor**; 64K is realistic for light agents and a
  stretch goal for heavy ones. This is fixable engineering, not a dead end.

---

## How to Read These Numbers (read this first)

The token counts in the tables are **accumulated**, not per-prompt. The loop in
`axiom/agents/runner.py::_call_with_tools_single` does:

```python
for round_num in range(MAX_TOOL_ROUNDS):        # MAX_TOOL_ROUNDS = 25
    response = await impl.call(model_id, messages, system, active_tools, token)
    _accum(response.usage)                       # SUMS input_tokens every round
    impl.append_assistant(messages, response)    # messages only GROWS
    impl.append_tool_results(messages, tool_results)
```

- `system` (static base) + `active_tools` (full tool schemas, ~12K tokens) are
  **re-sent and re-counted every round**.
- `messages` accumulates: each round appends the assistant turn + every tool
  result (each capped at 5000 chars ≈ 1.25K tokens, `runner.py` `str(result)[:5000]`).
- `_accum` adds each round's `input_tokens` to a running total.

So for N rounds:

```
accumulated ≈ N·base + tool_defs·N + history_growth·N(N-1)/2   ← what the tables below show
peak_window ≈ base + tool_defs + history_growth·(N-1)          ← what a context window must hold
```

A 25-round task reports an accumulated total several times larger than the
largest prompt the model ever actually saw. **Nothing currently measures
`peak_window`** — fixing that is Task 0 of the plan.

---

## Per-Agent ACCUMULATED Token Usage (measured)

Sorted by median accumulated input tokens (P50). **These are sums across rounds,
not window requirements** — see above.

| Agent | Tasks | P50 | P90 | P99 | Max Ever |
|---|---|---|---|---|---|
| Brain | 1 | 797K | 797K | 797K | 797,082 |
| Strategy Developer | 378 | **562K** | 737K | 879K | 1,038,342 |
| Quant Researcher | 34 | **244K** | 623K | 1.1M | 1,117,641 |
| Full-Stack Engineer | 5 | 109K | 546K | 546K | 546,036 |
| Risk Manager | 22 | **109K** | 190K | 268K | 267,546 |
| Simulation Agent | 1 | 136K | 136K | 136K | 136,268 |

### By Task Type (accumulated)

| Agent | Task Type | Tasks | Avg Input | Max Input |
|---|---|---|---|---|
| Strategy Developer | generate_strategies | 29 | 564K | 805K |
| Strategy Developer | develop_candidate | 132 | 545K | 984K |
| Strategy Developer | research | 183 | 488K | 809K |
| Strategy Developer | analysis | 29 | 438K | 1,038K |
| Strategy Developer | backtest | 4 | 443K | 542K |
| Quant Researcher | research | 23 | 406K | 1,118K |
| Quant Researcher | post_mortem | 11 | 153K | 621K |
| Risk Manager | risk_audit | 22 | 124K | 268K |
| Simulation Agent | analysis | 1 | 136K | 136K |
| Full-Stack Engineer | analysis | 5 | 188K | 546K |
| Brain | analysis | 1 | 797K | 797K |

### Current model split

| Model | Tasks | Avg Accumulated Input | Max Accumulated Input |
|---|---|---|---|
| `deepseek-v4-pro` | 278 | 544K | 1,118K |
| `deepseek-v4-flash` | 162 | 345K | 888K |

Both support 1M+ windows, so the system was built with **no budget discipline** —
which is the problem, not the window size itself.

---

## What Actually Has To Fit (per-round peak)

| Component | Sent when | Approx tokens | Bounded today? |
|---|---|---|---|
| Tool schemas (`active_tools`, full JSON) | every round | ~12K (58 tools) | ❌ ~all agents get ~all tools |
| Static base (`build_agent_context`/`build_brain_context`) | every round | ~8–40K | ⚠️ partly (registry cap, terminal-status filter) |
| Tool-doc lines (short form, in system) | every round | ~1.2K | ✅ |
| Task prompt + input_data | every round | ~0.5–1K | ✅ (`[:3000]`) |
| Data-availability menu | every round | ~1–5K | ⚠️ |
| **Accumulated message history** | grows each round | **~2–4K × rounds** | ❌ **no eviction** |

The last row decides whether the heavy agents fit, and it is the one earlier
trimming proposals ignored.

---

## Per-Agent Feasibility (estimated peak window)

| Agent | Peak window (est.) | 64K by trimming alone? | What it needs |
|---|---|---|---|
| Risk Manager | ~40–80K | ✅ likely | Static trims (Tasks 3–4) |
| Simulation / "Others" | ~30–70K | ✅ likely | Static trims |
| Quant Researcher | ~80–180K | ❌ | Trims **+ in-loop eviction** (Task 1) |
| Strategy Developer | ~150–300K | ❌ | Trims + eviction + tool filtering + fewer rounds |
| Brain | ~120–250K | ❌ | Trims + eviction; heaviest static base |

Estimates, because only the accumulated sum is recorded today. **Task 0 replaces
these with measured peaks.** 64K is a stretch for heavy agents; **128K is the
realistic supported floor.**

---

## Measured findings (2026-06-30 soak) — these re-scoped the plan

Task 0 instrumentation ran against live traffic. The peaks came in **far lower
than the original estimates**, and the breakdown showed where the tokens actually
are. This invalidated half the original plan:

| Agent | N | peak P50 | peak P90 | peak P99 / max | rounds P50 | accum P50 |
|---|---|---|---|---|---|---|
| Strategy Developer | 17 | 25,028 | 41,634 | **54,794** | 7 | 135,400 |
| Quant Researcher | 3 | 22,771 | 50,479 | 50,479 | 12 | 197,994 |
| Full-Stack Engineer | 1 | 28,883 | — | 28,883 | 16 | 315,977 |

Per-round composition (Strategy Developer, the heaviest):

| Component | Tokens | Reality vs original estimate |
|---|---|---|
| Static base (`build_agent_context`) | ~4.1K | est. 8–40K → **already lean** |
| Tool schemas (40 tools) | ~6.3K | est. ~12K (58 tools) → **already filtered** |
| Tool-doc lines + prompt + data menu | ~3K | — |
| **Fixed subtotal** | **~13K** | — |
| **Accumulated tool-result history** | **up to ~42K** | **~75% of the peak — the real driver** |

Consequences:
- **Everything already fits 64K** at the *input* level (max measured peak 54.8K).
  The only overflow risk is input + output sharing the window (54.8K + ~11K output
  ≈ 66K). So the goal narrowed to **buying ~15–20K of headroom**, not "fitting".
- **Task 2 (tool filtering) and Task 3 (static-base cap) were dropped** — their
  premises (12K tools, 8–40K base) were wrong; the real numbers are 6.3K and 4.1K,
  so together they'd save ~4K of the *fixed 13%* while risking breakage. Not worth it.
- **Task 5 (local_mode profile + CI guard) was dropped** — replaced by a single
  env-configurable budget constant (the one useful nugget), no CI guard.

## What was implemented

### ✅ Task 0 — Measure the peak, not the sum  *(done)*

- `_call_with_tools_single._accum` now tracks `peak_input_tokens` (max single-round
  input) and `rounds`, carried in `total_usage`.
- `runner.py::_record_context_budget_measurement` appends one JSONL row per task to
  `$AXIOM_HOME/workspace/context_budget.jsonl`.
- `scripts/context_budget_report.py` reads it (from the container by default) and
  prints per-agent peak percentiles + a window fit-check.

### ✅ Task 4 — Reduce `MAX_TOOL_ROUNDS` 25 → 12  *(done)*

- `tool_definitions.py`: `MAX_TOOL_ROUNDS` default **12**, override with
  `AXIOM_MAX_TOOL_ROUNDS`. Caps the 13–25-round tail that produced the 54.8K P99.

### ✅ Task 1 — Bound the in-loop history (eviction)  *(done)*

- `tool_definitions.py`: `CONTEXT_INPUT_BUDGET_TOKENS` (env `AXIOM_CONTEXT_INPUT_BUDGET`),
  **0 = disabled** (cloud default, behavior unchanged).
- `runner.py::_evict_tool_history` runs before each `impl.call`: when
  `system + tools + history` exceeds the budget, it replaces the **oldest**
  tool-result payloads with a stub, **oldest-first**, keeping the last 4 results,
  the task prompt, and all assistant turns. It **never removes a message**, so
  tool-call/result id pairing stays intact (no provider 400s). Works for both
  OpenAI (`role:"tool"`) and Anthropic (`tool_result` block) message shapes.
- This also removes stale-context noise — the "poisoning the reasoning" concern —
  not just tokens.

### ❌ Dropped: Task 2, Task 3, Task 5

See "Measured findings" above for the data that made them low-ROI.

## How to run on a bounded local model

1. Point the agents at the local model (Settings → Agents / routing).
2. Set the budget in `docker-compose.yml` (backend service, already stubbed):
   - 64K window: `AXIOM_CONTEXT_INPUT_BUDGET=46000` (≈18K headroom for output)
   - 128K window: `AXIOM_CONTEXT_INPUT_BUDGET=100000`
3. Restart the backend. Leave it unset for cloud (eviction off).
4. Re-check with `python scripts/context_budget_report.py`.

## Definition of Done

Runs on a 64K local model with no round overflowing the window: peak input held
under the configured budget by eviction, rounds capped at 12, ~18K reserved for
output. 128K is comfortable with margin. Eviction is off by default so cloud
behavior is unchanged, and the same knobs lower cloud cost when enabled.

---

## File Reference

- Tool loop / where eviction goes: `axiom/agents/runner.py::_call_with_tools_single`
- Per-round usage accounting: `_call_with_tools_single._accum`
- Tool-result cap (5000 chars): `axiom/agents/runner.py` (`str(result)[:5000]`)
- `MAX_TOOL_ROUNDS` (25): `axiom/agents/tool_definitions.py`
- Static base builders: `axiom/context.py::build_agent_context` / `build_brain_context`
- Research base builder: `axiom/research_context.py::build_research_context`
- Tool filtering + permissions: `axiom/agents/tool_registry.py::get_tools_for_agent` (+ `permissions=` on each `@register_tool`)
- ChromaDB recall: `axiom/context.py::_get_chroma_recall`
- Strategy registry cap: `axiom/context.py::_format_strategy_registry` (`_REGISTRY_MAX_ROWS`)
