<script lang="ts">
	/**
	 * Settings → Models (P1-T18)
	 *
	 * Four auxiliary slots — compression / recall / skill_extraction /
	 * post_mortem — each pinned to a {provider, model_id} pair with optional
	 * base_url + api_key overrides. Backed by `/api/brain/auxiliary` (GET/PUT).
	 */
	import { onMount } from 'svelte';
	import {
		getBrainAuxiliary,
		updateBrainAuxiliary,
		getForvenAuthProviders,
		getForvenAgentModelOptions,
		updateSettingsSection,
		getSettings,
		type BrainAuxiliaryEntry,
		type BrainAuxiliaryTaskKind,
		type ForvenAuthProviderStatus,
		type ForvenAgentModelOption,
	} from '$lib/api';

	export let settings: Record<string, unknown> = {};

	// Backup AI provider (wired setting). When an agent's primary provider's
	// credentials become unusable, the call falls back to this provider instead of
	// failing; 'none' disables fallback (the routine is then paused + an alert raised).
	const BACKUP_PROVIDER_CHOICES = ['openai', 'minimax', 'zai', 'lmstudio', 'groq', 'gemini'];
	// Seed from the parent prop for first paint; load() then refreshes it from the
	// LIVE settings so a revisit reflects the persisted choice (the parent's prop is
	// loaded once and our save doesn't update it).
	let backupProvider = String((settings.backup_ai_provider as string) ?? 'none');
	let backupModel = String((settings.backup_ai_model as string) ?? '');
	let backupSaving = false;
	let backupError: string | null = null;
	let backupSaved = false;

	// Reactive set of configured providers. Derived directly from `providers` so the
	// labels update when load() populates it (a `providerConfigured(name)` helper would
	// hide the `providers` dependency from Svelte and never refresh).
	$: configuredProviders = new Set(
		providers.filter((p) => p.configured).map((p) => p.provider as string)
	);
	// Models offered for the selected backup provider (reactive on modelOptions + provider).
	$: backupModels =
		backupProvider === 'none' ? [] : modelOptions.filter((m) => m.provider === backupProvider);

	async function saveBackup(prevProvider: string, prevModel: string) {
		backupSaving = true;
		backupError = null;
		backupSaved = false;
		try {
			await updateSettingsSection('agents', {
				backup_ai_provider: backupProvider,
				backup_ai_model: backupModel,
			});
			backupSaved = true;
			setTimeout(() => (backupSaved = false), 3000);
		} catch (e) {
			backupProvider = prevProvider; // roll back the optimistic selection on failure
			backupModel = prevModel;
			backupError = e instanceof Error ? e.message : 'Failed to save backup provider';
		} finally {
			backupSaving = false;
		}
	}

	function onBackupProviderChange(value: string) {
		const pp = backupProvider;
		const pm = backupModel;
		backupProvider = value;
		backupModel = ''; // reset to the new provider's default; avoids a cross-provider model
		void saveBackup(pp, pm);
	}

	function onBackupModelChange(value: string) {
		const pp = backupProvider;
		const pm = backupModel;
		backupModel = value;
		void saveBackup(pp, pm);
	}

	const TASK_KINDS: BrainAuxiliaryTaskKind[] = [
		'recall',
		'skill_extraction',
	];

	const TASK_LABELS: Partial<Record<BrainAuxiliaryTaskKind, string>> = {
		recall: 'Recall',
		skill_extraction: 'Skill extraction',
	};

	const TASK_DESCRIPTIONS: Partial<Record<BrainAuxiliaryTaskKind, string>> = {
		recall: 'Re-ranks FTS5 hits and writes the recall summary on /brain/recall.',
		skill_extraction: 'Distills successful trade patterns into reusable skill cards. Stronger reasoning.',
	};

	// Mirrors forven/model_routing.py:_DEFAULT_AUXILIARY_ROUTING — used by the
	// per-row "Reset to default" button. Keep in lockstep with the backend seed.
	const DEFAULTS: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>> = {
		recall: {
			provider: 'openrouter',
			model_id: 'openai/gpt-4o-mini',
			base_url: null,
			api_key: null,
		},
		skill_extraction: {
			provider: 'openrouter',
			model_id: 'anthropic/claude-3-5-sonnet',
			base_url: null,
			api_key: null,
		},
	};

	type RowDraft = {
		provider: string;
		model_id: string;
		base_url: string;
		api_key: string;
	};

	function entryToDraft(entry: BrainAuxiliaryEntry | null | undefined): RowDraft {
		return {
			provider: (entry?.provider ?? '') as string,
			model_id: (entry?.model_id ?? '') as string,
			base_url: (entry?.base_url ?? '') as string,
			api_key: (entry?.api_key ?? '') as string,
		};
	}

	function draftToEntry(draft: RowDraft): BrainAuxiliaryEntry {
		const trim = (s: string) => s.trim();
		return {
			provider: trim(draft.provider) || null,
			model_id: trim(draft.model_id) || null,
			base_url: trim(draft.base_url) || null,
			api_key: trim(draft.api_key) || null,
		};
	}

	type DraftMap = Partial<Record<BrainAuxiliaryTaskKind, RowDraft>>;

	function emptyDraftMap(): DraftMap {
		const m: DraftMap = {};
		for (const kind of TASK_KINDS) m[kind] = entryToDraft(null);
		return m;
	}

	function cloneDraftMap(src: DraftMap): DraftMap {
		const m: DraftMap = {};
		for (const kind of TASK_KINDS) m[kind] = { ...(src[kind] ?? entryToDraft(null)) };
		return m;
	}

	let originalDrafts: DraftMap = emptyDraftMap();
	let pendingDrafts: DraftMap = emptyDraftMap();

	let providers: ForvenAuthProviderStatus[] = [];
	let modelOptions: ForvenAgentModelOption[] = [];

	let loading = true;
	let saving = false;
	let loadError: string | null = null;
	let saveError: string | null = null;
	let saveMessage: string | null = null;

	$: dirtyKinds = TASK_KINDS.filter((kind) => isRowDirty(kind));
	$: isDirty = dirtyKinds.length > 0;

	function isRowDirty(kind: BrainAuxiliaryTaskKind): boolean {
		const o = originalDrafts[kind] ?? entryToDraft(null);
		const p = pendingDrafts[kind] ?? entryToDraft(null);
		return (
			o.provider !== p.provider ||
			o.model_id !== p.model_id ||
			o.base_url !== p.base_url ||
			o.api_key !== p.api_key
		);
	}

	function modelsForProvider(provider: string): ForvenAgentModelOption[] {
		if (!provider) return [];
		return modelOptions.filter((m) => m.provider === provider);
	}

	async function load() {
		loading = true;
		loadError = null;
		try {
			const [aux, authRes, modelRes] = await Promise.all([
				getBrainAuxiliary(),
				getForvenAuthProviders(),
				getForvenAgentModelOptions(false),
			]);
			providers = authRes.providers ?? [];
			modelOptions = modelRes.options ?? [];
			const next: DraftMap = { ...originalDrafts };
			for (const kind of TASK_KINDS) {
				next[kind] = entryToDraft(aux.auxiliary[kind]);
			}
			originalDrafts = next;
			pendingDrafts = cloneDraftMap(next);
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Failed to load auxiliary models';
		} finally {
			loading = false;
		}
		// Refresh the backup provider from LIVE settings (best-effort + independent of
		// the auxiliary-model load above) so a revisit reflects the persisted value
		// rather than the parent's once-loaded, possibly-stale `settings` prop.
		try {
			const live = await getSettings();
			backupProvider = String(live.backup_ai_provider ?? 'none');
			backupModel = String(live.backup_ai_model ?? '');
		} catch {
			// keep the prop-seeded value
		}
	}

	function setProvider(kind: BrainAuxiliaryTaskKind, provider: string) {
		const next = { ...(pendingDrafts[kind] ?? entryToDraft(null)), provider };
		// Clear model_id if it doesn't belong to the new provider's enumerated set.
		const valid = modelsForProvider(provider).some((m) => m.model_id === next.model_id);
		if (!valid) next.model_id = '';
		pendingDrafts = { ...pendingDrafts, [kind]: next };
	}

	function setField(
		kind: BrainAuxiliaryTaskKind,
		field: keyof RowDraft,
		value: string,
	) {
		pendingDrafts = {
			...pendingDrafts,
			[kind]: { ...(pendingDrafts[kind] ?? entryToDraft(null)), [field]: value },
		};
	}

	function resetRow(kind: BrainAuxiliaryTaskKind) {
		pendingDrafts = { ...pendingDrafts, [kind]: entryToDraft(DEFAULTS[kind]) };
	}

	function discard() {
		pendingDrafts = cloneDraftMap(originalDrafts);
		saveError = null;
	}

	async function save() {
		if (!isDirty || saving) return;
		saving = true;
		saveError = null;
		saveMessage = null;
		try {
			const payload: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>> = {};
			for (const kind of dirtyKinds) {
				payload[kind] = draftToEntry(pendingDrafts[kind] ?? entryToDraft(null));
			}
			const res = await updateBrainAuxiliary(payload);
			const nextOriginals: DraftMap = { ...originalDrafts };
			for (const kind of TASK_KINDS) {
				nextOriginals[kind] = entryToDraft(res.auxiliary[kind]);
			}
			originalDrafts = nextOriginals;
			pendingDrafts = cloneDraftMap(nextOriginals);
			saveMessage = 'Auxiliary models saved';
			setTimeout(() => (saveMessage = null), 3000);
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Failed to save auxiliary models';
		} finally {
			saving = false;
		}
	}

	onMount(() => {
		void load();
	});
