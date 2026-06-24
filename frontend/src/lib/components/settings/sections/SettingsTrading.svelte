<script lang="ts">
	import { onMount } from 'svelte';
	import {
		SETTINGS_MANIFEST,
		SETTINGS_SUBSECTIONS,
		type SettingsEntry,
	} from '$lib/settings/manifest';
	import SettingsSubsection from '$lib/components/settings/primitives/SettingsSubsection.svelte';
	import SettingsFieldRow from '$lib/components/settings/primitives/SettingsFieldRow.svelte';
	import SettingsAdvancedHeader from '$lib/components/settings/primitives/SettingsAdvancedHeader.svelte';
	import { originalValues, pendingValues } from '$lib/settings/dirty';
	import { openExternal } from '$lib/external-open';

	export let settings: Record<string, unknown>;
	// currentValues is exposed so the parent (Task 20 shell) can read it for the save bar.
	// It is derived reactively from originalValues + pendingValues for this area.
	export let currentValues: Record<string, unknown> = {};
	export let variant: 'default' | 'wizard' = 'default';
	export let visibleSubsections: string[] | null = null;

	const AREA = 'trading' as const;

	// Hyperliquid referral CTA — rendered inside the credentials subsection, which
	// shows up both in the setup wizard's "Trading basics" step and on the full
	// Settings page (the same place the TRADING HALTED banner routes users). Every
	// user needs a Hyperliquid account to trade, so we offer them one — with a 4%
	// fee discount for them — via our referral link.
	const HL_REFERRAL_URL = 'https://app.hyperliquid.xyz/join/FORVEN';
	let referralCopyFallback = false;
	async function openReferral(): Promise<void> {
		// Hand the URL to the system browser via the Tauri opener; window.open is a
		// silent no-op in the packaged shell. Reveal a copy-able fallback on failure.
		referralCopyFallback = !(await openExternal(HL_REFERRAL_URL));
	}

	const allSubs = SETTINGS_SUBSECTIONS.filter((s) => s.area === AREA);
	$: subs = variant === 'wizard' && visibleSubsections
		? allSubs.filter((s) => visibleSubsections!.includes(s.id))
		: allSubs;
	const areaEntries = SETTINGS_MANIFEST.filter((e) => e.area === AREA);
	$: entriesBySub = Object.fromEntries(
		subs.map((s) => [s.id, areaEntries.filter((e) => e.subsection === s.id)]),
	) as Record<string, SettingsEntry[]>;

	function readByPath(obj: unknown, path: string): unknown {
		return path
			.split('.')
			.reduce<any>((cursor, key) => (cursor == null ? undefined : cursor[key]), obj);
	}

	function initialValue(entry: SettingsEntry): unknown {
		// Settings blob is FLAT — backendSection is only a routing label, not a storage key.
		// Read directly against the settings object using backendPath.
		const v = readByPath(settings, entry.backendPath);
		return v === undefined ? entry.default : v;
	}

	// Seed original/current on mount so parent and dirty tracking have a baseline.
	onMount(() => {
		const origSeed: Record<string, unknown> = {};
		for (const e of areaEntries) origSeed[e.id] = initialValue(e);
		originalValues.update((o) => ({ ...o, ...origSeed }));
	});

	// Reactive derivation: currentValues = originals + pending (pending wins).
	// Keep bound to the parent by reassigning the object (triggers reactivity).
	$: {
		const originals: Record<string, unknown> = {};
		for (const e of areaEntries) originals[e.id] = initialValue(e);
		const pend = $pendingValues;
		const areaPending: Record<string, unknown> = {};
		for (const e of areaEntries) {
			if (e.id in pend) areaPending[e.id] = pend[e.id];
		}
		currentValues = { ...currentValues, ...originals, ...areaPending };
	}

	function displayValue(entry: SettingsEntry): unknown {
		const pend = $pendingValues;
		if (entry.id in pend) return pend[entry.id];
		return initialValue(entry);
	}

	// Get current exchange from either pending values or settings
	$: currentExchange = ($pendingValues['exchange.exchange'] as string) || (settings?.exchange as string) || 'hyperliquid';

	// Map exchange to credential subsection
	function getCredentialSubsectionForExchange(exchange: string): string {
		const map: Record<string, string> = {
			hyperliquid: 'trading-credentials-hl',
			binance: 'trading-credentials-binance',
			kraken: 'trading-credentials-kraken',
			okx: 'trading-credentials-okx',
			coinbase: 'trading-credentials-coinbase',
			generic_ccxt: 'trading-credentials-generic-ccxt',
		};
		return map[exchange] || 'trading-credentials-hl';
	}

	// Filter subsections: hide credential sections not matching current exchange
	$: filteredSubs = subs.filter((sub) => {
		// Always show non-credential sections
		if (!sub.id.startsWith('trading-credentials-')) return true;
		// Show only the credential section for current exchange
		return sub.id === getCredentialSubsectionForExchange(currentExchange);
	});
</script>

<div class="space-y-6">
	{#each filteredSubs as sub (sub.id)}
		{@const entries = entriesBySub[sub.id] ?? []}
		{@const usedBy = [...new Set(entries.flatMap((e) => e.usedBy))]}
		<SettingsSubsection
			label={sub.label}
			description={sub.description ?? ''}
			deepLinkTo={sub.deepLinkTo}
			{usedBy}
		>
			{#if sub.advanced}<SettingsAdvancedHeader />{/if}
			{#each entries as entry (entry.id)}
				<SettingsFieldRow
					id={entry.id}
					label={entry.label}
					description={entry.description}
					unit={entry.unit}
					defaultValue={entry.default}
					value={displayValue(entry)}
					type={entry.type}
					options={entry.options ?? []}
				/>
			{/each}
			{#if sub.id === 'trading-credentials-hl'}
				<div class="mt-4 rounded-lg border border-cyan-900/60 bg-cyan-950/20 p-4">
					<p class="text-sm text-gray-200">
						No Hyperliquid account yet?
						<a
							href={HL_REFERRAL_URL}
							on:click|preventDefault={openReferral}
							class="font-semibold text-cyan-300 hover:text-cyan-200 hover:underline"
						>
							Create one and get 4% off trading fees →
						</a>
					</p>
					<p class="mt-1 text-xs text-gray-500">
						Referral link — you get the fee discount, we earn a small share of exchange
						fees. This helps with operating costs.
					</p>
					{#if referralCopyFallback}
						<p class="mt-2 text-xs text-amber-300 break-all">
							Couldn't open your browser. Copy this link: {HL_REFERRAL_URL}
						</p>
					{/if}
				</div>
			{/if}
		</SettingsSubsection>
	{/each}
</div>
