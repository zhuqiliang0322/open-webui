const prompt =
	'这是一个多角色任务。必须实际调用多个 agent。heavy 给 1 条中文风险提示，coder 给 2 条本地检查命令，visual 给 3 行展示结构，release 给最终中文汇总。不要联网，不要读写文件。';

const label = (en: string, zh: string) => new RegExp(`^(${en}|${zh})$`, 'i');

const getWorkerCard = () =>
	cy.contains(/Collaborative Task|协作任务/i, { timeout: 60_000 }).closest('div.rounded-2xl');

const parseProgress = (text: string) => {
	const match = text.match(/(\d+)\s*\/\s*(\d+)\s*(Completed|已完成)/i);
	if (!match) {
		return null;
	}

	return {
		completed: Number(match[1]),
		total: Number(match[2])
	};
};

const waitForInProgressSubtasks = (deadlineMs: number, startedAt = Date.now()): Cypress.Chainable<void> => {
	return getWorkerCard()
		.contains(/Subtasks|子任务/i, { timeout: 120_000 })
		.then(() => getWorkerCard().invoke('text'))
		.then((text) => {
			const progress = parseProgress(text);
			if (progress && progress.total > 0 && progress.completed < progress.total) {
				expect(text).to.match(/Running|运行中/i);
				return;
			}

			if (Date.now() - startedAt > deadlineMs) {
				throw new Error(`Timed out waiting for in-progress subtask feedback. Latest worker card:\n${text}`);
			}

			return cy.wait(2_000).then(() => waitForInProgressSubtasks(deadlineMs, startedAt));
		});
};

describe('OpenClaw worker multi-agent regression', () => {
	it('shows live subtask progress and finishes with a terminal final result', () => {
		const token = String(Cypress.env('OPENWEBUI_TOKEN') || '');
		expect(token, 'OPENWEBUI_TOKEN').to.not.equal('');

		cy.viewport(1440, 1200);
		cy.visit(`/?models=openclaw/main&q=${encodeURIComponent(prompt)}&submit=true`, {
			onBeforeLoad(win) {
				win.localStorage.setItem('token', token);
				win.localStorage.setItem('locale', 'en-US');
				win.localStorage.setItem('version', 'test');
			}
		});

		getWorkerCard().contains(/Collaborative Task|协作任务/i).should('exist');
		getWorkerCard().contains(/Task Summary|任务摘要/i).should('exist');
		getWorkerCard().should('not.contain.text', 'OpenClaw Worker');
		getWorkerCard().should('not.contain.text', 'initial phase');
		getWorkerCard().screenshot('openclaw-worker-ack');

		waitForInProgressSubtasks(180_000);
		getWorkerCard().contains(/My Plan|我的安排/i, { timeout: 60_000 }).should('exist');
		getWorkerCard().screenshot('openclaw-worker-running');

		let jobId = '';
		getWorkerCard()
			.invoke('text')
			.then((text) => {
				const match = text.match(/[a-f0-9]{32,36}(?:-[a-f0-9]{4,12})*/i);
				expect(match, `worker card should contain a job id: ${text}`).to.not.be.null;
				jobId = match![0];
			});

		getWorkerCard().contains(/Final result|最终结果/i, { timeout: 240_000 }).should('exist');
		getWorkerCard().contains(label('Completed', '已完成'), { timeout: 240_000 }).should('exist');
		getWorkerCard().screenshot('openclaw-worker-completed');

		cy.then(() => {
			expect(jobId, 'resolved job id').to.not.equal('');
			cy.request({
				method: 'GET',
				url: `/openai/worker/jobs/${jobId}?model=openclaw/main`,
				headers: {
					Authorization: `Bearer ${token}`
				}
			}).then(({ body }) => {
				expect(body.phase).to.equal('completed');
				expect(['completed', 'succeeded']).to.include(body.status);
				expect(body.final_visible_text).to.be.a('string').and.not.be.empty;
				expect(body.subagent_progress).to.have.property('items');
				expect(body.subagent_progress.items.length).to.be.greaterThan(0);
				expect(body.subagent_progress.items.every((item) => item.state === 'completed')).to.equal(true);
			});
		});
	});
});
