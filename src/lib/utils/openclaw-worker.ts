const OPENCLAW_WORKER_MARKERS = [/OpenClaw Worker/i, /OpenClaw job/i];
const WORKER_CONTEXT_SPLIT_RE = /\n+\s*---\s*\n+\s*Worker execution context[\s\S]*$/i;

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'timed_out', 'cancelled']);
const TERMINAL_PHASES = new Set(['completed', 'failed', 'timed_out', 'cancelled']);
const FAILED_SUBAGENT_STATUS_RE = /(fail|error|abort)/i;
const TIMED_OUT_SUBAGENT_STATUS_RE = /(timed?\s*out|timeout|stall)/i;
const CANCELLED_SUBAGENT_STATUS_RE = /cancel/i;
const WAITING_RESULT_RE =
	/(已启动第?一批|已启动子会话|正在等待|等待各角色|收到完成后|稍后|waiting for|once they finish|started the first batch|will summarize|will send.*later|多角色协调进行中|正在生成最终汇总|汇总生成中|release 正在生成)/i;
const OPENCLAW_WORKER_LOCAL_FILE_PROTOCOL = 'openwebui:';
const OPENCLAW_WORKER_LOCAL_FILE_HOST = 'local-file';
const OPENCLAW_WORKER_IMAGE_SUFFIX_RE = /\.(avif|bmp|gif|jpe?g|png|svg|webp)$/i;
const OPENCLAW_WORKER_INLINE_CONTENT_TYPES = new Set([
	'application/javascript',
	'application/json',
	'application/pdf',
	'application/xml',
	'image/svg+xml'
]);

export type OpenClawWorkerHistoryItem = {
	action: string;
	description: string;
	done: boolean;
	hidden?: boolean;
};

export type OpenClawWorkerSubagentProgressItem = {
	sessionKey?: string;
	agentId: string;
	state: 'running' | 'completed';
	task: string;
	status: string;
	resultPreview: string;
};

export type OpenClawWorkerCoordinatorBrief = {
	requestSummary: string;
	intro: string;
	assignments: Array<{
		agentId: string;
		summary: string;
	}>;
};

export type OpenClawWorkerResolvedArtifact = {
	label: string;
	path: string;
};

export type OpenClawWorkerDisplayableJob = {
	id?: string | null;
	model?: string | null;
	status?: string | null;
	phase?: string | null;
	final_visible_text?: string | null;
	media_urls?: unknown;
	mediaUrls?: unknown;
	resolved_artifacts?: unknown;
	subagent_progress?: {
		activeCount?: number | null;
		items?: Array<{
			sessionKey?: string | null;
			agentId?: string | null;
			state?: string | null;
			task?: string | null;
			status?: string | null;
			resultPreview?: string | null;
		}>;
	} | null;
	status_history?: { phase?: string | null; status?: string | null }[];
	estimate?: {
		preferredInitialBatch?: string[] | null;
	} | null;
	error_message?: string | null;
};

const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const normalizeOpenClawWorkerText = (text: string | null | undefined): string => {
	return String(text ?? '')
		.replace(WORKER_CONTEXT_SPLIT_RE, '')
		.replace(/\s+/g, ' ')
		.trim();
};

