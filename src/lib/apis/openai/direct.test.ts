import { describe, expect, it } from 'vitest';

import {
	convertResponsesResultToChatCompletion,
	prepareDirectOpenAIRequest
} from './direct';

describe('prepareDirectOpenAIRequest', () => {
	it('keeps chat completions requests unchanged for non-responses connections', () => {
		const payload = {
			model: 'openclaw/main',
			stream: true,
			messages: [{ role: 'user', content: 'hello' }]
		};

		expect(prepareDirectOpenAIRequest('http://localhost:8080/v1/', {}, payload)).toEqual({
			requestUrl: 'http://localhost:8080/v1/chat/completions',
			requestBody: payload,
			isResponses: false
		});
	});

	it('converts chat completions payloads for responses connections', () => {
		const payload = {
			model: 'openclaw/main',
			stream: true,
			max_tokens: 128,
			stop: ['END'],
			stream_options: { include_usage: true },
			tools: [
				{
					type: 'function',
					function: {
						name: 'lookup',
						description: 'search',
						parameters: { type: 'object' }
					}
				}
			],
			messages: [
				{ role: 'system', content: 'system prompt' },
				{
					role: 'user',
					content: [
						{ type: 'text', text: 'hello' },
						{ type: 'image_url', image_url: { url: 'https://example.com/a.png' } }
					]
				},
				{
					role: 'assistant',
					output: [
						{
							type: 'message',
							role: 'assistant',
							content: [{ type: 'output_text', text: 'previous answer' }],
							id: 'msg_1',
							status: 'completed'
						}
					]
				},
				{
					role: 'assistant',
					content: 'tool call intro',
					tool_calls: [
						{
							id: 'call_1',
							function: { name: 'lookup', arguments: '{"q":"hello"}' }
						}
					]
				},
				{
					role: 'tool',
					tool_call_id: 'call_1',
					content: '{"result":"world"}'
				}
			]
		};

		const result = prepareDirectOpenAIRequest('http://localhost:8080/v1/', { api_type: 'responses' }, payload);

		expect(result.requestUrl).toBe('http://localhost:8080/v1/responses');
		expect(result.isResponses).toBe(true);
		expect(result.requestBody).toMatchObject({
			model: 'openclaw/main',
			stream: true,
			instructions: 'system prompt',
			max_output_tokens: 128,
			tools: [
				{
					type: 'function',
					name: 'lookup',
					description: 'search',
					parameters: { type: 'object' }
				}
			],
			input: [
				{
					type: 'message',
					role: 'user',
					content: [
						{ type: 'input_text', text: 'hello' },
						{ type: 'input_image', image_url: 'https://example.com/a.png' }
					]
				},
				{
					type: 'message',
					role: 'assistant',
					content: [{ type: 'output_text', text: 'previous answer' }]
				},
				{
					type: 'message',
					role: 'assistant',
					content: [{ type: 'output_text', text: 'tool call intro' }]
				},
				{
					type: 'function_call',
					call_id: 'call_1',
					name: 'lookup',
					arguments: '{"q":"hello"}'
				},
				{
					type: 'function_call_output',
					call_id: 'call_1',
					output: '{"result":"world"}'
				}
			]
		});
		expect(result.requestBody).not.toHaveProperty('messages');
		expect(result.requestBody).not.toHaveProperty('stop');
		expect(result.requestBody).not.toHaveProperty('stream_options');
	});
});

describe('convertResponsesResultToChatCompletion', () => {
	it('extracts output text into chat completions shape', () => {
		expect(
			convertResponsesResultToChatCompletion({
				id: 'resp_1',
				model: 'openclaw/main',
				output: [
					{
						type: 'message',
						content: [
							{ type: 'output_text', text: 'hello ' },
							{ type: 'output_text', text: 'world' }
						]
					}
				],
				usage: { total_tokens: 10 }
			})
		).toEqual({
			id: 'resp_1',
			object: 'chat.completion',
			model: 'openclaw/main',
			choices: [
				{
					index: 0,
					message: {
						role: 'assistant',
						content: 'hello world'
					},
					finish_reason: 'stop'
				}
			],
			usage: { total_tokens: 10 }
		});
	});

	it('returns an error payload when the responses request fails', () => {
		expect(
			convertResponsesResultToChatCompletion({
				id: 'resp_failed',
				model: 'openclaw/main',
				status: 'failed'
			})
		).toEqual({
			error: {
				message: "Responses API request ended with status 'failed'.",
				type: 'responses_api_error',
				status: 'failed'
			}
		});
	});
});
