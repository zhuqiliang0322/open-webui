type TranslateFn = (key: string) => string;

const identity = (key: string) => key;

const getTranslate = (translate?: TranslateFn) => translate ?? identity;

export const extractOpenAIErrorMessage = (error: unknown): string => {
	if (typeof error === 'string') {
		return error;
	}

	if (error == null) {
		return '';
	}

	if (typeof error !== 'object') {
		return String(error);
	}

	if ('content' in error) {
		return extractOpenAIErrorMessage(error.content);
	}

	if ('detail' in error) {
		return extractOpenAIErrorMessage(error.detail);
	}

	if ('message' in error) {
		return extractOpenAIErrorMessage(error.message);
	}

	if ('error' in error) {
		return extractOpenAIErrorMessage(error.error);
	}

	try {
		return JSON.stringify(error);
	} catch {
		return String(error);
	}
};

const appendRawError = (headline: string, raw: string) => {
	const friendly = headline.trim();
	const original = raw.trim();

	if (!original || !friendly || original === friendly || friendly.includes(original)) {
		return friendly || original;
	}

	return `${friendly}\n${original}`;
};

export const humanizeOpenAIErrorMessage = (error: unknown, translate?: TranslateFn): string => {
	const t = getTranslate(translate);
	const raw = extractOpenAIErrorMessage(error).trim();

	if (!raw) {
		return '';
	}

	const knownFriendlyMessages = [
		t('The request is too long for the current model.'),
		t('The upstream model is currently unavailable or was unloaded.'),
		t('The upstream service rejected this request format or tool payload.'),
		t('The upstream service stopped before producing a final answer.')
	];

	if (knownFriendlyMessages.some((message) => raw.startsWith(message))) {
		return raw;
	}

	const normalized = raw.toLowerCase();

	if (normalized.includes('context size has been exceeded')) {
		return appendRawError(t('The request is too long for the current model.'), raw);
	}

	if (
		normalized.includes('model unloaded') ||
		normalized.includes('failed to load model') ||
		normalized.includes('operation canceled')
	) {
		return appendRawError(
			t('The upstream model is currently unavailable or was unloaded.'),
			raw
		);
	}

	if (
		normalized.includes('provider rejected the request schema or tool payload') ||
		normalized.includes('(format)')
	) {
		return appendRawError(
			t('The upstream service rejected this request format or tool payload.'),
			raw
		);
	}

	if (
		(normalized.includes('responses api stream ended with status') ||
			normalized.includes('responses api request ended with status')) &&
		normalized.includes("'failed'")
	) {
		return [
			t('The upstream service stopped before producing a final answer.'),
			t('Common causes: the conversation is too long, the model is unavailable, or a sub-task failed.')
		].join('\n');
	}

	return raw;
};

export const normalizeChatMessageError = (error: unknown, translate?: TranslateFn) => {
	const content = humanizeOpenAIErrorMessage(error, translate);

	if (error && typeof error === 'object' && !Array.isArray(error)) {
		return {
			...error,
			content
		};
	}

	return { content };
};
