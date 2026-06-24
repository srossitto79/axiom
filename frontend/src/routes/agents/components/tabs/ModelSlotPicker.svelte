<script lang="ts">
	/**
	 * A constrained provider+model picker plus an ordered fallback list, used by
	 * every routing slot in the Routing & Fallbacks tab.
	 *
	 * Hard constraint (the page-wide safety invariant): the only options ever
	 * offered are CONNECTED providers and ENABLED models. The caller passes the
	 * already-filtered `selectable` options. An empty fallback list explicitly
	 * means "no fallback (fail closed)".
	 */
	import type { ForvenAgentModelOption } from '$lib/api';
	import { createEventDispatcher } from 'svelte';
	import ModelPicker from './ModelPicker.svelte';

	export let label: string;
	export let description = '';
	/** Primary choice as a "provider:model_id" key, or '' for unset. */
	export let value: string;
	/** Ordered fallback list, each a "provider:model_id" key. */
	export let fallbacks: string[] = [];
	/** Connected + enabled options only. */
	export let selectable: ForvenAgentModelOption[] = [];
	export let allowUnset = false;
	export let unsetLabel = '— select —';
	export let dirty = false;

	const dispatch = createEventDispatcher<{
		change: { value: string; fallbacks: string[] };
	}>();

	let addDraft = '';

	function emit() {
		dispatch('change', { value, fallbacks: [...fallbacks] });
	}

	function onPrimaryChange(next: string) {
		value = next;
		emit();
	}

	function handlePrimaryChange(e: CustomEvent<{ value: string }>) {
		onPrimaryChange(e.detail.value);
	}

	function addFallback() {
		const k = addDraft.trim();
		if (!k) return;
		fallbacks = [...fallbacks, k];
		addDraft = '';
		emit();
	}

	function removeFallback(idx: number) {
		fallbacks = fallbacks.filter((_, i) => i !== idx);
		emit();
	}

	function moveFallback(idx: number, dir: -1 | 1) {
		const target = idx + dir;
		if (target < 0 || target >= fallbacks.length) return;
		const next = [...fallbacks];
		[next[idx], next[target]] = [next[target], next[idx]];
		fallbacks = next;
		emit();
	}

	function labelForKey(key: string): string {
		const found = selectable.find((o) => o.key === key);
		if (found) return found.label;
		const sep = key.indexOf(':');
		return sep > 0 ? `${key.slice(0, sep)} / ${key.slice(sep + 1)}` : key;
	}

	function isStale(key: string): boolean {
		// A key that's no longer in the selectable set = its provider was
		// disconnected or its model disabled. Flag it so the operator notices.
		return Boolean(key) && !selectable.some((o) => o.key === key);
	}
</script>

<li class="bg-black border border-gray-800 rounded p-4 space-y-3">
	<div class="flex items-start justify-between gap-3">
		<div>
			<h3 class="text-sm font-semibold text-white">
				{label}
				{#if dirty}
					<span class="ml-2 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-amber-800 text-amber-300 bg-amber-950/40">unsaved</span>
				{/if}
			</h3>
			{#if description}
				<p class="text-xs text-gray-500 mt-0.5">{description}</p>
			{/if}
		</div>
	</div>

	<div class="block text-xs text-gray-400">
		<span class="block mb-1">Primary model</span>
		<ModelPicker
			{value}
			{selectable}
			{allowUnset}
			{unsetLabel}
			on:change={handlePrimaryChange}
		/>
	</div>

	<div class="space-y-2">
		<div class="text-xs text-gray-400 flex items-center justify-between">
			<span>Fallback chain</span>
			{#if fallbacks.length === 0}
				<span class="text-[10px] uppercase tracking-wider text-amber-400/90" title="With no fallback the call fails closed instead of silently switching providers.">no fallback · fail closed</span>
			{/if}
		</div>

		{#if fallbacks.length > 0}
			<ol class="space-y-1">
				{#each fallbacks as fb, idx (idx)}
					<li class="flex items-center gap-2 bg-gray-950 border border-gray-800 rounded px-2 py-1.5">
						<span class="text-[10px] text-gray-600 w-5 text-center">{idx + 1}</span>
						<span class="flex-1 font-mono text-xs {isStale(fb) ? 'text-amber-300' : 'text-gray-200'}">
							{labelForKey(fb)}{isStale(fb) ? ' (unavailable)' : ''}
						</span>
						<button
							type="button"
							class="text-gray-500 hover:text-white disabled:opacity-30 px-1"
							aria-label="Move up"
							disabled={idx === 0}
							on:click={() => moveFallback(idx, -1)}
						>↑</button>
						<button
							type="button"
							class="text-gray-500 hover:text-white disabled:opacity-30 px-1"
							aria-label="Move down"
							disabled={idx === fallbacks.length - 1}
							on:click={() => moveFallback(idx, 1)}
						>↓</button>
						<button
							type="button"
							class="text-red-400 hover:text-red-300 px-1"
							aria-label="Remove fallback"
							on:click={() => removeFallback(idx)}
						>✕</button>
					</li>
				{/each}
			</ol>
		{/if}

		<div class="flex items-end gap-2">
			<label class="flex-1 block text-[10px] text-gray-500 uppercase tracking-wider">
				Add fallback
				<select
					bind:value={addDraft}
					class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
				>
					<option value="">— pick a connected, enabled model —</option>
					{#each selectable as opt (opt.key)}
						<option value={opt.key}>{opt.label}</option>
					{/each}
				</select>
			</label>
			<button
				type="button"
				on:click={addFallback}
				disabled={!addDraft}
				class="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-200 hover:text-white hover:border-gray-500 disabled:opacity-50"
			>
				Add
			</button>
		</div>
	</div>
</li>
