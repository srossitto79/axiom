# Agents & Model Control Overhaul — Plan

## Context (why this is needed)

Choosing models in Forven is confusing and **unsafe**: the bot routinely calls
providers/models the operator never selected — sometimes spending real API
credits (you watched OpenRouter silently fall back to paid `gpt-4o-mini`, and
Gemini route to models you didn't pick). Configuration is also scattered across
three overlapping surfaces (Settings→Models, Settings→Agents, the /agents Hub),
fallbacks aren't user-editable, the per‑agent SOUL.md/AGENTS.md/ROLE.md identity
files went missing, and provider failures are effectively invisible outside the
dashboard.

A 6‑agent research pass (verified by an adversarial critic) confirmed the system
is **fail‑OPEN**. Root cause: **"credentials present" is conflated with "the user
explicitly chose this model,"** and hardcoded paid models (`openai/gpt-5.2`,
`openrouter` aux models) are injected into defaults, fallback chains, auxiliary
routing, and the `normalize → openai` / `get_provider → OpenAIProvider` paths at
every layer. Credential filters are best‑effort and several call sites bypass
them. The network boundary (`get_token`) blocks a *fully unconfigured* provider,
so the real, exploitable hole is a **credentialed‑but‑unselected model** (incl. a
provider whose key is only a stray env var).

### Decisions locked with the operator
1. **Require in‑app connect** — an env‑var key alone never authorizes spend. A
   provider must be explicitly *connected* in the app to be usable.
2. **Hard‑stop + loud alert on failure** — when a selected model's provider
   fails (invalid key / quota / down) and no fallback is configured, pause the
   affected agent and raise a red, impossible‑to‑miss alert. Never silently
   switch to a model the user didn't pick.
3. **Per‑agent SOUL & AGENTS** — every sub‑agent gets its own SOUL.md and
   AGENTS.md (plus ROLE.md), not shared global files.

## The invariant (the whole plan serves this)

> **The bot only ever issues an LLM request for a `(provider, model)` pair that
> the user has (a) explicitly connected AND (b) explicitly selected for that
> slot. If no such pair exists for a needed slot, it fails closed and surfaces
> the problem loudly — it never substitutes a default.**

"Slot" = primary/Brain · each agent · each auxiliary task kind · each backup.

---

## Phase 1 — Stop inadvertent spend (backend safety core)

Goal: make the invariant true by construction. This is the priority — it
protects the user regardless of the UI work.

**1.1 First‑class "connected provider" record.** Add an explicit in‑app
connection flag per provider, distinct from raw token presence. Env‑var keys do
NOT set it. `credential_status()`/`_provider_has_credentials` get a companion
`provider_is_connected(provider)` that requires the in‑app record. Files:
`forven/auth/store.py` (env keys at lines 38–46, `credential_status` ~496),
`forven/api_core.py` (the auth-provider Test/Save path that becomes "Connect").

**1.2 First‑class per‑slot selection.** Persist the explicit `(provider, model)`
the user chose for each slot. Stop treating `_DEFAULT_MODEL_ROUTING` /
`_DEFAULT_AUXILIARY_ROUTING` as *live routing answers* — demote them to UI
placeholder catalog only. `get_default_model_for_provider`,
`get_primary_provider_model`, `get_fallback_chain`, `get_auxiliary_routing`
return the user's selection or `None` — never a hardcoded model id. File:
`forven/model_routing.py` (defaults at 55–157; selectors at 361–539).

**1.3 One central resolver.** `resolve_route(slot) -> list[(provider, model)]`
returning ONLY pairs that are connected (1.1) AND selected (1.2), in order
[primary, then user‑defined fallbacks]. Empty → raise typed
`UnconfiguredRouteError(slot)`. Every inference entry point obtains its chain
only from here. New code in `forven/ai.py` / `forven/model_routing.py`.

**1.4 Remove the fail‑open‑to‑OpenAI branches.**
- `normalize_provider_and_model` (`ai.py:212-218, 441-499`) returns a
  sentinel/None for blank/unknown provider — no fabricated `('openai','gpt-5.2')`.
- `get_provider` (`agents/providers.py:1024-1025`) raises on unknown provider
  instead of returning `OpenAIProvider()`.