const summarizeOpenClawWorkerText = (
	text: string | null | undefined,
	limit = 96
): string => {
	const normalized = normalizeOpenClawWorkerText(text);
	if (!normalized) {
		return '';
	}
	if (normalized.length <= limit) {
		return normalized;
	}
	return `${normalized.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
};

export const getOpenClawWorkerSubagentPhaseKey = (
	item:
		| {
				state?: string | null;
				status?: string | null;
		  }
		| null
): string => {
	if (!item || String(item.state ?? '').trim().toLowerCase() !== 'completed') {
		return 'Running';
	}

	const status = String(item.status ?? '').trim();
	if (CANCELLED_SUBAGENT_STATUS_RE.test(status)) {
		return 'Cancelled';
	}
	if (TIMED_OUT_SUBAGENT_STATUS_RE.test(status)) {
		return 'Timed out';
	}
	if (FAILED_SUBAGENT_STATUS_RE.test(status)) {
		return 'Failed';
	}
	return 'Completed';
};

export type OpenClawWorkerResponse = {
	id: string;
	object: string;
	created_at: number;
	status: string;
	model: string;
	output: Array<{
		id: string;
		type: string;
		role: string;
		status: string;
		content: Array<{
			type: string;
			text: string;
		}>;
	}>;
	usage: {
		input_tokens: number;
		output_tokens: number;
		total_tokens: number;
	};
};

export const extractOpenClawWorkerJobId = (content: string | null | undefined): string | null => {
	if (!content) {
		return null;
	}

	const trimmed = content.trim();
	if (!OPENCLAW_WORKER_MARKERS.some((pattern) => pattern.test(trimmed))) {
		return null;
	}

	const patterns = [
		/<!--\s*OpenClaw Worker\b[\s\S]*?job id:\s*`([a-z0-9-]{8,64})`[\s\S]*?-->/i,
		/job id:\s*`([a-z0-9-]{8,64})`/i,
		/OpenClaw job\s*`([a-z0-9-]{8,64})`/i,
		/\/job\s+([a-z0-9-]{8,64})/i
	];

	for (const pattern of patterns) {
		const match = trimmed.match(pattern);
		if (match?.[1]) {
			return match[1];
		}
	}

	return null;
};

export const isOpenClawWorkerTerminal = (
	job: {
		status?: string | null;
		phase?: string | null;
		final_visible_text?: string | null;
		subagent_progress?: {
			activeCount?: number | null;
		} | null;
	} | null
): boolean => {
	if (!job) {
		return false;
	}

	const status = String(job.status ?? '')
		.trim()
		.toLowerCase();
	const phase = String(job.phase ?? '')
		.trim()
		.toLowerCase();
	const finalText = normalizeOpenClawWorkerText(job.final_visible_text);
	const activeCount = Number(job?.subagent_progress?.activeCount ?? 0);

	if (activeCount > 0 && WAITING_RESULT_RE.test(finalText)) {
		return false;
	}

	return TERMINAL_STATUSES.has(status) || TERMINAL_PHASES.has(phase);
};

export const getOpenClawWorkerPhaseKey = (
	job: {
		status?: string | null;
		phase?: string | null;
		final_visible_text?: string | null;
		subagent_progress?: {
			activeCount?: number | null;
		} | null;
	} | null
): string => {
	if (!job) {
		return 'Queued';
	}

	const status = String(job.status ?? '')
		.trim()
		.toLowerCase();
	const phase = String(job.phase ?? '')
		.trim()
		.toLowerCase();
	const finalText = normalizeOpenClawWorkerText(job.final_visible_text);
	const activeCount = Number(job?.subagent_progress?.activeCount ?? 0);

	if (activeCount > 0 && WAITING_RESULT_RE.test(finalText)) {
		return 'Running';
	}

	if (phase === 'completed' || status === 'succeeded') return 'Completed';
	if (phase === 'failed' || status === 'failed') return 'Failed';
	if (phase === 'timed_out' || status === 'timed_out') return 'Timed out';
	if (phase === 'cancelled' || status === 'cancelled') return 'Cancelled';
	if (phase === 'loading_model') return 'Loading model';
	if (phase === 'running' || status === 'running') return 'Running';
	if (phase === 'started') return 'Running';
	return 'Queued';
};

export const isOpenClawWorkerRenderableFinalText = (text: string | null | undefined): boolean => {
	const normalized = normalizeOpenClawWorkerText(text);
	return normalized !== '' && !WAITING_RESULT_RE.test(normalized);
};

const hasOpenClawWorkerResolvedArtifacts = (value: unknown): boolean =>
	Array.isArray(value) &&
	value.some((item) => {
		if (!item || typeof item !== 'object') {
			return false;
		}
		const artifact = item as { label?: unknown; path?: unknown };
		return Boolean(String(artifact.label ?? '').trim() && String(artifact.path ?? '').trim());
	});

const hasOpenClawWorkerMediaUrls = (value: unknown): boolean =>
	Array.isArray(value) &&
	value.some((item) => /^https?:\/\//i.test(String(item ?? '').trim()));

export const hasOpenClawWorkerDisplayableResult = (
	job: OpenClawWorkerDisplayableJob | null
): boolean => {
	if (!job) {
		return false;
	}

	return (
		isOpenClawWorkerRenderableFinalText(job.final_visible_text) ||
		hasOpenClawWorkerResolvedArtifacts(job.resolved_artifacts) ||
		hasOpenClawWorkerMediaUrls(job.media_urls) ||
		hasOpenClawWorkerMediaUrls(job.mediaUrls)
	);
};

export const shouldPollOpenClawWorkerJob = (job: OpenClawWorkerDisplayableJob | null): boolean => {
	if (!job) {
		return true;
	}

	if (!isOpenClawWorkerTerminal(job)) {
		return true;
	}

	const status = String(job.status ?? '')
		.trim()
		.toLowerCase();
	const phase = String(job.phase ?? '')
		.trim()
		.toLowerCase();
	const succeeded = phase === 'completed' || status === 'succeeded';

	return succeeded && !job.error_message && !hasOpenClawWorkerDisplayableResult(job);
};

export const shouldRenderOpenClawWorkerFinalResult = (
	job: OpenClawWorkerDisplayableJob | null
): boolean => {
	if (!job || !isOpenClawWorkerTerminal(job)) {
		return false;
	}

	return hasOpenClawWorkerDisplayableResult(job);
};

export const buildOpenClawWorkerLocalFileHref = (
	path: string,
	options: { jobId?: string | null; modelId?: string | null } = {}
): string => {
	const params = new URLSearchParams({ path });
	if (options.jobId) {
		params.set('jobId', options.jobId);
	}
	if (options.modelId) {
		params.set('model', options.modelId);
	}
	return `${OPENCLAW_WORKER_LOCAL_FILE_PROTOCOL}//${OPENCLAW_WORKER_LOCAL_FILE_HOST}?${params.toString()}`;
};

