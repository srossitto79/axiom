<script lang="ts">
	/**
	 * Shared sticky "unsaved changes" bar for the Agents config tabs.
	 *
	 * Renders nothing until `dirty` is true; then a single sticky bar with one
	 * Save and one Discard action. Standardizes the make-many-changes-then-save
	 * pattern across tabs (Routing & Fallbacks, Models, …).
	 */
	export let dirty = false;
	export let saving = false;
	export let message = 'You have unsaved changes.';
	export let saveLabel = 'Save changes';
	export let onSave: () => void;
	export let onDiscard: () => void;
</script>

{#if dirty}
	<div
		class="sticky bottom-0 z-10 flex items-center justify-between gap-3 rounded-lg border border-blue-700 bg-blue-950/90 px-4 py-3 shadow-lg backdrop-blur"
	>
		<span class="text-sm text-blue-100">{message}</span>
		<div class="flex gap-2">
			<button
				type="button"
				on:click={onDiscard}
				disabled={saving}
				class="text-sm px-3 py-1.5 rounded border border-gray-600 text-gray-200 hover:text-white hover:border-gray-400 disabled:opacity-60"
			>
				Discard
			</button>
			<button
				type="button"
				on:click={onSave}
				disabled={saving}
				class="text-sm px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium disabled:opacity-60"
			>
				{saving ? 'Saving…' : saveLabel}
			</button>
		</div>
	</div>
{/if}
