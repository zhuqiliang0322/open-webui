import { describe, expect, it } from 'vitest';

import { createOpenAITextStream } from './index';

const streamFromEvents = (events: string[]) =>
	new ReadableStream<Uint8Array>({
		start(controller) {
			const encoder = new TextEncoder();
			for (const event of events) {
				controller.enqueue(encoder.encode(`${event}\n\n`));
			}
			controller.close();
		}
	});

const collectUpdates = async (events: string[]) => {
	const iterator = await createOpenAITextStream(streamFromEvents(events), false);
	const updates = [];
	for await (const update of iterator) {
		updates.push(update);
	}
	return updates;
};

describe('createOpenAITextStream', () => {
	it('keeps OpenAI chat-completions streaming unchanged', async () => {
		const updates = await collectUpdates([
			`data: ${JSON.stringify({ choices: [{ delta: { content: 'Hi' } }] })}`,
			'data: [DONE]'
		]);

		expect(updates.map((update) => update.value)).toEqual(['Hi', '']);
		expect(updates.at(-1)?.done).toBe(true);
	});

	it('streams Responses API output text deltas without duplicating the done text', async () => {
		const updates = await collectUpdates([
			`data: ${JSON.stringify({ type: 'response.output_text.delta', delta: '你' })}`,
			`data: ${JSON.stringify({ type: 'response.output_text.delta', delta: '好' })}`,
			`data: ${JSON.stringify({ type: 'response.output_text.done', text: '你好' })}`,
			`data: ${JSON.stringify({
				type: 'response.completed',
				response: { usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 } }
			})}`
		]);

		expect(updates.filter((update) => update.value).map((update) => update.value)).toEqual([
			'你',
			'好'
		]);
		expect(updates.find((update) => update.usage)?.usage).toMatchObject({
			prompt_tokens: 3,
			completion_tokens: 2,
			total_tokens: 5
		});
		expect(updates.at(-1)).toMatchObject({ done: true, value: '' });
	});

	it('surfaces Responses API stream failures', async () => {
		const updates = await collectUpdates([
			`data: ${JSON.stringify({
				type: 'response.failed',
				response: { error: { message: 'bad response' } }
			})}`
		]);

		expect(updates.at(-1)).toMatchObject({
			done: true,
			value: '',
			error: { message: 'bad response' }
		});
	});

	it('surfaces completed Responses API events with failed status', async () => {
		const updates = await collectUpdates([
			`data: ${JSON.stringify({
				type: 'response.output_text.delta',
				delta: 'partial'
			})}`,
			`data: ${JSON.stringify({
				type: 'response.completed',
				response: {
					status: 'failed',
					error: { message: 'context size has been exceeded' }
				}
			})}`
		]);

		expect(updates.at(-1)).toMatchObject({
			done: true,
			value: '',
			error: { message: 'context size has been exceeded' }
		});
	});
});
