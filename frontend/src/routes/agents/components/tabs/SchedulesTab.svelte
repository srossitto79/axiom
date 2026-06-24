<script lang="ts">
	/**
	 * Schedules tab. One scheduler editor — reuses SchedulerJobRow and the Hub's
	 * save handler. The host page owns the jobs list and the save handler so this
	 * tab stays a thin presenter (and the duplicate editor that lived in
	 * SettingsAgents is dropped).
	 */
	import type { ForvenSchedulerJob } from '$lib/api';
	import SchedulerJobRow from '../SchedulerJobRow.svelte';

	export let jobs: ForvenSchedulerJob[] = [];
	export let onSave: (
		jobId: string | number,
		scheduleType: string,
		scheduleExpr: string,
		enabled: boolean,
	) => Promise<void>;
	export let showErrors = false;
	export let loading = false;
</script>

<section class="border border-gray-800 rounded-lg bg-black p-6 space-y-4">
	<header class="border-b border-gray-800 pb-2">
		<h2 class="text-lg font-semibold text-white">Schedules</h2>
		<p class="text-xs text-gray-500 mt-1">
			Cron / interval schedules for continuous learning and trading processes. Each job has its own cadence.
		</p>
	</header>

	{#if loading && jobs.length === 0}
		<p class="text-sm text-gray-400">Loading scheduler jobs…</p>
	{:else if jobs.length === 0}
		<p class="text-sm text-gray-400">No scheduler jobs found.</p>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full text-left text-xs">
				<thead>
					<tr class="border-b border-[#222] text-gray-500 uppercase tracking-wider">
						<th class="px-4 py-2 font-medium">Name</th>
						<th class="px-4 py-2 font-medium">Schedule</th>
						<th class="px-4 py-2 font-medium">Next Run</th>
						<th class="px-4 py-2 font-medium">Status</th>
						<th class="px-4 py-2 font-medium">Enabled</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-[#222]">
					{#each jobs as job (job.id)}
						<SchedulerJobRow {job} {onSave} {showErrors} />
					{:else}
						<tr><td colspan="5" class="px-4 py-4 text-center text-gray-500">No jobs configured</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>