export const extractOpenClawWorkerLocalFileReference = (
	href: string | null | undefined
): { path: string; jobId: string | null; modelId: string | null } | null => {
	try {
		const url = new URL(
			String(href ?? ''),
			typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
		);
		if (
			url.protocol !== OPENCLAW_WORKER_LOCAL_FILE_PROTOCOL ||
			url.hostname !== OPENCLAW_WORKER_LOCAL_FILE_HOST
		) {
			return null;
		}
		const path = url.searchParams.get('path')?.trim();
		if (!path) {
			return null;
		}
		return {
			path,
			jobId: url.searchParams.get('jobId')?.trim() || null,
			modelId: url.searchParams.get('model')?.trim() || null
		};
	} catch {
		return null;
	}
};

export const extractOpenClawWorkerLocalFilePath = (
	href: string | null | undefined
): string | null => {
	return extractOpenClawWorkerLocalFileReference(href)?.path ?? null;
};

export const parseOpenClawWorkerArtifactFilename = (
	contentDisposition: string | null | undefined
): string | null => {
	const value = String(contentDisposition ?? '').trim();
	if (!value) {
		return null;
	}

	const encodedMatch = value.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
	if (encodedMatch?.[1]) {
		try {
			return decodeURIComponent(encodedMatch[1].trim());
		} catch {
			return encodedMatch[1].trim();
		}
	}

	const plainMatch = value.match(/filename\s*=\s*"?([^";]+)"?/i);
	return plainMatch?.[1]?.trim() || null;
};

export const shouldOpenClawWorkerArtifactInline = (
	contentType: string | null | undefined,
	contentDisposition: string | null | undefined
): boolean => {
	if (/attachment/i.test(String(contentDisposition ?? ''))) {
		return false;
	}

	const normalizedType = String(contentType ?? '')
		.split(';', 1)[0]
		.trim()
		.toLowerCase();
	if (!normalizedType) {
		return false;
	}

	return (
		normalizedType.startsWith('text/') ||
		normalizedType.startsWith('image/') ||
		normalizedType.startsWith('audio/') ||
		normalizedType.startsWith('video/') ||
		OPENCLAW_WORKER_INLINE_CONTENT_TYPES.has(normalizedType)
	);
};