**1.5 Fallbacks are opt‑in and explicit.** Remove the forced `openai/minimax`
appends in `get_fallback_chain` (`model_routing.py:514-528`) and the
`gemini/openai` inserts in the openrouter/groq/gemini default chains. A
provider's default chain contains only itself. An empty user fallback list means
**no fallback** (fail closed), never the hardcoded chain.

**1.6 Lowest‑level chokepoint (last line of defense).** In `ai._call_single`
and each `providers.py` adapter `.call`/`.stream`, assert
`provider_is_connected(provider)` AND `(provider, model)` ∈ selection set BEFORE
building the HTTP request; raise `CredentialError`/`UnconfiguredRouteError`
otherwise. This closes the credentialed‑but‑unselected‑model gap that
`get_token` alone does not. Also reconcile the provider matrix —
`_call_single` raises `ValueError` for anthropic/deepseek that the tool‑call
path supports (`ai.py:822-905` vs `agents/providers.py`).

**1.7 Fix the concrete unattended offenders** (route via resolver, `fallback`
restricted to user‑selected backups, skip with a logged "no LLM configured" when
empty):
- `jobs/daily_learning.py:57-61` (hardcodes `openai/gpt-4o-mini`, `fallback=True`, on a daily cron)
- `quant_skills_extractor.py:134-147` (`call_ai_sync` default `fallback=True`)
- `hypothesis_verdict.py:46-58`, `strategy_extrapolation.py:38-47` (autonomous, `fallback=True`, no pre‑check)
- `deepdive_session.py:168-227` (no credential pre‑check; degrade path `fallback=True`)
- `assistant_session.py:188`, `bot.py:1473` (chat degrade `fallback=True`)
- `bot_factory/engine.py:368` (`provider='auto'`)

**1.8 Auxiliary routing fail‑closed + fix the clobber bug.** Default each unset
aux kind to the user's PRIMARY selection (not `openrouter`); callers
(`recall.py`, `control_plane/smart_approval.py`, `quant_skills_extractor.py`)
skip the feature when unconfigured. **Fix the bug**: a `/api/model-policy` save
currently resets `auxiliary` back to the openrouter defaults —
`_coerce_model_policy_update_payload` (`api_core.py:1174-1182`) omits the
`auxiliary` key, so `_coerce_model_routing` re‑seeds it. Carry `auxiliary`
forward on every policy save.

**1.9 Make the persisted policy sane.** Make `get_model_routing()` a pure read —
move the zai‑priority migration (`model_routing.py:344-352`) out of the getter
into a one‑shot startup migration so reads never mutate the KV blob. Add an
admin **"reset routing to defaults"** action. (Once defaults are fail‑closed
sentinels with no hardcoded model ids, a persisted blob can no longer pin paid
models.) Note for current systems: the persisted KV `forven:model-routing`
overrides code defaults per‑key, which is why earlier code‑default edits didn't
take effect live.

