<script lang="ts">
	import DOMPurify from 'dompurify';
	import { toast } from 'svelte-sonner';

	import type { Token } from 'marked';
	import { getContext, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';

	const i18n = getContext<any>('i18n');

	import { getOpenClawWorkerArtifactContent } from '$lib/apis/openai';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { showControls, showFileNavPath } from '$lib/stores';
	import { displayFileHandler, unescapeHtml } from '$lib/utils';
	import {
		extractOpenClawWorkerLocalFileReference,
		extractOpenClawWorkerLocalFilePath,
		parseOpenClawWorkerArtifactFilename,
		shouldOpenClawWorkerArtifactInline
	} from '$lib/utils/openclaw-worker';

	import Image from '$lib/components/common/Image.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import HtmlToken from './HTMLToken.svelte';
	import TextToken from './MarkdownInlineTokens/TextToken.svelte';
	import CodespanToken from './MarkdownInlineTokens/CodespanToken.svelte';
	import MentionToken from './MarkdownInlineTokens/MentionToken.svelte';
	import NoteLinkToken from './MarkdownInlineTokens/NoteLinkToken.svelte';
	import SourceToken from './SourceToken.svelte';

	export let id: string;
	export let done = true;
	export let tokens: Token[];
	export let sourceIds: string[] = [];
	export let onSourceClick: (...args: any[]) => void = () => {};

	const inlineImageObjectUrls = new Set<string>();
	let inlineImageSrcs: Record<string, string> = {};
	let inlineImageLoading: Record<string, boolean> = {};
	let inlineImageAutoRequested: Record<string, boolean> = {};

	const collectOpenClawWorkerLocalImageReferences = (
		items: Token[] | undefined | null
	): OpenClawWorkerLocalFileReference[] => {
		const discovered = new Map<string, OpenClawWorkerLocalFileReference>();

		const visit = (value: unknown) => {
			if (!value) {
				return;
			}
			if (Array.isArray(value)) {
				value.forEach(visit);
				return;
			}
			if (typeof value !== 'object') {
				return;
			}

			const token = value as {
				type?: string;
				href?: string;
				tokens?: Token[];
				items?: unknown[];
				rows?: unknown[];
				header?: unknown[];
			};
			if (token.type === 'image') {
				const localFileReference = extractOpenClawWorkerLocalFileReference(token.href);
				if (localFileReference) {
					discovered.set(localFileReference.path, localFileReference);
				}
			}
			if (Array.isArray(token.tokens)) {
				visit(token.tokens);
			}
			if (Array.isArray(token.items)) {
				visit(token.items);
			}
			if (Array.isArray(token.rows)) {
				visit(token.rows);
			}
			if (Array.isArray(token.header)) {
				visit(token.header);
			}
		};

		visit(items ?? []);
		return Array.from(discovered.values());
	};

	/**
	 * Check if a URL is a same-origin note link and return the note ID if so.
	 */
	const getNoteIdFromHref = (href: string): string | null => {
		try {
			const url = new URL(href, window.location.origin);
			if (url.origin === window.location.origin) {
				const match = url.pathname.match(/^\/notes\/([^/]+)$/);
				if (match) {
					return match[1];
				}
			}
		} catch {
			// Invalid URL
		}
		return null;
	};

	const triggerArtifactDownload = (objectUrl: string, filename: string) => {
		const anchor = document.createElement('a');
		anchor.href = objectUrl;
		anchor.download = filename;
		anchor.rel = 'noopener noreferrer';
		document.body.appendChild(anchor);
		anchor.click();
		anchor.remove();
		setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
	};

	type OpenClawWorkerLocalFileReference = {
		path: string;
		jobId: string | null;
		modelId: string | null;
	};

	const openOpenClawWorkerArtifact = async (reference: OpenClawWorkerLocalFileReference) => {
		const response = await getOpenClawWorkerArtifactContent(
			localStorage.token,
			reference.path,
			false,
			reference.jobId,
			reference.modelId
		);
		const contentDisposition = response.headers.get('content-disposition');
		const contentType = response.headers.get('content-type');
		const filename =
			parseOpenClawWorkerArtifactFilename(contentDisposition) ??
			reference.path.split('/').pop() ??
			'artifact';
		const objectUrl = URL.createObjectURL(await response.blob());

		if (shouldOpenClawWorkerArtifactInline(contentType, contentDisposition)) {
			const popup = window.open(objectUrl, '_blank', 'noopener,noreferrer');
			if (!popup) {
				triggerArtifactDownload(objectUrl, filename);
				return;
			}
			setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
			return;
		}

		triggerArtifactDownload(objectUrl, filename);
	};

	const showOpenClawWorkerInlineImage = async (
		reference: OpenClawWorkerLocalFileReference,
		options: {
			quiet?: boolean;
		} = {}
	) => {
		const normalizedPath = String(reference.path ?? '').trim();
		if (!normalizedPath) {
			return;
		}
		if (inlineImageSrcs[normalizedPath] || inlineImageLoading[normalizedPath]) {
			return;
		}

		inlineImageLoading = {
			...inlineImageLoading,
			[normalizedPath]: true
		};

		try {
			const response = await getOpenClawWorkerArtifactContent(
				localStorage.token,
				normalizedPath,
				false,
				reference.jobId,
				reference.modelId
			);
			const objectUrl = URL.createObjectURL(await response.blob());
			inlineImageObjectUrls.add(objectUrl);
			inlineImageSrcs = {
				...inlineImageSrcs,
				[normalizedPath]: objectUrl
			};
		} catch (error) {
			console.error(error);
			if (!options.quiet) {
				toast.error($i18n.t('Failed to load image preview.'));
			}
		} finally {
			const remaining = { ...inlineImageLoading };
			delete remaining[normalizedPath];
			inlineImageLoading = remaining;
		}
	};

	/**
	 * Handle link clicks - intercept same-origin app URLs for in-app navigation
	 */
	const handleLinkClick = async (e: MouseEvent, href: string) => {
		const localFileReference = extractOpenClawWorkerLocalFileReference(href);
		if (localFileReference) {
			e.preventDefault();
			try {
				await openOpenClawWorkerArtifact(localFileReference);
			} catch (error) {
				console.error(error);
				displayFileHandler(localFileReference.path, { showControls, showFileNavPath });
				toast.error($i18n.t('Failed to open artifact directly. Showing file browser instead.'));
			}
			return;
		}

		try {
			const url = new URL(href, window.location.origin);
			// Check if same origin and an in-app route
			if (
				url.origin === window.location.origin &&
				(url.pathname.startsWith('/notes/') ||
					url.pathname.startsWith('/c/') ||
					url.pathname.startsWith('/channels/'))
			) {
				e.preventDefault();
				goto(url.pathname + url.search + url.hash);
			}
		} catch {
			// Invalid URL, let browser handle it
		}
	};

	onDestroy(() => {
		for (const objectUrl of inlineImageObjectUrls) {
			URL.revokeObjectURL(objectUrl);
		}
		inlineImageObjectUrls.clear();
		inlineImageSrcs = {};
		inlineImageLoading = {};
		inlineImageAutoRequested = {};
	});

	$: {
		for (const localFileReference of collectOpenClawWorkerLocalImageReferences(tokens)) {
			const localFilePath = localFileReference.path;
			if (
				!inlineImageAutoRequested[localFilePath] &&
				!inlineImageSrcs[localFilePath] &&
				!inlineImageLoading[localFilePath]
			) {
				inlineImageAutoRequested = {
					...inlineImageAutoRequested,
					[localFilePath]: true
				};
				void showOpenClawWorkerInlineImage(localFileReference, { quiet: true });
			}
		}
	}
</script>

	{#each tokens as token, tokenIdx (tokenIdx)}
		{#if token.type === 'escape'}
			{unescapeHtml(token.text)}
		{:else if token.type === 'html'}
			<HtmlToken {id} {token} />
	{:else if token.type === 'link'}
		{@const localFilePath = extractOpenClawWorkerLocalFilePath(token.href)}
		{@const localFileReference = extractOpenClawWorkerLocalFileReference(token.href)}
		{@const noteId = localFilePath ? null : getNoteIdFromHref(token.href)}
		{#if localFilePath}
			<a
				href={token.href}
				class="codespan cursor-pointer"
				title={localFilePath}
				rel="nofollow"
				on:click={(e) => handleLinkClick(e, token.href)}
			>
				{#if token.tokens}
					<svelte:self id={`${id}-a`} tokens={token.tokens} {onSourceClick} {done} />
				{:else}
					{token.text}
				{/if}
			</a>
		{:else if noteId}
			<NoteLinkToken {noteId} href={token.href} />
		{:else if token.tokens}
			<a
				href={token.href}
				target="_blank"
				rel="nofollow"
				title={token.title}
				on:click={(e) => handleLinkClick(e, token.href)}
			>
				<svelte:self id={`${id}-a`} tokens={token.tokens} {onSourceClick} {done} />
			</a>
		{:else}
			<a
				href={token.href}
				target="_blank"
				rel="nofollow"
				title={token.title}
				on:click={(e) => handleLinkClick(e, token.href)}>{token.text}</a
			>
		{/if}
	{:else if token.type === 'image'}
		{@const localFilePath = extractOpenClawWorkerLocalFilePath(token.href)}
		{@const localFileReference = extractOpenClawWorkerLocalFileReference(token.href)}
		{#if localFilePath}
			{@const inlineImageSrc = inlineImageSrcs[localFilePath]}
			{@const inlineImagePending = !!inlineImageLoading[localFilePath]}
			{#if inlineImageSrc}
				<button
					type="button"
					class="cursor-pointer"
					title={localFilePath}
					on:click={() =>
						localFileReference ? openOpenClawWorkerArtifact(localFileReference) : undefined}
				>
					<Image src={inlineImageSrc} alt={token.text} />
				</button>
			{:else}
				<button
					type="button"
					class="codespan cursor-pointer"
					title={localFilePath}
					on:click={() =>
						localFileReference ? showOpenClawWorkerInlineImage(localFileReference) : undefined}
				>
					{inlineImagePending ? $i18n.t('Loading image preview...') : $i18n.t('Show image preview')}
				</button>
			{/if}
		{:else}
			<Image src={token.href} alt={token.text} />
		{/if}
	{:else if token.type === 'strong'}
		<strong><svelte:self id={`${id}-strong`} tokens={token.tokens} {onSourceClick} /></strong>
	{:else if token.type === 'em'}
		<em><svelte:self id={`${id}-em`} tokens={token.tokens} {onSourceClick} /></em>
	{:else if token.type === 'codespan'}
		<CodespanToken {token} {done} />
	{:else if token.type === 'br'}
		<br />
	{:else if token.type === 'del'}
		<del><svelte:self id={`${id}-del`} tokens={token.tokens} {onSourceClick} /></del>
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'iframe'}
		<iframe
			src="{WEBUI_BASE_URL}/api/v1/files/{token.fileId}/content"
			title={token.fileId}
			width="100%"
			frameborder="0"
			on:load={(e) => {
				try {
					const iframe = e.currentTarget as HTMLIFrameElement;
					const height = iframe.contentWindow?.document.body.scrollHeight;
					if (height) {
						iframe.style.height = height + 20 + 'px';
					}
				} catch {}
			}}
		></iframe>
	{:else if token.type === 'mention'}
		<MentionToken {token} />
	{:else if token.type === 'footnote'}
		{@html DOMPurify.sanitize(
			`<sup class="footnote-ref footnote-ref-text">${token.escapedText}</sup>`
		) || ''}
	{:else if token.type === 'citation'}
		{#if (sourceIds ?? []).length > 0}
			<SourceToken {id} {token} {sourceIds} onClick={onSourceClick} />
		{:else}
			<TextToken {token} {done} />
		{/if}
	{:else if token.type === 'text'}
		<TextToken {token} {done} />
	{/if}
{/each}