export const buildOpenClawWorkerRenderableFinalText = (
	text: string | null | undefined,
	artifacts:
		| Array<{
				label?: string | null;
				path?: string | null;
		  }>
		| null
		| undefined,
	mediaUrls: unknown = [],
	job: { id?: string | null; model?: string | null } | null = null
): string => {
	let nextText = String(text ?? '');
	const linkOptions = { jobId: job?.id ?? null, modelId: job?.model ?? null };
	const resolvedArtifacts = Array.isArray(artifacts)
		? artifacts
				.map((artifact) => ({
					label: String(artifact?.label ?? '').trim(),
					path: String(artifact?.path ?? '').trim()
				}))
				.filter((artifact) => artifact.label && artifact.path)
				.sort((left, right) => right.label.length - left.label.length)
		: [];

	for (const artifact of resolvedArtifacts) {
		const href = buildOpenClawWorkerLocalFileHref(artifact.path, linkOptions);
		const pattern = new RegExp('`' + escapeRegExp(artifact.label) + '`', 'g');
		nextText = nextText.replace(pattern, `[${artifact.label}](${href})`);
	}

	const inlineImageMarkdown = resolvedArtifacts
		.filter((artifact) => OPENCLAW_WORKER_IMAGE_SUFFIX_RE.test(artifact.path || artifact.label))
		.map((artifact) => {
			const href = buildOpenClawWorkerLocalFileHref(artifact.path, linkOptions);
			const alt = artifact.path.split('/').pop()?.trim() || artifact.label || 'artifact';
			const imageMarkdown = `![${alt}](${href})`;
			const existingImagePattern = new RegExp(`!\\[[^\\]]*\\]\\(${escapeRegExp(href)}\\)`);
			return existingImagePattern.test(nextText) ? '' : imageMarkdown;
		})
		.filter(Boolean);

	if (inlineImageMarkdown.length > 0) {
		nextText = [nextText.trimEnd(), inlineImageMarkdown.join('\n\n')]
			.filter(Boolean)
			.join('\n\n');
	}

	const mediaImageMarkdown = Array.isArray(mediaUrls)
		? mediaUrls
				.map((url, index) => String(url ?? '').trim())
				.filter((url) => /^https?:\/\//i.test(url))
				.filter((url) => !nextText.includes(url))
				.map((url, index) => `![generated-image-${index + 1}](${url})`)
		: [];

	if (mediaImageMarkdown.length > 0) {
		nextText = [nextText.trimEnd(), mediaImageMarkdown.join('\n\n')]
			.filter(Boolean)
			.join('\n\n');
	}

	return nextText;
};

export const buildOpenClawWorkerStatusHistory = (
	job: {
		status?: string | null;
		phase?: string | null;
		status_history?: { phase?: string | null; status?: string | null }[];
	} | null
): OpenClawWorkerHistoryItem[] => {
	if (!job) {
		return [];
	}

	const history = Array.isArray(job.status_history) ? job.status_history : [];
	const phaseKeys = history
		.map((entry) => getOpenClawWorkerPhaseKey(entry))
		.filter((phase, index, items) => phase && (index === 0 || phase !== items[index - 1]));

	const fallbackPhase = getOpenClawWorkerPhaseKey(job);
	if (phaseKeys.length === 0 || phaseKeys[phaseKeys.length - 1] !== fallbackPhase) {
		phaseKeys.push(fallbackPhase);
	}

	return phaseKeys.map((phase, index) => ({
		action: 'openclaw_worker',
		description: phase,
		done: index < phaseKeys.length - 1 || isOpenClawWorkerTerminal(job)
	}));
};

export const getOpenClawWorkerSubagentItems = (
	job:
		| {
				subagent_progress?: {
					items?: Array<{
						sessionKey?: string | null;
						agentId?: string | null;
						state?: string | null;
						task?: string | null;
						status?: string | null;
						resultPreview?: string | null;
					}>;
				} | null;
		  }
		| null
): OpenClawWorkerSubagentProgressItem[] => {
	const items = Array.isArray(job?.subagent_progress?.items) ? job.subagent_progress.items : [];

	return items
		.filter((item) => item && typeof item === 'object')
		.map((item) => ({
			sessionKey: String(item?.sessionKey ?? '').trim(),
			agentId: String(item?.agentId ?? '').trim(),
			state: (
				String(item?.state ?? '').trim().toLowerCase() === 'completed' ? 'completed' : 'running'
			) as OpenClawWorkerSubagentProgressItem['state'],
			task: String(item?.task ?? '').trim(),
			status: String(item?.status ?? '').trim(),
			resultPreview: String(item?.resultPreview ?? '').trim()
		}))
		.filter((item) => item.agentId);
};

export const buildOpenClawWorkerCoordinatorBrief = (
	userPrompt:
		| string
		| null
		| undefined,
	job:
		| {
				status?: string | null;
				phase?: string | null;
				estimate?: {
					preferredInitialBatch?: string[] | null;
				} | null;
		  }
		| null,
	items: OpenClawWorkerSubagentProgressItem[]
): OpenClawWorkerCoordinatorBrief => {
	const requestSummary = summarizeOpenClawWorkerText(userPrompt, 140);
	const assignments = items
		.slice(0, 4)
		.map((item) => ({
			agentId: item.agentId,
			summary: summarizeOpenClawWorkerText(item.task || item.resultPreview, 88)
		}))
		.filter((item) => item.agentId && item.summary);

	let intro = '';
	if (isOpenClawWorkerTerminal(job)) {
		intro = '我已按协作方式处理完这项任务，下面可以直接看到分工记录和最终结果。';
	} else if (assignments.length > 1) {
		intro = '我已接到你的请求，正在按分工推进，下面是当前安排。';
	} else if (assignments.length === 1) {
		intro = `我已接到你的请求，先安排 ${assignments[0].agentId} 处理第一步，再根据结果继续推进。`;
	} else {
		const firstBatch = Array.isArray(job?.estimate?.preferredInitialBatch)
			? job?.estimate?.preferredInitialBatch?.filter((item): item is string => Boolean(item))
			: [];
		if (firstBatch.length > 0) {
			intro = `我已接到你的请求，先安排 ${firstBatch.join('、')} 开始，随后再补后续角色。`;
		} else {
			intro = '我已接到你的请求，正在把它拆成可执行的协作步骤。';
		}
	}

	return {
		requestSummary,
		intro,
		assignments
	};
};

export const buildOpenClawWorkerResponsesResult = (
	modelId: string,
	ackText: string
): OpenClawWorkerResponse => {
	const messageId = `msg_worker_${crypto.randomUUID()}`;

	return {
		id: `resp_worker_${crypto.randomUUID()}`,
		object: 'response',
		created_at: Math.floor(Date.now() / 1000),
		status: 'completed',
		model: modelId,
		output: [
			{
				id: messageId,
				type: 'message',
				role: 'assistant',
				status: 'completed',
				content: [
					{
						type: 'output_text',
						text: ackText
					}
				]
			}
		],
		usage: {
			input_tokens: 0,
			output_tokens: 0,
			total_tokens: 0
		}
	};
};

export const buildOpenClawWorkerResponsesStreamLines = (
	response: OpenClawWorkerResponse
): string[] => {
	const message = response.output[0];
	const part = message?.content?.[0];
	const text = part?.text ?? '';
	const startedResponse = {
		...response,
		status: 'in_progress',
		output: []
	};

	return [
		`data: ${JSON.stringify({ type: 'response.created', response: startedResponse })}`,
		`data: ${JSON.stringify({ type: 'response.in_progress', response: startedResponse })}`,
		`data: ${JSON.stringify({ type: 'response.output_item.added', output_index: 0, item: { ...message, status: 'in_progress', content: [] } })}`,
		`data: ${JSON.stringify({ type: 'response.content_part.added', item_id: message.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '' } })}`,
		`data: ${JSON.stringify({ type: 'response.output_text.delta', item_id: message.id, output_index: 0, content_index: 0, delta: text })}`,
		`data: ${JSON.stringify({ type: 'response.output_text.done', item_id: message.id, output_index: 0, content_index: 0, text })}`,
		`data: ${JSON.stringify({ type: 'response.content_part.done', item_id: message.id, output_index: 0, content_index: 0, part: { type: 'output_text', text } })}`,
		`data: ${JSON.stringify({ type: 'response.output_item.done', output_index: 0, item: message })}`,
		`data: ${JSON.stringify({ type: 'response.completed', response })}`
	];
};