**1.10 Hard‑stop on unconfigured/failed slot.** When `resolve_route` returns
empty, or a selected provider is auth‑invalid/quota‑exhausted with no configured
fallback: pause the affected agent (don't silently requeue forever) and emit a
CRITICAL alert (ties into Phase 2). Never substitute.

---

## Phase 2 — Make failures impossible to miss (loud surfacing)

Today runtime provider failures go only to the Python logger; silent fallbacks
log at `warning`; `health_monitor` has **no** provider check so the red
`CriticalAlertsBanner` can never light for a provider; and the rich alert panels
are dashboard‑only. Only `AgentProviderBanner` is global, and it sees only the
static "no credentials at all" case.

**2.1 Runtime provider‑health store.** A small KV store
`forven:provider-health:{provider}` = `{state: ok|degraded|down, kind:
rate_limit|quota|auth|transient|fallback, reason, last_error_at, last_ok_at,
fallback_to}`. Written from the runner's classify branches
(`agents/runner.py:1370-1456`) and from fallback/retarget sites
(`ai.py:791-792`, `runner.py:221-247, 1128-1137`) — one write per failure kind,
keyed by the ACTUAL provider used (fixes the wrong‑provider/`unknown` labeling at
`runner.py:1393,544`). Classify on provider identity, NOT the broad
`is_transient_provider_exception` bucket (which folds in SQLite locks).

**2.2 Health‑monitor provider check.** Add `check_ai_providers()` to the check
loop (`health_monitor.py:1347`). RED when a *selected* provider is
auth‑invalid/quota‑exhausted (→ `_dispatch_alerts` fires a CRITICAL alert →
`/api/health/alerts` → `CriticalAlertsBanner` lights, reusing existing
machinery), AMBER for sustained rate‑limit/active fallback.

**2.3 Periodic re‑verification.** On a slow cadence (~10–15 min, gated by
`autonomous_runtime_allowed()`), call the existing `_verify_provider_key`
(`api_core.py:7064`) for each connected provider and write the result into the
health store — so a key that goes invalid mid‑run is caught before a task fails,
instead of showing false‑green.

**2.4 Global ConnectionHealthBanner.** New component mounted in
`frontend/src/routes/+layout.svelte` next to `AgentProviderBanner` (so it shows
on EVERY page), driven by a new `GET /api/agents/provider-health-runtime` (or an
extension of the existing endpoint). **Red, non‑dismissible** for auth/quota
(operator must act, per decision #2); **amber, dismissible** for
rate‑limit/active fallback. Show substitution explicitly: *"strategy‑developer
is paused — gemini-2.5-flash-lite failed (quota); no fallback configured."*
Plus a one‑shot error Toast on an ok→down transition. Promote the quota alert
from `warning` to `error`.

---

## Phase 3 — Unified Agents control page (UI consolidation)

Consolidate the three surfaces into one tabbed page at `/agents` (the user only
configures models in one place; **only connected + enabled models are ever
selectable**). Tabs, left→right in dependency order:

1. **Roster** (default) — current Hub: agent cards with a *validated* per‑agent
   model dropdown (reuse `optionsForAgent` at `/agents/+page.svelte:436-451`),
   add/edit/remove developers, KPIs, task queue, terminal modal. A per‑agent
   **detail drawer** for role/instructions/SOUL.md/AGENTS.md/ROLE.md (now
   per‑agent — Phase 4) + discord token. **Retire the free‑text model inputs**
   in the old personas editor (`SettingsAgents.svelte:980-994`).
2. **Providers & Keys** — lift the AI‑providers + OAuth block verbatim
   (`SettingsAgents.svelte:533-829, 375-512`). The **"Connect"** action sets the
   in‑app connection record from Phase 1.1; keeps Test/Disconnect.
3. **Models** — the enable checkboxes (`agent_model_keys`) grouped by provider +
   refresh. This list gates selectability everywhere downstream.
4. **Routing & Fallbacks** (NEW) — per‑slot selection (primary/Brain, each of
   the 5 aux kinds, backup) and a per‑slot **ordered, opt‑in fallback list**
   constrained to connected+enabled models. Wires the currently‑orphaned
   `PUT /api/model-policy` and the aux endpoint. Empty fallback = fail closed.
5. **Schedules** — one scheduler editor (reuse `SchedulerJobRow`); drop the
   duplicate in `SettingsAgents`.
6. **Health** (NEW) — static `provider-health` warnings + the runtime health
   store (Phase 2) + key re‑verification status + one‑click reconcile.

Navigation: `/agents?tab=…`. In `manifest.ts` drop the standalone `models` area
and `agents-*` subsections (or repoint deep links); `settings/+page.svelte`
renders a thin "Configuration moved to Agents →" redirect for `agents`/`models`.
Hoist model discovery / provider status / model‑policy / enabled‑keys into shared
Svelte stores loaded once at page level (replacing 3 independent fetch+cache
copies). Reuse vs build: REUSE the providers+OAuth block, enable‑checkbox grid,
`SchedulerJobRow`, Hub roster/terminal; BUILD the Routing & Fallbacks editor and
Health tab.

---

## Phase 4 — Restore per‑agent SOUL.md / AGENTS.md / ROLE.md

Root cause (verified, not a code deletion): `init_workspace()` — the only seeder
of the identity files — is never called on the default **API‑owned** runtime
(`FORVEN_BOT_OWNS_RUNTIME` unset; the Discord bot's gateway‑only branch at
`bot.py:774-780` skips `_bootstrap()`). Today SOUL.md/AGENTS.md are GLOBAL
(workspace root) and missing on disk; only per‑agent ROLE.md exists.

Per decision #3, move to **per‑agent** SOUL/AGENTS:
- **Seed on startup**: call `init_workspace()` from `api.py` lifespan (~line 304,
  before agent seeding; idempotent) so global IDENTITY.md and templates exist.
- **Per‑agent files**: extend `create_agent` (`agents/manager.py:61`) and
  `seed_default_agents` (`bot.py:687-705`) to write `agents/<id>/SOUL.md`,
  `agents/<id>/AGENTS.md`, `agents/<id>/ROLE.md` from templates (personalized by
  role). Make seeding **self‑healing**: re‑write any missing per‑agent file for
  existing DB agents on startup (today `update_agent` never writes them).
- **Consumption**: update `_build_agent_documents` (`api_core.py:1222-1240`) and
  `put_agent_document` (`api_core.py:7319-7335`) to read/write the per‑agent
  SOUL/AGENTS/ROLE; update `build_agent_context` (`context.py:401`) to inject the
  per‑agent SOUL + AGENTS (keep global IDENTITY.md for mission/risk).
- **UI**: the Roster detail drawer (Phase 3) edits all three per‑agent docs with
  correct labels (the current UI mislabels global files as per‑agent).
- **One‑time remediation** for this install: seed the per‑agent files for the 7
  built‑ins from templates without overwriting.

---

## Sequencing

- **Phase 1 first** (stops the bleeding; can ship before any UI). 
- **Phase 2** alongside/after 1 (surfaces the new fail‑closed behavior).
- **Phase 4** is independent and small — can land early.
- **Phase 3** last (exposes the new config; depends on 1–2 backend shapes).
- Quick wins to land immediately within Phase 1: the **aux‑clobber bug** (1.8),
  `fallback=False` on the autonomous offenders (1.7), and the **workspace
  seeder** (Phase 4 seeding).

## Verification

**Invariant test suite (the proof):**
1. With NO provider connected → every inference entry point raises
   `UnconfiguredRouteError`/`CredentialError` and **zero outbound HTTP calls**
   are attempted (mock `httpx`, assert no calls).
2. With exactly one provider+model connected/selected → assert no other
   provider/model is ever contacted across all entry points and all 5 aux kinds,
   including under fallback **and with a stray env var set** for an unselected
   provider.
3. Saving `/api/model-policy` does NOT reset `auxiliary` (regression for the
   clobber bug).
4. Startup seeding creates global IDENTITY.md + per‑agent SOUL/AGENTS/ROLE.md for
   all 7 built‑ins (temp `FORVEN_HOME`).

**Manual / live:** connect only one provider; confirm agents run only on it and
nothing else is billed (watch the provider dashboards). Pull a key mid‑run →
the global banner lights red and the agent pauses (no silent switch). Configure
an explicit fallback → confirm it's used; remove it → confirm hard‑stop. Open
the Agents page → all config reachable via tabs, only connected+enabled models
selectable, SOUL/AGENTS/ROLE populated per agent.

## Key files (by workstream)

- **Routing/safety:** `forven/model_routing.py`, `forven/ai.py`,
  `forven/agents/runner.py`, `forven/agents/providers.py`, `forven/auth/store.py`,
  `forven/api_core.py`; offenders in `jobs/daily_learning.py`,
  `quant_skills_extractor.py`, `hypothesis_verdict.py`,
  `strategy_extrapolation.py`, `deepdive_session.py`, `assistant_session.py`,
  `bot.py`, `bot_factory/engine.py`, `recall.py`, `control_plane/smart_approval.py`.
- **Surfacing:** `forven/health_monitor.py`, `forven/routers/health.py`,
  `forven/agents/provider_health.py`, `frontend/.../+layout.svelte`,
  `AgentProviderBanner.svelte`, `CriticalAlertsBanner.svelte`, `Toast.svelte`.
- **UI:** `frontend/src/routes/agents/+page.svelte`,
  `SettingsAgents.svelte`, `SettingsModels.svelte`, `settings/manifest.ts`,
  `settings/+page.svelte`, `routers/agents.py`, `routers/brain.py`.
- **Identity files:** `forven/workspace.py`, `forven/agents/manager.py`,
  `forven/bot.py`, `forven/api.py`, `forven/context.py`, `forven/api_core.py`,
  `templates/workspace/*.md`.
