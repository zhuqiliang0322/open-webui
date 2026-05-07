import { EventSourceParserStream } from 'eventsource-parser/stream';
import type { ParsedEvent } from 'eventsource-parser';

type TextStreamUpdate = {
	done: boolean;
	value: string;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	sources?: any;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	selectedModelId?: any;
	error?: any;
	usage?: ResponseUsage;
};

type ResponseUsage = {
	/** Including images and tools if any */
	prompt_tokens: number;
	/** The tokens generated */
	completion_tokens: number;
	/** Sum of the above two fields */
	total_tokens: number;
	/** Any other fields that aren't part of the base OpenAI spec */
	[other: string]: unknown;
};

const normalizeResponsesUsage = (
	usage: Record<string, unknown> | undefined
): ResponseUsage | null => {
	if (!usage || typeof usage !== 'object') {
		return null;
	}

	const promptTokens = Number(usage.prompt_tokens ?? usage.input_tokens ?? 0);
	const completionTokens = Number(usage.completion_tokens ?? usage.output_tokens ?? 0);
	const totalTokens = Number(usage.total_tokens ?? promptTokens + completionTokens);

	return {
		...usage,
		prompt_tokens: Number.isFinite(promptTokens) ? promptTokens : 0,
		completion_tokens: Number.isFinite(completionTokens) ? completionTokens : 0,
		total_tokens: Number.isFinite(totalTokens) ? totalTokens : 0
	};
};

// createOpenAITextStream takes a responseBody with a SSE response,
// and returns an async generator that emits delta updates with large deltas chunked into random sized chunks
export async function createOpenAITextStream(
	responseBody: ReadableStream<Uint8Array>,
	splitLargeDeltas: boolean
): Promise<AsyncGenerator<TextStreamUpdate>> {
	const eventStream = responseBody
		.pipeThrough(new TextDecoderStream())
		.pipeThrough(new EventSourceParserStream())
		.getReader();
	let iterator = openAIStreamToIterator(eventStream);
	if (splitLargeDeltas) {
		iterator = streamLargeDeltasAsRandomChunks(iterator);
	}
	return iterator;
}

async function* openAIStreamToIterator(
	reader: ReadableStreamDefaultReader<ParsedEvent>
): AsyncGenerator<TextStreamUpdate> {
	while (true) {
		const { value, done } = await reader.read();
		if (done) {
			yield { done: true, value: '' };
			break;
		}
		if (!value) {
			continue;
		}
		const data = value.data;
		if (data.startsWith('[DONE]')) {
			yield { done: true, value: '' };
			break;
		}

		try {
			const parsedData = JSON.parse(data);

			if (parsedData.error) {
				yield { done: true, value: '', error: parsedData.error };
				break;
			}

			if (parsedData.sources) {
				yield { done: false, value: '', sources: parsedData.sources };
				continue;
			}

			if (parsedData.selected_model_id) {
				yield { done: false, value: '', selectedModelId: parsedData.selected_model_id };
				continue;
			}

			if (parsedData.usage) {
				yield { done: false, value: '', usage: parsedData.usage };
				continue;
			}

			if (typeof parsedData.type === 'string' && parsedData.type.startsWith('response.')) {
				if (parsedData.type === 'response.output_text.delta') {
					yield {
						done: false,
						value: typeof parsedData.delta === 'string' ? parsedData.delta : ''
					};
					continue;
				}

				if (parsedData.type === 'response.completed') {
					const response = parsedData.response ?? {};
					const status = response?.status;
					const error =
						response?.error ??
						(parsedData.error || null) ??
						(typeof status === 'string' && status !== 'completed'
							? {
									message: `Responses API stream ended with status '${status}'.`,
									type: 'responses_api_error',
									status
								}
							: null);

					if (error) {
						yield {
							done: true,
							value: '',
							error
						};
						break;
					}

					const usage = normalizeResponsesUsage(response?.usage);
					if (usage) {
						yield { done: false, value: '', usage };
					}
					yield { done: true, value: '' };
					break;
				}

				if (parsedData.type === 'response.failed' || parsedData.type === 'response.incomplete') {
					yield {
						done: true,
						value: '',
						error: parsedData.response?.error ??
							parsedData.error ??
							parsedData.response?.incomplete_details ?? {
								message:
									parsedData.type === 'response.incomplete'
										? 'Responses API stream ended incomplete'
										: 'Responses API stream failed'
							}
					};
					break;
				}

				continue;
			}

			yield {
				done: false,
				value: parsedData.choices?.[0]?.delta?.content ?? ''
			};
		} catch (e) {
			console.error('Error extracting delta from SSE event:', e);
		}
	}
}

// streamLargeDeltasAsRandomChunks will chunk large deltas (length > 5) into random sized chunks between 1-3 characters
// This is to simulate a more fluid streaming, even though some providers may send large chunks of text at once
async function* streamLargeDeltasAsRandomChunks(
	iterator: AsyncGenerator<TextStreamUpdate>
): AsyncGenerator<TextStreamUpdate> {
	for await (const textStreamUpdate of iterator) {
		if (textStreamUpdate.done) {
			yield textStreamUpdate;
			return;
		}

		if (textStreamUpdate.error) {
			yield textStreamUpdate;
			continue;
		}
		if (textStreamUpdate.sources) {
			yield textStreamUpdate;
			continue;
		}
		if (textStreamUpdate.selectedModelId) {
			yield textStreamUpdate;
			continue;
		}
		if (textStreamUpdate.usage) {
			yield textStreamUpdate;
			continue;
		}

		let content = textStreamUpdate.value;
		if (content.length < 5) {
			yield { done: false, value: content };
			continue;
		}
		while (content != '') {
			const chunkSize = Math.min(Math.floor(Math.random() * 3) + 1, content.length);
			const chunk = content.slice(0, chunkSize);
			yield { done: false, value: chunk };
			// Do not sleep if the tab is hidden
			// Timers are throttled to 1s in hidden tabs
			if (document?.visibilityState !== 'hidden') {
				await sleep(5);
			}
			content = content.slice(chunkSize);
		}
	}
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
