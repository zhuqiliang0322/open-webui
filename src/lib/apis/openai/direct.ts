type OpenAIApiConfig = {
	api_type?: string;
};

type JsonRecord = Record<string, unknown>;

type DirectRequest = {
	requestUrl: string;
	requestBody: JsonRecord;
	isResponses: boolean;
};

const RESPONSES_ALLOWED_FIELDS: Record<string, Set<string>> = {
	message: new Set(['type', 'role', 'content']),
	function_call: new Set(['type', 'call_id', 'name', 'arguments', 'id']),
	function_call_output: new Set(['type', 'call_id', 'output'])
};

const TEXT_PART_TYPES = new Set(['text', 'input_text', 'output_text']);

const extractTextContent = (content: unknown): string => {
	if (typeof content === 'string') {
		return content;
	}

	if (Array.isArray(content)) {
		return content
			.filter((part) => TEXT_PART_TYPES.has(part?.type))
			.map((part) => part?.text ?? '')
			.join('\n');
	}

	return content == null ? '' : String(content);
};

const normalizeStoredOutputItem = (item: JsonRecord) => {
	const allowed = RESPONSES_ALLOWED_FIELDS[item?.type ?? ''];
	if (!allowed) {
		return item;
	}

	return Object.fromEntries(Object.entries(item).filter(([key]) => allowed.has(key)));
};

export const convertChatCompletionPayloadToResponsesPayload = (payload: JsonRecord) => {
	const responsesPayload = structuredClone(payload);
	const messages = Array.isArray(responsesPayload.messages) ? responsesPayload.messages : [];
	delete responsesPayload.messages;

	let instructions = '';
	const input: JsonRecord[] = [];

	for (const message of messages) {
		const typedMessage = (message ?? {}) as JsonRecord;
		const role = typedMessage.role ?? 'user';
		const content = typedMessage.content ?? '';
		const storedOutput = typedMessage.output;

		if (Array.isArray(storedOutput) && storedOutput.length > 0) {
			input.push(...storedOutput.map((item) => normalizeStoredOutputItem((item ?? {}) as JsonRecord)));
			continue;
		}

		if (role === 'system') {
			instructions = extractTextContent(content);
			continue;
		}

		if (
			role === 'assistant' &&
			Array.isArray(typedMessage.tool_calls) &&
			typedMessage.tool_calls.length > 0
		) {
			const text = extractTextContent(content);
			if (text.trim()) {
				input.push({
					type: 'message',
					role: 'assistant',
					content: [{ type: 'output_text', text }]
				});
			}

			for (const toolCall of typedMessage.tool_calls) {
				const typedToolCall = (toolCall ?? {}) as JsonRecord;
				const func = (typedToolCall.function ?? {}) as JsonRecord;
				input.push({
					type: 'function_call',
					call_id: typedToolCall.id ?? '',
					name: func.name ?? '',
					arguments: func.arguments ?? '{}'
				});
			}
			continue;
		}

		if (role === 'tool') {
			input.push({
				type: 'function_call_output',
				call_id: typedMessage.tool_call_id ?? '',
				output: content
			});
			continue;
		}

		const textType = role === 'assistant' ? 'output_text' : 'input_text';

		let contentParts: JsonRecord[] = [];
		if (typeof content === 'string') {
			contentParts = [{ type: textType, text: content }];
		} else if (Array.isArray(content)) {
			for (const part of content) {
				const typedPart = (part ?? {}) as JsonRecord;
				if (TEXT_PART_TYPES.has((typedPart.type as string | undefined) ?? '')) {
					contentParts.push({ type: textType, text: typedPart.text ?? '' });
				} else if (typedPart.type === 'image_url') {
					const imageData = typedPart.image_url;
					contentParts.push({
						type: 'input_image',
						image_url:
							typeof imageData === 'string'
								? imageData
								: ((imageData as JsonRecord | undefined)?.url ?? '')
					});
				}
			}
		} else {
			contentParts = [{ type: textType, text: String(content ?? '') }];
		}

		input.push({
			type: 'message',
			role,
			content: contentParts
		});
	}

	responsesPayload.input = input;

	if (instructions) {
		responsesPayload.instructions = instructions;
	}

	if ('max_tokens' in responsesPayload) {
		responsesPayload.max_output_tokens = responsesPayload.max_tokens;
		delete responsesPayload.max_tokens;
	}

	if ('max_completion_tokens' in responsesPayload) {
		responsesPayload.max_output_tokens = responsesPayload.max_completion_tokens;
		delete responsesPayload.max_completion_tokens;
	}

	for (const unsupportedKey of [
		'stream_options',
		'logit_bias',
		'frequency_penalty',
		'presence_penalty',
		'stop'
	]) {
		delete responsesPayload[unsupportedKey];
	}

	if (Array.isArray(responsesPayload.tools)) {
		responsesPayload.tools = responsesPayload.tools.map((tool) => {
			if (!tool || typeof tool !== 'object' || !tool.function) {
				return tool;
			}

			const typedTool = tool as JsonRecord;
			const func = (typedTool.function ?? {}) as JsonRecord;
			const convertedTool: JsonRecord = {
				type: typedTool.type ?? 'function',
				name: func.name ?? ''
			};

			if ('description' in func) {
				convertedTool.description = func.description;
			}
			if ('parameters' in func) {
				convertedTool.parameters = func.parameters;
			}
			if ('strict' in func) {
				convertedTool.strict = func.strict;
			}

			return convertedTool;
		});
	}

	return responsesPayload;
};

export const prepareDirectOpenAIRequest = (
	baseUrl: string,
	apiConfig: OpenAIApiConfig | null | undefined,
	payload: JsonRecord
): DirectRequest => {
	const normalizedBaseUrl = baseUrl.replace(/\/$/, '');
	const isResponses = apiConfig?.api_type === 'responses';

	if (!isResponses) {
		return {
			requestUrl: `${normalizedBaseUrl}/chat/completions`,
			requestBody: payload,
			isResponses: false
		};
	}

	return {
		requestUrl: `${normalizedBaseUrl}/responses`,
		requestBody: convertChatCompletionPayloadToResponsesPayload(payload),
		isResponses: true
	};
};

export const convertResponsesResultToChatCompletion = (response: JsonRecord) => {
	const status = response?.status;
	const error = response?.error;

	if (error && typeof error === 'object' && 'message' in error && error.message) {
		return {
			error: {
				...(error as JsonRecord),
				...(status ? { status } : {})
			}
		};
	}

	if (typeof error === 'string' && error) {
		return {
			error: {
				message: error,
				type: 'responses_api_error',
				...(status ? { status } : {})
			}
		};
	}

	if (typeof status === 'string' && status !== 'completed') {
		return {
			error: {
				message: `Responses API request ended with status '${status}'.`,
				type: 'responses_api_error',
				status
			}
		};
	}

	const output = Array.isArray(response?.output) ? response.output : [];
	let content = '';

	for (const item of output) {
		const typedItem = (item ?? {}) as JsonRecord;
		if (typedItem.type !== 'message' || !Array.isArray(typedItem.content)) {
			continue;
		}

		for (const part of typedItem.content) {
			const typedPart = (part ?? {}) as JsonRecord;
			if (typedPart.type === 'output_text') {
				content += (typedPart.text as string | undefined) ?? '';
			}
		}
	}

	return {
		id: response?.id ?? '',
		object: 'chat.completion',
		model: response?.model ?? '',
		choices: [
			{
				index: 0,
				message: {
					role: 'assistant',
					content
				},
				finish_reason: 'stop'
			}
		],
		usage: response?.usage ?? {}
	};
};
