<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getDiagnosticsSnapshot,
		getResumableTasks,
		resumeTask,
		type CheckResult,
		type CheckStatus,
		type DiagnosticsSnapshot,
		type ResumableTask,
	} from '$lib/api/diagnostics';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	// Task types that place orders / mutate external state. As of commit b008c12 these
	// are deliberately NOT auto-resumed without a checkpoint, so a manual resume can
	// re-run a side effect (e.g. double-place an order). Warn before resuming them.
	const EXTERNAL_MUTATING_TYPES = new Set(['trade_execution', 'phantom_repair']);

	const AUTO_REFRESH_MS = 60_000;

	let snapshot: DiagnosticsSnapshot | null = null;
	let resumable: ResumableTask[] = [];
	let loading = true;
	let error = '';
	let actionError = '';
	let actionMessage = '';
	let resumingId: number | null = null;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;
	let expanded = new Set<string>();

	const STATUS_LABEL: Record<CheckStatus, string> = {
		pass: 'Pass',
		warn: 'Warn',
		fail: 'Fail',
	};

	function statusClasses(status: CheckStatus | undefined): string {
		switch (status) {
			case 'pass':
				return 'text-green-400 border-green-800 bg-green-950/20';
			case 'warn':
				return 'text-amber-300 border-amber-800 bg-amber-950/20';
			case 'fail':
				return 'text-red-300 border-red-800 bg-red-950/20';
			default:
				return 'text-gray-400 border-[#333] bg-[#0d0d0d]';
		}
	}

	function statusDot(status: CheckStatus): string {
		switch (status) {
			case 'pass':
				return 'bg-green-500';
			case 'warn':
				return 'bg-amber-400';
			case 'fail':
				return 'bg-red-500';
			default:
				return 'bg-gray-500';
		}
	}

	function overallTitle(status: CheckStatus | undefined): string {
		switch (status) {
			case 'pass':
				return 'All Systems Healthy';
			case 'warn':
				return 'Attention Needed';
			case 'fail':
				return 'Failures Detected';
			default:
				return 'Status Unknown';
		}
	}

	type CostRollup = { cost_usd: number; task_count: number; total_tokens: number; window_hours: number };

	function costRollup(list: CheckResult[]): CostRollup | null {
		const row = list.find((c) => c.name === 'recent_costs');
		if (!row) return null;
		const d = row.detail ?? {};
		const num = (v: unknown): number => (typeof v === 'number' ? v : Number(v) || 0);
		return {
			cost_usd: num(d.cost_usd),
			task_count: num(d.task_count),
			total_tokens: num(d.total_tokens),
			window_hours: num(d.window_hours) || 24,
		};
	}

	function isExternalMutating(type: string | null | undefined): boolean {
		return type != null && EXTERNAL_MUTATING_TYPES.has(type);
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function detailEntries(detail: Record<string, unknown>): Array<[string, string]> {
		return Object.entries(detail).map(([key, value]) => [key, formatDetailValue(value)]);
	}

	function formatDetailValue(value: unknown): string {
		if (value === null || value === undefined) return '—';
		if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
			return String(value);
		}
		try {
			return JSON.stringify(value);
		} catch {
			return String(value);
		}
	}

	function toggleExpanded(name: string) {
		const next = new Set(expanded);
		if (next.has(name)) next.delete(name);
		else next.add(name);
		expanded = next;
	}

	async function loadAll() {
		error = '';
		loading = true;
		try {
			const [snap, list] = await Promise.all([
				getDiagnosticsSnapshot(),
				getResumableTasks(),
			]);
			snapshot = snap;
			resumable = list.tasks ?? [];
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load diagnostics.';
		} finally {
			loading = false;
		}
	}

	async function handleResume(task: ResumableTask) {
		if (resumingId !== null) return;
		if (isExternalMutating(task.type)) {
			const ok = window.confirm(
				`"${task.type}" tasks place orders or mutate external state. ` +
					`Re-queuing this task may repeat that side effect (e.g. double-place an order). Resume anyway?`,
			);
			if (!ok) return;
		}
		resumingId = task.id;
		actionMessage = '';
		actionError = '';
		try {
			await resumeTask(task.id);
			actionMessage = `Task ${task.display_id ?? task.id} re-queued. Runner will pick it up on next tick.`;
			await loadAll();
		} catch (err) {
			actionError = `Resume of ${task.display_id ?? `#${task.id}`} failed: ${
				err instanceof Error ? err.message : 'unknown error'
			}`;
		} finally {
			resumingId = null;
		}
	}

	onMount(() => {
		void loadAll();
		refreshTimer = setInterval(() => {
			void loadAll();
		}, AUTO_REFRESH_MS);
	});

	onDestroy(() => {
		if (refreshTimer !== null) {
			clearInterval(refreshTimer);
			refreshTimer = null;
		}
	});

	$: checks = snapshot?.checks ?? [];
	$: summary = snapshot?.summary ?? { pass: 0, warn: 0, fail: 0 };
	// A snapshot with zero checks is "unknown", not healthy — never imply false-green.
	$: overall = checks.length === 0 ? undefined : snapshot?.overall;
	$: cost = costRollup(checks);
	$: mcpServers = snapshot?.mcp_servers ?? [];
</script>

<svelte:head>
	<title>Diagnostics | Forven</title>
	<meta name="description" content="Health checks, 24h cost rollup, and resumable tasks for the Forven runtime." />
</svelte:head>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<div class="flex items-center justify-between gap-4">
		<div class="flex items-center gap-3">
			<svg class="w-6 h-6 text-cyan-400" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-1-13h2v6h-2zm0 8h2v2h-2z" />
			</svg>
			<div>
				<h1 class="text-2xl font-bold tracking-tight">Diagnostics</h1>
				<div class="text-[11px] text-gray-500 mt-0.5">
					{#if snapshot}
						Updated {formatTimestamp(snapshot.generated_at)} · auto-refresh 60s
					{:else}
						Health checks, 24h cost rollup, and resumable tasks
					{/if}
				</div>
			</div>
		</div>
		<button
			class="text-xs border border-[#333] px-3 py-1.5 text-gray-300 hover:text-white hover:border-[#555] transition-colors disabled:opacity-60"
			on:click={() => void loadAll()}
			disabled={loading}
			title="Re-run all checks immediately"
		>
			{loading ? 'Running…' : 'Run checks now'}
		</button>
	</div>

	{#if error}
		<ErrorBanner message={error} tone="error" />
	{/if}

	{#if actionError}
		<ErrorBanner message={actionError} tone="warning" dismissible on:dismiss={() => (actionError = '')} />
	{/if}

	{#if actionMessage}
		<div class="rounded border border-[#3a3220] bg-[#16130d] px-4 py-3 text-sm text-amber-200">
			{actionMessage}
		</div>
	{/if}

	{#if loading && !snapshot}
		<LoadingState message="Running diagnostics…" />
	{/if}

	{#if snapshot}
		<div class="grid grid-cols-1 md:grid-cols-5 gap-4">
			<div class="border rounded p-4 col-span-1 md:col-span-2 {statusClasses(overall)}">
				<div class="text-[10px] uppercase tracking-wider opacity-80">Overall</div>
				<div class="text-2xl font-bold mt-1">{overallTitle(overall)}</div>
				<div class="text-xs mt-2 opacity-90">
					{checks.length} check(s) ran ·
					{summary.pass} pass / {summary.warn} warn / {summary.fail} fail
				</div>
			</div>
			<div class="border border-[#333] bg-[#0d0d0d] rounded p-4">
				<div class="text-[10px] uppercase tracking-wider text-gray-500">
					Cost{#if cost}{` (${cost.window_hours}h)`}{/if}
				</div>
				{#if cost}
					<div class="text-2xl font-bold mt-1 text-gray-100">${cost.cost_usd.toFixed(4)}</div>
					<div class="text-[11px] text-gray-500 mt-1">
						{cost.task_count} task(s) · {cost.total_tokens.toLocaleString()} tokens
					</div>
				{:else}
					<div class="text-sm font-bold mt-1 text-gray-500">—</div>
					<div class="text-[11px] text-gray-500 mt-1">no cost data</div>
				{/if}
			</div>
			<div class="border border-[#333] bg-[#0d0d0d] rounded p-4">
				<div class="text-[10px] uppercase tracking-wider text-gray-500">Resumable Tasks</div>
				<div class="text-2xl font-bold mt-1 text-gray-100">{resumable.length}</div>
				<div class="text-[11px] text-gray-500 mt-1">interrupted &amp; recoverable</div>
			</div>
			<div class="border border-[#333] bg-[#0d0d0d] rounded p-4">
				<div class="text-[10px] uppercase tracking-wider text-gray-500">Last Snapshot</div>
				<div class="text-sm font-bold mt-1 text-gray-100">{formatTimestamp(snapshot.generated_at)}</div>
				<div class="text-[11px] text-gray-500 mt-1">auto-refreshes every 60s</div>
			</div>
		</div>

		<div class="border border-[#333] bg-[#0d0d0d] rounded">
			<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between">
				<h2 class="text-sm font-bold uppercase tracking-wider text-gray-200">Health Checks</h2>
				<span class="text-[10px] text-gray-500">click a row for detail</span>
			</div>
			<div class="divide-y divide-[#1a1a1a]">
				{#each checks as check (check.name)}
					{@const isOpen = expanded.has(check.name)}
					{@const details = detailEntries(check.detail ?? {})}
					<div class="px-4 py-3">
						<button
							class="w-full flex items-start justify-between gap-4 text-left"
							on:click={() => toggleExpanded(check.name)}
							aria-expanded={isOpen}
						>
							<div class="flex items-start gap-3 min-w-0">
								<span class="mt-1 inline-block w-2 h-2 rounded-full shrink-0 {statusDot(check.status)}"></span>
								<div class="min-w-0">
									<div class="text-xs font-bold text-gray-100 truncate">{check.name}</div>
									<div class="text-[11px] text-gray-400 mt-0.5">{check.summary}</div>
									{#if check.checked_at}
										<div class="text-[10px] text-gray-600 mt-0.5">checked {formatTimestamp(check.checked_at)}</div>
									{/if}
								</div>
							</div>
							<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border rounded shrink-0 {statusClasses(check.status)}">
								{STATUS_LABEL[check.status] ?? check.status}
							</span>
						</button>
						{#if isOpen && details.length > 0}
							<div class="mt-3 ml-5 border-l border-[#222] pl-4 space-y-1">
								{#each details as [key, value]}
									<div class="grid grid-cols-[140px_1fr] gap-2 text-[11px]">
										<div class="text-gray-500 truncate">{key}</div>
										<div class="text-gray-200 break-all">{value}</div>
									</div>
								{/each}
							</div>
						{/if}
					</div>
				{/each}
				{#if checks.length === 0}
					<div class="px-4 py-6 text-center text-xs text-gray-500">No checks reported.</div>
				{/if}
			</div>
		</div>

		{#if mcpServers.length > 0}
			<div class="border border-[#333] bg-[#0d0d0d] rounded">
				<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between">
					<h2 class="text-sm font-bold uppercase tracking-wider text-gray-200">MCP Servers</h2>
					<span class="text-[10px] text-gray-500">click a row to manage</span>
				</div>
				<div class="divide-y divide-[#1a1a1a]">
					{#each mcpServers as server (server.name)}
						<a
							href="/integrations/mcp/{server.name}"
							class="px-4 py-3 flex items-start justify-between gap-4 hover:bg-[#141414] transition-colors"
						>
							<div class="flex items-start gap-3 min-w-0">
								<span
									class="mt-1 inline-block w-2 h-2 rounded-full shrink-0 {!server.enabled
										? 'bg-gray-600'
										: server.last_status === 'ok'
											? 'bg-green-500'
											: server.last_status === 'error'
												? 'bg-red-500'
												: 'bg-gray-500'}"
								></span>
								<div class="min-w-0">
									<div class="text-xs font-bold text-gray-100 truncate">
										{server.name}
										{#if server.transport}
											<span class="text-[10px] font-normal text-gray-500">· {server.transport}</span>
										{/if}
										{#if !server.enabled}
											<span class="text-[10px] font-normal text-gray-600">· disabled</span>
										{/if}
									</div>
									<div class="text-[11px] text-gray-400 mt-0.5">
										{server.last_status ?? 'never checked'}
										{#if server.last_status_at}
											· {formatTimestamp(server.last_status_at)}
										{/if}
									</div>
									{#if server.last_error_short}
										<div class="text-[11px] text-red-300/80 mt-1 break-all">{server.last_error_short}</div>
									{/if}
								</div>
							</div>
							<span class="text-[10px] text-gray-500 shrink-0 mt-1">→</span>
						</a>
					{/each}
				</div>
			</div>
		{/if}

		<div class="border border-[#333] bg-[#0d0d0d] rounded">
			<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between">
				<h2 class="text-sm font-bold uppercase tracking-wider text-gray-200">Resumable Tasks</h2>
				<span class="text-[10px] text-gray-500">{resumable.length} waiting</span>
			</div>
			{#if resumable.length === 0}
				<div class="px-4 py-6 text-center text-xs text-gray-500">
					No interrupted tasks. Tasks left running when the app closes show up here.
				</div>
			{:else}
				<div class="divide-y divide-[#1a1a1a]">
					{#each resumable as task (task.id)}
						{@const external = isExternalMutating(task.type)}
						<div class="px-4 py-3 flex items-center justify-between gap-4">
							<div class="min-w-0">
								<div class="text-xs font-bold text-gray-100 truncate flex items-center gap-2">
									<span class="truncate">{task.display_id ?? `#${task.id}`} · {task.title}</span>
									{#if task.type}
										<span
											class="text-[10px] font-normal uppercase tracking-wider px-1.5 py-0.5 border rounded shrink-0 {external
												? 'text-amber-300 border-amber-800 bg-amber-950/30'
												: 'text-gray-400 border-[#333] bg-[#111]'}"
										>
											{task.type}
										</span>
									{/if}
								</div>
								<div class="text-[11px] text-gray-500 mt-0.5">
									Agent {task.agent_id ?? 'unknown'} · interrupted {formatTimestamp(task.interrupted_at)}
									{#if task.started_at}
										· started {formatTimestamp(task.started_at)}
									{/if}
									{#if task.checkpoint_count > 0}
										· {task.checkpoint_count} checkpoint(s)
									{/if}
								</div>
								{#if task.latest_checkpoint}
									<div class="text-[11px] text-cyan-300/80 mt-1 truncate">
										latest: {task.latest_checkpoint.key} ({formatTimestamp(task.latest_checkpoint.updated_at)})
									</div>
								{/if}
								{#if external}
									<div class="text-[11px] text-amber-300/90 mt-1">
										⚠ Not auto-resumed by design — re-queuing may repeat an external side effect (e.g. place an
										order). Resume only if you are sure it did not already run.
									</div>
								{/if}
							</div>
							<button
								class="text-xs border px-3 py-1.5 transition-colors disabled:opacity-60 {external
									? 'border-amber-700 text-amber-200 hover:bg-amber-900/30'
									: 'border-cyan-700 text-cyan-200 hover:bg-cyan-900/30'}"
								on:click={() => handleResume(task)}
								disabled={resumingId !== null}
							>
								{resumingId === task.id ? 'Resuming…' : external ? 'Resume (caution)' : 'Resume'}
							</button>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
