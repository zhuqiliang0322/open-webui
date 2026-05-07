import { describe, expect, it } from 'vitest';

import {
	buildOpenClawWorkerCoordinatorBrief,
	buildOpenClawWorkerResponsesResult,
	buildOpenClawWorkerResponsesStreamLines,
	buildOpenClawWorkerRenderableFinalText,
	buildOpenClawWorkerStatusHistory,
	extractOpenClawWorkerLocalFileReference,
	extractOpenClawWorkerJobId,
	parseOpenClawWorkerArtifactFilename,
	getOpenClawWorkerSubagentPhaseKey,
	getOpenClawWorkerSubagentItems,
	getOpenClawWorkerPhaseKey,
	hasOpenClawWorkerDisplayableResult,
	isOpenClawWorkerRenderableFinalText,
	isOpenClawWorkerTerminal,
	shouldPollOpenClawWorkerJob,
	shouldRenderOpenClawWorkerFinalResult,
	shouldOpenClawWorkerArtifactInline
} from './openclaw-worker';

describe('openclaw worker helpers', () => {
	it('extracts a job id from the OpenClaw worker ack block', () => {
		const content = [
			'<!-- OpenClaw Worker | job id: `abc12345-def6-7890-abcd-ef1234567890` -->',
			'',
			'已接到你的请求，正在按协作方式处理。'
		].join('\n');

		expect(extractOpenClawWorkerJobId(content)).toBe('abc12345-def6-7890-abcd-ef1234567890');
	});

	it('extracts a job id from the legacy OpenClaw ack line', () => {
		expect(extractOpenClawWorkerJobId('OpenClaw job `deadbeef-1234-5678-90ab-cdef12345678`')).toBe(
			'deadbeef-1234-5678-90ab-cdef12345678'
		);
	});

	it('maps worker phases into visible status labels', () => {
		expect(getOpenClawWorkerPhaseKey({ phase: 'loading_model', status: 'running' })).toBe(
			'Loading model'
		);
		expect(getOpenClawWorkerPhaseKey({ phase: 'completed', status: 'succeeded' })).toBe(
			'Completed'
		);
		expect(getOpenClawWorkerPhaseKey({ phase: 'failed', status: 'failed' })).toBe('Failed');
	});

	it('builds de-duplicated status history entries', () => {
		const history = buildOpenClawWorkerStatusHistory({
			phase: 'completed',
			status: 'succeeded',
			status_history: [
				{ phase: 'queued', status: 'queued' },
				{ phase: 'started', status: 'running' },
				{ phase: 'loading_model', status: 'running' },
				{ phase: 'running', status: 'running' },
				{ phase: 'completed', status: 'succeeded' }
			]
		});

		expect(history.map((entry) => entry.description)).toEqual([
			'Queued',
			'Running',
			'Loading model',
			'Running',
			'Completed'
		]);
		expect(history.at(-1)?.done).toBe(true);
	});

	it('marks terminal jobs correctly', () => {
		expect(isOpenClawWorkerTerminal({ phase: 'completed', status: 'succeeded' })).toBe(true);
		expect(isOpenClawWorkerTerminal({ phase: 'running', status: 'running' })).toBe(false);
	});

	it('renders displayable final results for terminal jobs', () => {
		expect(
			shouldRenderOpenClawWorkerFinalResult({
				phase: 'completed',
				status: 'succeeded',
				final_visible_text: '图片已生成。'
			})
		).toBe(true);
		expect(
			shouldRenderOpenClawWorkerFinalResult({
				phase: 'failed',
				status: 'failed',
				final_visible_text: '任务失败：模型返回错误。'
			})
		).toBe(true);
		expect(
			shouldRenderOpenClawWorkerFinalResult({
				phase: 'failed',
				status: 'failed',
				final_visible_text: ''
			})
		).toBe(false);
	});

	it('keeps polling successful terminal jobs until a displayable result arrives', () => {
		const emptyTerminalJob = {
			phase: 'completed',
			status: 'succeeded',
			final_visible_text: ''
		};
		const terminalJobWithMedia = {
			...emptyTerminalJob,
			media_urls: ['https://example.test/result.png']
		};

		expect(hasOpenClawWorkerDisplayableResult(emptyTerminalJob)).toBe(false);
		expect(shouldPollOpenClawWorkerJob(emptyTerminalJob)).toBe(true);
		expect(hasOpenClawWorkerDisplayableResult(terminalJobWithMedia)).toBe(true);
		expect(shouldPollOpenClawWorkerJob(terminalJobWithMedia)).toBe(false);
		expect(shouldRenderOpenClawWorkerFinalResult(terminalJobWithMedia)).toBe(true);
	});

	it('does not treat malformed artifact or media items as displayable results', () => {
		const malformedTerminalJob = {
			phase: 'completed',
			status: 'succeeded',
			final_visible_text: '',
			resolved_artifacts: [{}],
			media_urls: ['not-a-url']
		};

		expect(hasOpenClawWorkerDisplayableResult(malformedTerminalJob)).toBe(false);
		expect(shouldPollOpenClawWorkerJob(malformedTerminalJob)).toBe(true);
		expect(shouldRenderOpenClawWorkerFinalResult(malformedTerminalJob)).toBe(false);
	});

	it('treats waiting placeholder text with active subtasks as non-terminal', () => {
		const job = {
			phase: 'completed',
			status: 'succeeded',
			final_visible_text: '多角色协调进行中。',
			subagent_progress: {
				activeCount: 2
			}
		};

		expect(isOpenClawWorkerTerminal(job)).toBe(false);
		expect(getOpenClawWorkerPhaseKey(job)).toBe('Running');
	});

	it('reads subagent progress items from the worker job payload', () => {
		const items = getOpenClawWorkerSubagentItems({
			subagent_progress: {
				items: [
					{
						sessionKey: 'agent:coder:subagent:child-1',
						agentId: 'coder',
						state: 'completed',
						task: '给出 2 条本地 CLI 自检命令。',
						status: 'completed successfully',
						resultPreview: '命令 A'
					},
					{
						sessionKey: 'agent:visual:subagent:child-2',
						agentId: 'visual',
						state: 'running',
						task: '给出 3 行显示结构。'
					}
				]
			}
		});

		expect(items).toEqual([
			{
				sessionKey: 'agent:coder:subagent:child-1',
				agentId: 'coder',
				state: 'completed',
				task: '给出 2 条本地 CLI 自检命令。',
				status: 'completed successfully',
				resultPreview: '命令 A'
			},
			{
				sessionKey: 'agent:visual:subagent:child-2',
				agentId: 'visual',
				state: 'running',
				task: '给出 3 行显示结构。',
				status: '',
				resultPreview: ''
			}
		]);
	});

	it('builds a main-agent brief from the user prompt and current assignments', () => {
		const brief = buildOpenClawWorkerCoordinatorBrief(
			'帮我安排一个多 agent 协作任务，最后给我中文汇总。',
			{
				phase: 'running',
				status: 'running'
			},
			[
				{
					agentId: 'coder',
					state: 'running',
					task: '给出 2 条本地 CLI 自检命令。',
					status: '',
					resultPreview: ''
				},
				{
					agentId: 'release',
					state: 'running',
					task: '整理最终中文汇总。',
					status: '',
					resultPreview: ''
				}
			]
		);

		expect(brief.requestSummary).toContain('多 agent 协作任务');
		expect(brief.intro).toContain('正在按分工推进');
		expect(brief.assignments).toEqual([
			{ agentId: 'coder', summary: '给出 2 条本地 CLI 自检命令。' },
			{ agentId: 'release', summary: '整理最终中文汇总。' }
		]);
	});

	it('maps completed subagent statuses into visible labels', () => {
		expect(getOpenClawWorkerSubagentPhaseKey({ state: 'running', status: '' })).toBe('Running');
		expect(getOpenClawWorkerSubagentPhaseKey({ state: 'completed', status: 'completed successfully' })).toBe(
			'Completed'
		);
		expect(getOpenClawWorkerSubagentPhaseKey({ state: 'completed', status: 'stalled' })).toBe(
			'Timed out'
		);
		expect(getOpenClawWorkerSubagentPhaseKey({ state: 'completed', status: 'failed with context error' })).toBe(
			'Failed'
		);
		expect(getOpenClawWorkerSubagentPhaseKey({ state: 'completed', status: 'cancelled by user' })).toBe(
			'Cancelled'
		);
	});

	it('builds a synthetic responses result for worker acknowledgements', () => {
		const response = buildOpenClawWorkerResponsesResult(
			'openclaw/main',
			'<!-- OpenClaw Worker | job id: `job-web-001` -->\n已接到你的请求，正在按协作方式处理。'
		);

		expect(response.model).toBe('openclaw/main');
		expect(response.output[0].content[0].text).toContain('job-web-001');
	});

	it('builds responses stream lines for worker acknowledgements', () => {
		const response = buildOpenClawWorkerResponsesResult(
			'openclaw/main',
			'<!-- OpenClaw Worker | job id: `job-web-001` -->\n已接到你的请求，正在按协作方式处理。'
		);
		const lines = buildOpenClawWorkerResponsesStreamLines(response);

		expect(lines[0]).toContain('"type":"response.created"');
		expect(lines.at(-1)).toContain('"type":"response.completed"');
		expect(lines.join('\n')).toContain('job-web-001');
	});

	it('hides waiting placeholders from the final-result renderer', () => {
		expect(isOpenClawWorkerRenderableFinalText('多角色协调进行中。')).toBe(false);
		expect(isOpenClawWorkerRenderableFinalText('多角色协作已收口。')).toBe(true);
	});

	it('renders local image artifacts inline in the final result', () => {
		const rendered = buildOpenClawWorkerRenderableFinalText(
			'任务已完成。\n`~/OpenClaw/downloads/orange_cat_poster.png`',
			[
				{
					label: '~/OpenClaw/downloads/orange_cat_poster.png',
					path: '/Users/panda/OpenClaw/downloads/orange_cat_poster.png'
				}
			],
			[],
			{ id: 'job-web-001', model: 'openclaw/main' }
		);

		expect(rendered).toContain(
			'[~/OpenClaw/downloads/orange_cat_poster.png](openwebui://local-file?path=%2FUsers%2Fpanda%2FOpenClaw%2Fdownloads%2Forange_cat_poster.png&jobId=job-web-001&model=openclaw%2Fmain)'
		);
		expect(rendered).toContain(
			'![orange_cat_poster.png](openwebui://local-file?path=%2FUsers%2Fpanda%2FOpenClaw%2Fdownloads%2Forange_cat_poster.png&jobId=job-web-001&model=openclaw%2Fmain)'
		);
		expect(
			extractOpenClawWorkerLocalFileReference(
				'openwebui://local-file?path=%2FUsers%2Fpanda%2FOpenClaw%2Fdownloads%2Forange_cat_poster.png&jobId=job-web-001&model=openclaw%2Fmain'
			)
		).toEqual({
			path: '/Users/panda/OpenClaw/downloads/orange_cat_poster.png',
			jobId: 'job-web-001',
			modelId: 'openclaw/main'
		});
	});

	it('renders remote media urls inline when final text has not included them yet', () => {
		const rendered = buildOpenClawWorkerRenderableFinalText('', [], [
			'https://example.test/generated.png'
		]);

		expect(rendered).toBe('![generated-image-1](https://example.test/generated.png)');
	});

	it('parses artifact filenames from content disposition headers', () => {
		expect(
			parseOpenClawWorkerArtifactFilename("inline; filename*=UTF-8''market_conclusion.md")
		).toBe('market_conclusion.md');
		expect(parseOpenClawWorkerArtifactFilename('attachment; filename="visual_spec.md"')).toBe(
			'visual_spec.md'
		);
	});

	it('decides whether artifacts should open inline', () => {
		expect(shouldOpenClawWorkerArtifactInline('text/markdown; charset=utf-8', 'inline')).toBe(
			true
		);
		expect(shouldOpenClawWorkerArtifactInline('application/pdf', 'inline')).toBe(true);
		expect(shouldOpenClawWorkerArtifactInline('application/zip', 'attachment')).toBe(false);
	});
});
