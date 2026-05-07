import { describe, expect, it } from 'vitest';

import {
	extractOpenAIErrorMessage,
	humanizeOpenAIErrorMessage,
	normalizeChatMessageError
} from './openai-errors';

describe('openai-errors', () => {
	it('extracts nested error messages', () => {
		expect(
			extractOpenAIErrorMessage({
				error: {
					message: 'Context size has been exceeded.'
				}
			})
		).toBe('Context size has been exceeded.');
	});

	it('humanizes failed responses api status errors', () => {
		expect(humanizeOpenAIErrorMessage("Responses API stream ended with status 'failed'.")).toBe(
			[
				'The upstream service stopped before producing a final answer.',
				'Common causes: the conversation is too long, the model is unavailable, or a sub-task failed.'
			].join('\n')
		);
	});

	it('humanizes context limit errors', () => {
		expect(humanizeOpenAIErrorMessage('Context size has been exceeded.')).toBe(
			[
				'The request is too long for the current model.',
				'Context size has been exceeded.'
			].join('\n')
		);
	});

	it('normalizes chat message errors to a content object', () => {
		expect(
			normalizeChatMessageError({
				content: 'provider rejected the request schema or tool payload'
			})
		).toEqual({
			content: [
				'The upstream service rejected this request format or tool payload.',
				'provider rejected the request schema or tool payload'
			].join('\n')
		});
	});
});
