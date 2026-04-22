<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';

	import { getOpenClawWorkerJob } from '$lib/apis/openai';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import {
		buildOpenClawWorkerCoordinatorBrief,
		buildOpenClawWorkerRenderableFinalText,
		getOpenClawWorkerPhaseKey,
		buildOpenClawWorkerStatusHistory,
		getOpenClawWorkerSubagentItems,
		getOpenClawWorkerSubagentPhaseKey,
		isOpenClawWorkerRenderableFinalText,
		isOpenClawWorkerTerminal
	} from '$lib/utils/openclaw-worker';

	import ContentRenderer from '../ContentRenderer.svelte';
	import StatusHistory from './StatusHistory.svelte';

	const i18n = getContext('i18n');

	export let jobId = '';
	export let modelId = '';
	export let model = null;
	export let history;
	export let messageId = '';
	export let selectedModels = [];
	export let editCodeBlock = true;

	let job = null;
	let error = '';
	let loading = true;
	let mounted = false;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let requestKey = '';

	const getParentUserPrompt = (threadHistory: typeof history, currentMessageId: string): string => {
		const messages = threadHistory?.messages;
		if (!messages || !currentMessageId) {
			return '';
		}
		const currentMessage = messages[currentMessageId];
		const parentMessage = currentMessage?.parentId ? messages[currentMessage.parentId] : null;
		if (!parentMessage || parentMessage.role !== 'user') {
			return '';
		}
		return typeof parentMessage.content === 'string' ? parentMessage.content : '';
	};

	const clearPollTimer = () => {
		if (pollTimer) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
	};

	const schedulePoll = (delayMs = 3000) => {
		clearPollTimer();
		if (!mounted) {
			return;
		}

		pollTimer = setTimeout(() => {
			void loadJob();
		}, delayMs);
	};

	const loadJob = async () => {
		if (!jobId || !modelId) {
			loading = false;
			return;
		}

		try {
			const nextJob = await getOpenClawWorkerJob(localStorage.token, jobId, modelId);
			job = nextJob;
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : `${e}`;
		} finally {
			loading = false;
			if (mounted && (error || !isOpenClawWorkerTerminal(job))) {
				schedulePoll(error ? 5000 : 3000);
			}
		}
	};

	const restartPolling = () => {
		clearPollTimer();
		job = null;
		error = '';
		loading = true;
		void loadJob();
	};

	onMount(() => {
		mounted = true;
		requestKey = `${jobId}:${modelId}`;
		restartPolling();

		const handleVisibilityChange = () => {
			if (!document.hidden && !isOpenClawWorkerTerminal(job)) {
				void loadJob();
			}
		};

		document.addEventListener('visibilitychange', handleVisibilityChange);

		return () => {
			document.removeEventListener('visibilitychange', handleVisibilityChange);
		};
	});

	onDestroy(() => {
		mounted = false;
		clearPollTimer();
	});

	$: nextRequestKey = `${jobId}:${modelId}`;
	$: if (mounted && nextRequestKey !== requestKey) {
		requestKey = nextRequestKey;
		restartPolling();
	}

	$: phaseKey = getOpenClawWorkerPhaseKey(job);
	$: statusHistory = buildOpenClawWorkerStatusHistory(job);
	$: subagentItems = getOpenClawWorkerSubagentItems(job);
	$: parentUserPrompt = getParentUserPrompt(history, messageId);
	$: coordinatorBrief = buildOpenClawWorkerCoordinatorBrief(parentUserPrompt, job, subagentItems);
	$: subagentCompletedCount = subagentItems.filter((item) => item.state === 'completed').length;
	$: renderableFinalText = buildOpenClawWorkerRenderableFinalText(
		job?.final_visible_text,
		Array.isArray(job?.resolved_artifacts) ? job.resolved_artifacts : []
	);
	$: showFinalResult = Boolean(
		job &&
		isOpenClawWorkerTerminal(job) &&
		isOpenClawWorkerRenderableFinalText(job.final_visible_text)
	);
	$: showEmptyResult = Boolean(
		job && isOpenClawWorkerTerminal(job) && !showFinalResult && !job?.error_message
	);
	$: badgeClass =
		phaseKey === 'Completed'
			? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
			: phaseKey === 'Failed' || phaseKey === 'Timed out'
				? 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300'
				: phaseKey === 'Cancelled'
					? 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200'
					: phaseKey === 'Loading model'
						? 'bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
						: 'bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300';
</script>