</script>

<div class="space-y-6">
	<section
		aria-labelledby="models-backup-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-3"
	>
		<header class="border-b border-gray-800 pb-2">
			<h2 id="models-backup-heading" class="text-lg font-semibold text-white">
				Backup AI provider
			</h2>
			<p class="text-xs text-gray-500 mt-1">
				If an agent's primary provider's credentials become unusable, fall back to this
				provider instead of failing. <span class="text-gray-400">None</span> disables fallback —
				a credential problem then pauses the affected routine and alerts you.
			</p>
		</header>
		<div class="grid gap-3 sm:grid-cols-2 max-w-xl">
			<label class="block text-xs text-gray-400">
				Fallback provider
				<select
					value={backupProvider}
					on:change={(e) => onBackupProviderChange((e.target as HTMLSelectElement).value)}
					disabled={backupSaving}
					class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
				>
					<option value="none">None (disabled)</option>
					{#each BACKUP_PROVIDER_CHOICES as name (name)}
						<option value={name}>{name}{configuredProviders.has(name) ? '' : ' (not configured)'}</option>
					{/each}
				</select>
			</label>

			{#if backupProvider !== 'none'}
				<label class="block text-xs text-gray-400">
					Fallback model
					{#if backupModels.length > 0}
						<select
							value={backupModel}
							on:change={(e) => onBackupModelChange((e.target as HTMLSelectElement).value)}
							disabled={backupSaving}
							class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
						>
							<option value="">Provider default</option>
							{#each backupModels as opt (opt.key)}
								<option value={opt.model_id}>{opt.label}</option>
							{/each}
							{#if backupModel && !backupModels.some((m) => m.model_id === backupModel)}
								<option value={backupModel}>{backupModel} (custom)</option>
							{/if}
						</select>
					{:else}
						<input
							type="text"
							value={backupModel}
							on:change={(e) => onBackupModelChange((e.target as HTMLInputElement).value)}
							placeholder="leave blank for the provider default"
							class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
						/>
					{/if}
				</label>
			{/if}
		</div>
		{#if backupSaving}<p class="text-xs text-gray-500">Saving…</p>{/if}
		{#if backupSaved}<p class="text-xs text-green-400" role="status">Backup provider saved</p>{/if}
		{#if backupError}<p class="text-xs text-red-400" role="alert">{backupError}</p>{/if}
	</section>

	<section
		aria-labelledby="models-aux-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-4"
	>
		<header class="border-b border-gray-800 pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 id="models-aux-heading" class="text-lg font-semibold text-white">
					Auxiliary models
				</h2>
				<p class="text-xs text-gray-500 mt-1">
					Lightweight LLMs that handle compression, recall summaries, skill extraction, and
					post-mortems — separate from the primary Brain reasoning model. Defaults route via
					OpenRouter; override the base URL and API key per-row to pin to a specific provider.
				</p>
			</div>
			<button
				type="button"
				on:click={() => load()}
				disabled={loading}
				class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 disabled:opacity-60"
			>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
		</header>

		{#if loadError}
			<p class="text-xs text-red-400" role="alert">{loadError}</p>
		{/if}
		{#if saveError}
			<p class="text-xs text-red-400" role="alert">{saveError}</p>
		{/if}
		{#if saveMessage}
			<p class="text-xs text-green-400" role="status">{saveMessage}</p>
		{/if}

		{#if loading}
			<p class="text-sm text-gray-400">Loading auxiliary models…</p>
		{:else}
			<ul class="space-y-3">
				{#each TASK_KINDS as kind (kind)}
					{@const draft = pendingDrafts[kind] ?? entryToDraft(null)}
					{@const rowDirty = isRowDirty(kind)}
					{@const providerModels = modelsForProvider(draft.provider)}
					{@const knownModel =
						providerModels.find((m) => m.model_id === draft.model_id) ?? null}
					<li class="bg-black border border-gray-800 rounded p-4 space-y-3">
						<div class="flex items-start justify-between gap-3">
							<div>
								<h3 class="text-sm font-semibold text-white">
									{TASK_LABELS[kind]}
									{#if rowDirty}
										<span
											class="ml-2 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-amber-800 text-amber-300 bg-amber-950/40"
											>unsaved</span
										>
									{/if}
								</h3>
								<p class="text-xs text-gray-500 mt-0.5">{TASK_DESCRIPTIONS[kind]}</p>
							</div>
							<button
								type="button"
								on:click={() => resetRow(kind)}
								class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500"
							>
								Reset to default
							</button>
						</div>

						<div class="grid gap-3 md:grid-cols-2">
							<label class="block text-xs text-gray-400">
								Provider
								<select
									value={draft.provider}
									on:change={(e) =>
										setProvider(kind, (e.target as HTMLSelectElement).value)}
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
								>
									<option value="">— select —</option>
									{#each providers as p (p.provider)}
										<option value={p.provider}>
											{p.provider}{p.configured ? '' : ' (not configured)'}
										</option>
									{/each}
								</select>
							</label>

							<label class="block text-xs text-gray-400">
								Model ID
								{#if providerModels.length > 0}
									<select
										value={draft.model_id}
										on:change={(e) =>
											setField(kind, 'model_id', (e.target as HTMLSelectElement).value)}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									>
										<option value="">— select —</option>
										{#each providerModels as opt (opt.key)}
											<option value={opt.model_id}>{opt.label}</option>
										{/each}
										{#if draft.model_id && !knownModel}
											<option value={draft.model_id}>{draft.model_id} (custom)</option>
										{/if}
									</select>
								{:else}
									<input
										type="text"
										value={draft.model_id}
										on:input={(e) =>
											setField(kind, 'model_id', (e.target as HTMLInputElement).value)}
										placeholder="e.g. openai/gpt-4o-mini"
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									/>
								{/if}
							</label>

							<label class="block text-xs text-gray-400">
								Base URL override <span class="text-gray-600">(optional)</span>
								<input
									type="text"
									value={draft.base_url}
									on:input={(e) =>
										setField(kind, 'base_url', (e.target as HTMLInputElement).value)}
									placeholder="leave blank to use provider default"
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
								/>
							</label>

							<label class="block text-xs text-gray-400">
								API key override <span class="text-gray-600">(optional)</span>
								<input
									type="password"
									value={draft.api_key}
									on:input={(e) =>
										setField(kind, 'api_key', (e.target as HTMLInputElement).value)}
									placeholder="leave blank to use provider default"
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
								/>
							</label>
						</div>

						{#if knownModel}
							<p class="text-xs text-gray-500">
								Cost: <span class="text-gray-400">—</span>
								<span class="text-gray-600">(per-1k pricing not yet exposed)</span>
							</p>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	{#if !loading && isDirty}
		<div
			class="sticky bottom-4 z-10 flex items-center justify-between gap-3 border border-amber-800 bg-amber-950/30 rounded-lg px-4 py-3"
			role="region"
			aria-label="Unsaved changes"
		>
			<p class="text-sm text-amber-200">
				{dirtyKinds.length} unsaved row{dirtyKinds.length === 1 ? '' : 's'}.
			</p>
			<div class="flex gap-2">
				<button
					type="button"
					on:click={discard}
					disabled={saving}
					class="text-sm px-3 py-1.5 rounded border border-gray-700 text-gray-200 hover:text-white hover:border-gray-500 disabled:opacity-60"
				>
					Discard
				</button>
				<button
					type="button"
					on:click={save}
					disabled={saving}
					class="text-sm px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-60"
				>
					{saving ? 'Saving…' : 'Save'}
				</button>
			</div>
		</div>
	{/if}
</div>