{#if jobId}
	<div
		class="my-3 rounded-2xl border border-gray-200/80 bg-white/80 p-4 dark:border-gray-800 dark:bg-gray-900/70"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="min-w-0">
				<div
					class="text-xs font-semibold uppercase tracking-[0.16em] text-gray-500 dark:text-gray-400"
				>
					{$i18n.t('Collaborative Task')}
				</div>

				{#if job?.id || jobId}
					<div class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
						{$i18n.t('Task ID')}:
						<span class="font-mono">{job?.id ?? jobId}</span>
					</div>
				{/if}
			</div>

			<div class={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${badgeClass}`}>
				{$i18n.t(phaseKey)}
			</div>
		</div>

		{#if coordinatorBrief.requestSummary || coordinatorBrief.intro || coordinatorBrief.assignments.length > 0}
			<div class="mt-3 rounded-xl border border-gray-200 bg-gray-50/90 p-3 dark:border-gray-800 dark:bg-gray-950/30">
				{#if coordinatorBrief.requestSummary}
					<div class="text-xs font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">
						{$i18n.t('Task Summary')}
					</div>
					<div class="mt-2 text-sm leading-6 text-gray-700 dark:text-gray-200">
						{coordinatorBrief.requestSummary}
					</div>
				{/if}

				{#if coordinatorBrief.intro}
					<div class="mt-3 text-sm leading-6 text-gray-700 dark:text-gray-200">
						{coordinatorBrief.intro}
					</div>
				{/if}

				{#if coordinatorBrief.assignments.length > 0}
					<div class="mt-3">
						<div class="text-xs font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">
							{$i18n.t('My Plan')}
						</div>

						<div class="mt-2 flex flex-col gap-2">
							{#each coordinatorBrief.assignments as assignment}
								<div class="rounded-lg bg-white/90 px-3 py-2 dark:bg-gray-900/70">
									<div class="text-sm font-medium text-gray-800 dark:text-gray-100">
										{assignment.agentId}
									</div>
									<div class="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
										{assignment.summary}
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			</div>
		{/if}

		{#if loading && !job}
			<div class="mt-3 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
				<Spinner />
				<span>{$i18n.t('Queued')}</span>
			</div>
		{/if}

		{#if statusHistory.length > 0}
			<div class="mt-3">
				<StatusHistory {statusHistory} />
			</div>
		{/if}

		{#if subagentItems.length > 0}
			<div class="mt-3 rounded-xl border border-gray-200 bg-gray-50/90 p-3 dark:border-gray-800 dark:bg-gray-950/30">
				<div class="flex items-center justify-between gap-3">
					<div
						class="text-xs font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400"
					>
						{$i18n.t('Subtasks')}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">
						{subagentCompletedCount}/{subagentItems.length} {$i18n.t('Completed')}
					</div>
				</div>

				<div class="mt-3 flex flex-col gap-2">
					{#each subagentItems as item}
						{@const subagentPhaseKey = getOpenClawWorkerSubagentPhaseKey(item)}
						{@const subagentBadgeClass =
							subagentPhaseKey === 'Completed'
								? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
								: subagentPhaseKey === 'Failed' || subagentPhaseKey === 'Timed out'
									? 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300'
									: subagentPhaseKey === 'Cancelled'
										? 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200'
										: 'bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300'}
						<div class="rounded-xl border border-gray-200/80 bg-white/90 px-3 py-2 dark:border-gray-800 dark:bg-gray-900/70">
							<div class="flex items-center justify-between gap-3">
								<div class="min-w-0 text-sm font-medium text-gray-800 dark:text-gray-100">
									{item.agentId}
								</div>
								<div class={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${subagentBadgeClass}`}>
									{$i18n.t(subagentPhaseKey)}
								</div>
							</div>

							{#if item.task}
								<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
									{item.task}
								</div>
							{/if}

							{#if item.status}
								<div class="mt-2 text-xs text-gray-500 dark:text-gray-400">
									{$i18n.t('Status')}: {item.status}
								</div>
							{/if}

							{#if item.resultPreview}
								<div class="mt-2 rounded-lg bg-gray-50/90 px-2.5 py-2 text-xs text-gray-600 dark:bg-gray-950/40 dark:text-gray-300">
									{item.resultPreview}
								</div>
							{/if}
						</div>
					{/each}
				</div>
			</div>
		{/if}

		{#if error}
			<div
				class="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/80 dark:bg-red-950/30 dark:text-red-300"
			>
				{$i18n.t('Worker status unavailable')}: {error}
			</div>
		{/if}

		{#if job?.error_message}
			<div
				class="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/80 dark:bg-red-950/30 dark:text-red-300"
			>
				{job.error_message}
			</div>
		{/if}

		{#if showFinalResult}
			<div class="mt-4 border-t border-gray-200 pt-4 dark:border-gray-800">
				<div
					class="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400"
				>
					{$i18n.t('Final result')}
				</div>

				<ContentRenderer
					id={`${messageId}-openclaw-worker-result`}
					content={renderableFinalText}
					{history}
					{messageId}
					{selectedModels}
					done={true}
					{model}
					sources={[]}
					floatingButtons={false}
					{editCodeBlock}
					topPadding={false}
				/>
			</div>
		{:else if showEmptyResult}
			<div class="mt-3 text-sm text-gray-500 dark:text-gray-400">
				{$i18n.t('No text result was returned for this worker job.')}
			</div>
		{/if}
	</div>
{/if}
