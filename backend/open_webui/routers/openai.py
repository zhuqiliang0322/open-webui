import asyncio
import base64
from datetime import datetime
import hashlib
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse
from uuid import uuid4

import aiohttp
from aiocache import cached


from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from fastapi import Depends, HTTPException, Request, APIRouter, status
from fastapi.responses import (
    FileResponse,
    StreamingResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel, ConfigDict

from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.internal.db import get_async_session

from open_webui.models.models import Models
from open_webui.models.files import Files
from open_webui.models.access_grants import AccessGrants
from open_webui.models.groups import Groups
from open_webui.utils.access_control import has_connection_access, check_model_access
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.config import (
    CACHE_DIR,
    OPENCLAW_WORKER_API_BASE_URL,
    OPENCLAW_WORKER_API_TOKEN,
)
from open_webui.env import (
    MODELS_CACHE_TTL,
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    FORWARD_SESSION_INFO_HEADER_CHAT_ID,
    BYPASS_MODEL_ACCESS_CONTROL,
    ENABLE_OPENAI_API_PASSTHROUGH,
)
from open_webui.models.users import UserModel

from open_webui.constants import ERROR_MESSAGES


from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_system_prompt_to_body,
)
from open_webui.utils.misc import (
    convert_logit_bias_input_to_json,
    stream_chunks_handler,
)
from open_webui.utils.session_pool import (
    cleanup_response,
    get_session,
    stream_wrapper,
)

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.headers import include_user_info_headers
from open_webui.utils.anthropic import is_anthropic_url, get_anthropic_models

log = logging.getLogger(__name__)

OPENCLAW_WORKER_WAITING_RESULT_RE = re.compile(
    r'(已启动第?一批|已启动子会话|正在等待|等待各角色|收到完成后|稍后|waiting for|once they finish|'
    r'started the first batch|will summarize|will send.*later|多角色协调进行中|正在生成最终汇总|汇总生成中|'
    r'release 正在生成)',
    flags=re.IGNORECASE,
)
OPENCLAW_WORKER_THIN_RESULT_RE = re.compile(
    r'^(信息不足[。！!]?|暂无(?:可展示)?(?:最终)?结果[。！!]?|无(?:法)?(?:提供)?(?:更多)?信息[。！!]?|'
    r'没有(?:可展示)?(?:最终)?结果[。！!]?|无(?:可)?结果[。！!]?|no output|insufficient information[.!]?)$',
    flags=re.IGNORECASE,
)
OPENCLAW_WORKER_FAILED_STATUS_RE = re.compile(
    r'(?:^|\b)(?:fail|error|abort|model unloaded|context size has been exceeded)',
    flags=re.IGNORECASE,
)
OPENCLAW_WORKER_TIMED_OUT_STATUS_RE = re.compile(r'(?:timed?\s*out|timeout|stall)', flags=re.IGNORECASE)
OPENCLAW_WORKER_CANCELLED_STATUS_RE = re.compile(r'cancel', flags=re.IGNORECASE)
OPENCLAW_WORKER_CONTROL_TOKEN_RE = re.compile(r'<\|[^>\n]*\|?>|<channel\|>', flags=re.IGNORECASE)
OPENCLAW_WORKER_INTERMEDIATE_CHILD_RESULT_RE = re.compile(
    r'^(?:now let me|let me |i(?: am|\'m) going to|i will |first[, ]|让我|我将|接下来|需要继续|继续(?:读取|查看|探索)|现在让我)',
    flags=re.IGNORECASE,
)
OPENCLAW_WORKER_AUXILIARY_METADATA_TRANSCRIPT_PATTERNS = (
    re.compile(r'Generate a concise,\s*3-5 word title with an emoji summarizing the chat history\.', flags=re.IGNORECASE),
    re.compile(r'Suggest 3-5 relevant follow-up questions or prompts', flags=re.IGNORECASE),
    re.compile(
        r'Generate 1-3 broad tags categorizing the main themes of the chat history',
        flags=re.IGNORECASE,
    ),
    re.compile(r'Generate a detailed prompt for am image generation task', flags=re.IGNORECASE),
)
OPENCLAW_WORKER_ARTIFACT_CODE_SPAN_RE = re.compile(r'`([^`\n]{1,200})`')
OPENCLAW_WORKER_ARTIFACT_CONTEXT_AGENT_RE = re.compile(
    r'\b(release|heavy|visual|coder|ops|main)\b',
    flags=re.IGNORECASE,
)
OPENCLAW_WORKER_ARTIFACT_FILE_EXTENSIONS = {
    '.csv',
    '.css',
    '.doc',
    '.docx',
    '.gif',
    '.htm',
    '.html',
    '.jpeg',
    '.jpg',
    '.js',
    '.json',
    '.md',
    '.mjs',
    '.pdf',
    '.png',
    '.ppt',
    '.pptx',
    '.py',
    '.scss',
    '.sh',
    '.sql',
    '.svg',
    '.ts',
    '.tsx',
    '.txt',
    '.webp',
    '.xls',
    '.xlsx',
    '.yaml',
    '.yml',
}
OPENCLAW_WORKER_ARTIFACT_INLINE_MEDIA_TYPES = {
    'application/javascript',
    'application/json',
    'application/pdf',
    'application/xml',
    'image/svg+xml',
}
OPENCLAW_WORKER_FILE_ID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
OPENCLAW_WORKER_FILE_PATH_RE = re.compile(
    r'/api/v\d+/files/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
)
OPENCLAW_WORKER_FILE_TAG_RE = re.compile(r'<file\s+([^>]+?)/?>', flags=re.IGNORECASE)
OPENCLAW_WORKER_FILE_TAG_ATTR_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_-]*)="([^"]*)"')
OPENCLAW_WORKER_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg'}
OPENCLAW_WORKER_DATA_URL_RE = re.compile(r'^data:([\w.+/-]+);base64,(.+)$', re.IGNORECASE | re.DOTALL)
OPENCLAW_WORKER_LOCAL_HOSTNAMES = {'127.0.0.1', 'localhost'}
OPENCLAW_WORKER_INPUT_CACHE_MAX_FILES = 128
OPENCLAW_WORKER_INPUT_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


##########################################
#
# Utility functions
# Let the responses returned through this gate be worth
# the question that summoned them.
#
##########################################

# Headers that become stale after aiohttp auto-decompresses the upstream
# response body.  Forwarding them verbatim causes desktop / programmatic
# clients to attempt decompression of an already-decoded payload, resulting
# in ZlibError.  See https://github.com/aio-libs/aiohttp/issues/4462.
_STRIP_PROXY_HEADERS = frozenset({'Content-Encoding', 'Content-Length', 'Transfer-Encoding'})


def _clean_proxy_headers(raw_headers) -> dict:
    """Return a copy of *raw_headers* with stale encoding headers removed."""
    return {k: v for k, v in raw_headers.items() if k not in _STRIP_PROXY_HEADERS}


async def send_get_request(
    request: Request = None,
    url=None,
    key=None,
    user: UserModel = None,
    config=None,
):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            if request and config:
                headers, cookies = await get_headers_and_cookies(request, url, key, config, user=user)
            else:
                headers = {
                    **({'Authorization': f'Bearer {key}'} if key else {}),
                }
                cookies = None

                if ENABLE_FORWARD_USER_INFO_HEADERS and user:
                    headers = include_user_info_headers(headers, user)

            async with session.get(
                url,
                headers=headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f'Connection error: {e}')
        return None


async def get_models_request(
    request: Request = None,
    url=None,
    key=None,
    user: UserModel = None,
    config=None,
):
    if is_anthropic_url(url):
        return await get_anthropic_models(url, key, user=user)
    return await send_get_request(request, f'{url}/models', key, user=user, config=config)


def openai_reasoning_model_handler(payload):
    """
    Handle reasoning model specific parameters
    """
    if 'max_tokens' in payload:
        # Convert "max_tokens" to "max_completion_tokens" for all reasoning models
        payload['max_completion_tokens'] = payload['max_tokens']
        del payload['max_tokens']

    # Handle system role conversion based on model type
    if payload['messages'][0]['role'] == 'system':
        model_lower = payload['model'].lower()
        # Legacy models use "user" role instead of "system"
        if model_lower.startswith('o1-mini') or model_lower.startswith('o1-preview'):
            payload['messages'][0]['role'] = 'user'
        else:
            payload['messages'][0]['role'] = 'developer'

    return payload


async def get_headers_and_cookies(
    request: Request,
    url,
    key=None,
    config=None,
    metadata: Optional[dict] = None,
    user: UserModel = None,
):
    cookies = {}
    headers = {
        'Content-Type': 'application/json',
        **(
            {
                'HTTP-Referer': 'https://openwebui.com/',
                'X-Title': 'Open WebUI',
            }
            if 'openrouter.ai' in url
            else {}
        ),
    }

    if ENABLE_FORWARD_USER_INFO_HEADERS and user:
        headers = include_user_info_headers(headers, user)
        if metadata and metadata.get('chat_id'):
            headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = metadata.get('chat_id')

    token = None
    auth_type = config.get('auth_type')

    if auth_type == 'bearer' or auth_type is None:
        # Default to bearer if not specified
        token = f'{key}'
    elif auth_type == 'none':
        token = None
    elif auth_type == 'session':
        cookies = request.cookies
        token = request.state.token.credentials
    elif auth_type == 'system_oauth':
        cookies = request.cookies

        oauth_token = None
        try:
            if request.cookies.get('oauth_session_id', None):
                oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                    user.id,
                    request.cookies.get('oauth_session_id', None),
                )
        except Exception as e:
            log.error(f'Error getting OAuth token: {e}')

        if oauth_token:
            token = f'{oauth_token.get("access_token", "")}'

    elif auth_type in ('azure_ad', 'microsoft_entra_id'):
        token = get_microsoft_entra_id_access_token()

    if token:
        headers['Authorization'] = f'Bearer {token}'

    if config.get('headers') and isinstance(config.get('headers'), dict):
        headers = {**headers, **config.get('headers')}

    return headers, cookies


def get_microsoft_entra_id_access_token():
    """
    Get Microsoft Entra ID access token using DefaultAzureCredential for Azure OpenAI.
    Returns the token string or None if authentication fails.
    """
    try:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), 'https://cognitiveservices.azure.com/.default'
        )
        return token_provider()
    except Exception as e:
        log.error(f'Error getting Microsoft Entra ID access token: {e}')
        return None


def resolve_openclaw_worker_api_config(api_config: Optional[dict] = None) -> tuple[str, str]:
    api_config = api_config or {}

    worker_api_base_url = str(api_config.get('worker_api_base_url') or OPENCLAW_WORKER_API_BASE_URL or '').strip()
    worker_api_token = str(api_config.get('worker_api_token') or OPENCLAW_WORKER_API_TOKEN or '').strip()

    return worker_api_base_url.rstrip('/'), worker_api_token


async def fetch_openclaw_worker_json(
    worker_api_base_url: str,
    worker_api_token: str,
    method: str,
    path: str,
    payload: Optional[dict] = None,
) -> dict:
    headers = {}
    if worker_api_token:
        headers['Authorization'] = f'Bearer {worker_api_token}'
    if payload is not None:
        headers['Content-Type'] = 'application/json'

    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.request(
            method=method,
            url=f'{worker_api_base_url}{path}',
            headers=headers,
            data=json.dumps(payload) if payload is not None else None,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            text = await response.text()
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = None

            if not response.ok:
                if isinstance(body, dict):
                    detail = body.get('detail') or body.get('error') or text or f'HTTP Error: {response.status}'
                else:
                    detail = text or f'HTTP Error: {response.status}'
                raise HTTPException(status_code=response.status, detail=detail)

            if not isinstance(body, dict):
                raise HTTPException(status_code=502, detail='Worker API returned an invalid response.')

            return body


def extract_text_from_responses_content(content: list | str | None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')
        if item_type in ('input_text', 'text', 'output_text'):
            parts.append(str(item.get('text', '')))
    return '\n'.join(part for part in parts if part).strip()


def extract_openclaw_worker_prompt(payload: dict) -> str:
    prompt_parts = []

    for message in payload.get('messages') or []:
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue

        content = message.get('content')
        if isinstance(content, str):
            text = content.strip()
        else:
            text = extract_text_from_responses_content(content)

        if text:
            prompt_parts.append(text)

    prompt = '\n\n'.join(part for part in prompt_parts if part).strip()
    if prompt:
        return prompt

    if isinstance(payload.get('input'), str):
        return str(payload['input']).strip()

    prompt_parts = []
    for item in payload.get('input') or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') != 'message':
            continue
        if item.get('role') != 'user':
            continue
        text = extract_text_from_responses_content(item.get('content'))
        if text:
            prompt_parts.append(text)

    prompt = '\n\n'.join(part for part in prompt_parts if part).strip()
    if prompt:
        return prompt

    return str(payload.get('instructions') or '').strip()


def extract_openclaw_worker_file_id(value: str) -> str:
    candidate = str(value or '').strip()
    if not candidate:
        return ''

    if OPENCLAW_WORKER_FILE_ID_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    path = parsed.path if (parsed.scheme or candidate.startswith('/')) else candidate
    match = OPENCLAW_WORKER_FILE_PATH_RE.search(path)
    if match:
        return str(match.group(1) or '').strip()

    return ''


def parse_openclaw_worker_file_tags(text: str) -> list[dict[str, str]]:
    source = str(text or '')
    if '<file' not in source:
        return []

    entries: list[dict[str, str]] = []
    for match in OPENCLAW_WORKER_FILE_TAG_RE.finditer(source):
        attrs_raw = str(match.group(1) or '')
        attrs = {m.group(1).lower(): m.group(2) for m in OPENCLAW_WORKER_FILE_TAG_ATTR_RE.finditer(attrs_raw)}
        url = str(attrs.get('url') or '').strip()
        if not url:
            continue
        entries.append(
            {
                'url': url,
                'type': str(attrs.get('content_type') or attrs.get('type') or '').strip(),
                'name': str(attrs.get('name') or '').strip(),
            }
        )
    return entries


def collect_openclaw_worker_attachment_candidates(payload: dict) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []

    def append_candidate(url: str, media_type: str = '', name: str = ''):
        normalized_url = str(url or '').strip()
        if not normalized_url:
            return
        candidates.append(
            {
                'url': normalized_url,
                'type': str(media_type or '').strip(),
                'name': str(name or '').strip(),
            }
        )

    def scan_content(content: list | str | None):
        if isinstance(content, str):
            for tagged_file in parse_openclaw_worker_file_tags(content):
                append_candidate(tagged_file.get('url', ''), tagged_file.get('type', ''), tagged_file.get('name', ''))
            return
        if not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get('type') or '').strip().lower()
            if part_type in {'input_image', 'image_url'}:
                image_url = part.get('image_url')
                if isinstance(image_url, dict):
                    append_candidate(
                        image_url.get('url', ''),
                        image_url.get('content_type') or image_url.get('type') or '',
                        image_url.get('name') or '',
                    )
                else:
                    append_candidate(str(image_url or ''), part.get('content_type') or part.get('mime_type') or '')
                continue
            if part_type in {'input_text', 'text', 'output_text'}:
                for tagged_file in parse_openclaw_worker_file_tags(part.get('text') or ''):
                    append_candidate(tagged_file.get('url', ''), tagged_file.get('type', ''), tagged_file.get('name', ''))

    for item in payload.get('input') or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('type') or '').strip() != 'message':
            continue
        if str(item.get('role') or '').strip() != 'user':
            continue
        scan_content(item.get('content'))

    for message in payload.get('messages') or []:
        if not isinstance(message, dict):
            continue
        if str(message.get('role') or '').strip() != 'user':
            continue
        scan_content(message.get('content'))
        for file_item in message.get('files') or []:
            if not isinstance(file_item, dict):
                continue
            file_payload = file_item.get('file') if isinstance(file_item.get('file'), dict) else {}
            append_candidate(
                file_item.get('url') or file_item.get('id') or file_payload.get('id') or '',
                file_item.get('content_type') or (file_payload.get('meta') or {}).get('content_type') or '',
                file_item.get('name') or file_payload.get('filename') or '',
            )

    return candidates


def guess_openclaw_worker_attachment_extension(media_type: str, fallback_name: str = '') -> str:
    normalized_media_type = str(media_type or '').strip().lower()
    base_media_type = normalized_media_type.split(';', 1)[0]
    guessed_extension = mimetypes.guess_extension(base_media_type) if base_media_type else None
    extension = str(guessed_extension or '').strip().lower()

    # Python may return `.jpe`; normalize to a widely supported suffix.
    if extension == '.jpe':
        extension = '.jpg'

    if not extension and fallback_name:
        extension = Path(fallback_name).suffix.lower()

    return extension or '.bin'


async def materialize_openclaw_worker_data_url(raw_url: str, fallback_name: str = '') -> Optional[dict]:
    match = OPENCLAW_WORKER_DATA_URL_RE.match(str(raw_url or '').strip())
    if not match:
        return None

    media_type = str(match.group(1) or '').strip().lower()
    encoded_payload = re.sub(r'\s+', '', str(match.group(2) or ''))
    if not encoded_payload:
        return None

    try:
        file_bytes = await asyncio.to_thread(base64.b64decode, encoded_payload, validate=True)
    except Exception:
        log.warning('Failed to decode OpenClaw worker data URL attachment', exc_info=True)
        return None

    if not file_bytes:
        return None

    extension = guess_openclaw_worker_attachment_extension(media_type, fallback_name=fallback_name)
    digest = hashlib.sha256(file_bytes).hexdigest()
    cache_root = CACHE_DIR / 'openclaw' / 'worker_inputs'
    await asyncio.to_thread(cache_root.mkdir, parents=True, exist_ok=True)

    file_path = cache_root / f'{digest[:24]}{extension}'
    if not file_path.exists():
        await asyncio.to_thread(file_path.write_bytes, file_bytes)
    await asyncio.to_thread(prune_openclaw_worker_input_cache, cache_root)

    file_name = str(fallback_name or '').strip() or f'openclaw-worker-input{extension}'
    if not Path(file_name).suffix:
        file_name = f'{file_name}{extension}'

    return {
        'path': str(file_path),
        'type': media_type or None,
        'name': file_name,
    }


def prune_openclaw_worker_input_cache(
    cache_root: Path,
    *,
    max_files: int = OPENCLAW_WORKER_INPUT_CACHE_MAX_FILES,
    max_age_seconds: int = OPENCLAW_WORKER_INPUT_CACHE_MAX_AGE_SECONDS,
) -> None:
    if max_files < 1 or max_age_seconds < 1:
        return
    if not cache_root.is_dir():
        return

    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for path in cache_root.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue

        age_seconds = now - stat.st_mtime
        if age_seconds > max_age_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            continue

        candidates.append((stat.st_mtime, path))

    if len(candidates) <= max_files:
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, path in candidates[max_files:]:
        try:
            path.unlink()
        except OSError:
            pass


def openclaw_worker_endpoint_is_local(url: str) -> bool:
    hostname = urlparse(str(url or '').strip()).hostname or ''
    return hostname in OPENCLAW_WORKER_LOCAL_HOSTNAMES


async def resolve_openclaw_worker_attachments(
    payload: dict,
    *,
    user: Optional[UserModel] = None,
    db: Optional[AsyncSession] = None,
) -> list[dict]:
    candidates = collect_openclaw_worker_attachment_candidates(payload)
    if not candidates:
        return []

    file_cache: dict[str, object | None] = {}
    attachments = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    for candidate in candidates:
        raw_url = str(candidate.get('url') or '').strip()
        if not raw_url:
            continue
        output_url = raw_url
        file_id = extract_openclaw_worker_file_id(raw_url)
        file_record = None
        if file_id:
            if file_id not in file_cache:
                file_item = await Files.get_file_by_id(file_id, db=db)
                if file_item and user is not None and user.role != 'admin':
                    has_direct_access = file_item.user_id == user.id
                    has_shared_access = await has_access_to_file(file_id, 'read', user, db=db)
                    if not has_direct_access and not has_shared_access:
                        file_item = None
                file_cache[file_id] = file_item
            file_record = file_cache[file_id]
            if file_record is None:
                continue

        resolved_type = str(candidate.get('type') or '').strip()
        resolved_name = str(candidate.get('name') or '').strip()
        resolved_path = ''

        if file_record:
            resolved_path = str(getattr(file_record, 'path', '') or '').strip()
            if not resolved_name:
                resolved_name = str(getattr(file_record, 'filename', '') or '').strip()
            if not resolved_type:
                file_meta = getattr(file_record, 'meta', {}) or {}
                resolved_type = str(file_meta.get('content_type') or '').strip()

        if not resolved_name and resolved_path:
            resolved_name = Path(resolved_path).name

        if not resolved_type:
            lowered_hint = str(candidate.get('type') or '').strip().lower()
            if lowered_hint in {'image', 'input_image'}:
                resolved_type = 'image/*'

        if OPENCLAW_WORKER_DATA_URL_RE.match(raw_url):
            data_url_attachment = await materialize_openclaw_worker_data_url(raw_url, fallback_name=resolved_name)
            if data_url_attachment:
                resolved_path = resolved_path or str(data_url_attachment.get('path') or '').strip()
                if not resolved_type:
                    resolved_type = str(data_url_attachment.get('type') or '').strip()
                if not resolved_name:
                    resolved_name = str(data_url_attachment.get('name') or '').strip()
                output_url = None

        dedupe_key = (
            file_id or resolved_path or raw_url,
            resolved_path,
            resolved_type.lower(),
            resolved_name.lower(),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        attachments.append(
            {
                'id': file_id or None,
                'url': output_url,
                'name': resolved_name or None,
                'type': resolved_type or None,
                'path': resolved_path or None,
            }
        )

    return attachments


def openclaw_worker_attachment_is_visual(attachment: dict) -> bool:
    media_type = str(attachment.get('type') or '').strip().lower()
    if media_type.startswith('image/') or media_type in {'image', 'image/*'}:
        return True

    path_or_name = str(attachment.get('path') or attachment.get('name') or '').strip().lower()
    if path_or_name:
        suffix = Path(path_or_name).suffix.lower()
        if suffix in OPENCLAW_WORKER_IMAGE_EXTENSIONS:
            return True

    return False


def openclaw_worker_has_visual_attachments(attachments: list[dict]) -> bool:
    return any(openclaw_worker_attachment_is_visual(item) for item in attachments if isinstance(item, dict))


def looks_like_openclaw_worker_candidate(prompt: str) -> bool:
    normalized = (prompt or '').strip().lower()
    if not normalized:
        return False

    return bool(
        re.search(
            r'(multi[\s-]?agent|multiple agents|all agents|协作|多 agent|多角色|并行|分工|角色分配|角色协同)',
            normalized,
        )
    )


def looks_like_openclaw_visual_generation_candidate(prompt: str) -> bool:
    normalized = (prompt or '').strip().lower()
    if not normalized:
        return False

    if re.search(
        r'(dreamina|即梦|text[\s-]?to[\s-]?image|image[\s-]?to[\s-]?image|文生图|图生图|生图|改图|高清化|upscale|image generation|show the image in this chat|submit_id)',
        normalized,
    ):
        return True

    generation_verbs = (
        r'(生成|做|制作|创建|画|绘制|设计|渲染|来一张|出一张|帮我做|帮我生成|帮我画|generate|make|create|draw|design|render)'
    )
    image_targets = (
        r'(图片|图像|海报|封面|插画|配图|壁纸|横幅|缩略图|效果图|渲染图|poster|banner|cover|illustration|artwork|thumbnail|wallpaper|concept art)'
    )

    return bool(re.search(fr'({generation_verbs}.*{image_targets}|{image_targets}.*{generation_verbs})', normalized))


def is_openclaw_worker_internal_metadata_prompt(prompt: str) -> bool:
    normalized = (prompt or '').strip().lower()
    if not normalized:
        return False

    if '### chat history:' not in normalized:
        return False

    return any(
        marker in normalized
        for marker in (
            'generate a concise, 3-5 word title with an emoji summarizing the chat history',
            'generate 1-3 broad tags categorizing the main themes of the chat history',
            'suggest 3-5 relevant follow-up questions or prompts',
            'generate a detailed prompt for am image generation task',
        )
    )


def summarize_openclaw_worker_text(text: str, limit: int = 140) -> str:
    normalized = re.sub(r'\s+', ' ', str(text or '').strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + '…'


def openclaw_worker_result_looks_waiting(text: str | None) -> bool:
    normalized = str(text or '').strip()
    return bool(normalized and OPENCLAW_WORKER_WAITING_RESULT_RE.search(normalized))


def openclaw_worker_result_looks_thin(text: str | None) -> bool:
    normalized = str(text or '').strip()
    if not normalized:
        return False
    if normalized == '(no output)':
        return True
    return bool(OPENCLAW_WORKER_THIN_RESULT_RE.search(normalized))


def openclaw_worker_subagent_status_outcome(status_text: str | None) -> str:
    normalized = str(status_text or '').strip()
    if not normalized:
        return ''
    if OPENCLAW_WORKER_CANCELLED_STATUS_RE.search(normalized):
        return 'cancelled'
    if OPENCLAW_WORKER_TIMED_OUT_STATUS_RE.search(normalized):
        return 'timed_out'
    if OPENCLAW_WORKER_FAILED_STATUS_RE.search(normalized):
        return 'failed'
    lowered = normalized.lower()
    if any(keyword in lowered for keyword in ('complete', 'done', 'success', 'succeed')):
        return 'completed'
    return ''


def openclaw_worker_child_result_looks_intermediate(text: str | None) -> bool:
    normalized = summarize_openclaw_worker_text(str(text or '').strip(), limit=220)
    if not normalized:
        return False
    if len(normalized) > 220:
        return False
    return bool(OPENCLAW_WORKER_INTERMEDIATE_CHILD_RESULT_RE.search(normalized))


def normalize_openclaw_worker_child_result_preview(text: str | None) -> str:
    normalized = summarize_openclaw_worker_text(str(text or '').strip(), limit=220)
    if not normalized:
        return ''
    if openclaw_worker_result_looks_thin(normalized):
        return ''
    if OPENCLAW_WORKER_CONTROL_TOKEN_RE.search(normalized):
        return ''
    if openclaw_worker_result_looks_waiting(normalized):
        return ''
    if openclaw_worker_child_result_looks_intermediate(normalized):
        return ''
    return normalized


def read_openclaw_worker_transcript_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''


def openclaw_worker_transcript_has_subagent_activity(text: str) -> bool:
    return 'sessions_spawn' in text or '[Internal task completion event]' in text


def openclaw_worker_transcript_mentions_job_id(text: str, job_id: str) -> bool:
    normalized_job_id = str(job_id or '').strip()
    return bool(normalized_job_id) and normalized_job_id in text


def openclaw_worker_transcript_is_auxiliary_metadata(text: str) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in OPENCLAW_WORKER_AUXILIARY_METADATA_TRANSCRIPT_PATTERNS)


def resolve_openclaw_agent_session_entry(root: Path, agent_id: str, session_key: str) -> tuple[dict, Path | None]:
    store_path = root / 'config' / 'agents' / agent_id / 'sessions' / 'sessions.json'
    if not store_path.is_file():
        return {}, None

    try:
        store = json.loads(store_path.read_text(encoding='utf-8'))
    except Exception:
        return {}, None

    if not isinstance(store, dict):
        return {}, None

    entry = store.get(session_key)
    if not isinstance(entry, dict):
        return {}, None

    session_file = str(entry.get('sessionFile') or '').strip()
    if session_file:
        transcript_path = Path(session_file)
        if transcript_path.is_file():
            return entry, transcript_path

    session_id = str(entry.get('sessionId') or '').strip()
    if session_id:
        transcript_path = root / 'config' / 'agents' / agent_id / 'sessions' / f'{session_id}.jsonl'
        if transcript_path.is_file():
            return entry, transcript_path

    return entry, None


def extract_openclaw_session_error_message(transcript_path: Path | None) -> str:
    if transcript_path is None or not transcript_path.is_file():
        return ''

    for raw_line in reversed(transcript_path.read_text(encoding='utf-8', errors='replace').splitlines()):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except Exception:
            continue
        if record.get('type') != 'message':
            continue
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        error_message = str(message.get('errorMessage') or '').strip()
        if error_message:
            return error_message
    return ''


def extract_openclaw_session_latest_assistant_text(transcript_path: Path | None) -> str:
    if transcript_path is None or not transcript_path.is_file():
        return ''

    for raw_line in reversed(transcript_path.read_text(encoding='utf-8', errors='replace').splitlines()):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except Exception:
            continue
        if record.get('type') != 'message':
            continue
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        if message.get('role') != 'assistant':
            continue
        contents = message.get('content') if isinstance(message.get('content'), list) else []
        text_parts = [
            str(item.get('text') or '').strip()
            for item in contents
            if isinstance(item, dict) and item.get('type') == 'text' and str(item.get('text') or '').strip()
        ]
        if text_parts:
            return '\n\n'.join(text_parts)
    return ''


def inspect_openclaw_child_session(job: dict, session_key: str) -> dict | None:
    root = infer_openclaw_root_from_worker_job(job)
    session_key = str(session_key or '').strip()
    match = re.match(r'agent:([^:]+):subagent:', session_key)
    agent_id = match.group(1).strip() if match else ''
    if root is None or not agent_id:
        return None

    entry, transcript_path = resolve_openclaw_agent_session_entry(root, agent_id, session_key)
    if not entry and transcript_path is None:
        return None

    raw_status = str(entry.get('status') or '').strip()
    error_message = extract_openclaw_session_error_message(transcript_path)
    preview = normalize_openclaw_worker_child_result_preview(
        extract_openclaw_session_latest_assistant_text(transcript_path)
    )

    outcome = openclaw_worker_subagent_status_outcome(raw_status)
    if error_message and outcome not in {'failed', 'timed_out', 'cancelled'}:
        outcome = 'failed'

    terminal = bool(error_message) or outcome in {'completed', 'failed', 'timed_out', 'cancelled'}
    if not terminal:
        return {
            'terminal': False,
            'resultPreview': preview,
        }

    if outcome == 'timed_out':
        detail = error_message or raw_status
        status_text = f'timed out: {detail}' if detail else 'timed out'
    elif outcome == 'cancelled':
        detail = error_message or raw_status
        status_text = f'cancelled: {detail}' if detail else 'cancelled'
    elif outcome == 'failed':
        detail = error_message or raw_status or 'subagent execution failed'
        status_text = f'failed: {detail}'
        preview = ''
    else:
        status_text = raw_status or 'completed'

    return {
        'terminal': True,
        'status': summarize_openclaw_worker_text(status_text, limit=120),
        'resultPreview': preview,
    }


def enrich_openclaw_worker_subagent_progress(job: dict, progress: Optional[dict]) -> Optional[dict]:
    if not isinstance(progress, dict):
        return progress

    items = progress.get('items')
    if not isinstance(items, list):
        return progress

    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        raw_item['resultPreview'] = normalize_openclaw_worker_child_result_preview(raw_item.get('resultPreview'))

        session_key = str(raw_item.get('sessionKey') or '').strip()
        child_session = inspect_openclaw_child_session(job, session_key) if session_key else None
        if not isinstance(child_session, dict):
            continue

        if child_session.get('terminal'):
            raw_item['state'] = 'completed'
            if child_session.get('status'):
                raw_item['status'] = child_session.get('status')
            raw_item['resultPreview'] = str(child_session.get('resultPreview') or '').strip()
            continue

        if not raw_item.get('resultPreview') and child_session.get('resultPreview'):
            raw_item['resultPreview'] = str(child_session.get('resultPreview') or '').strip()

    progress['completedCount'] = sum(
        1 for item in items if isinstance(item, dict) and str(item.get('state') or '').strip() == 'completed'
    )
    progress['activeCount'] = sum(
        1 for item in items if isinstance(item, dict) and str(item.get('state') or '').strip() != 'completed'
    )
    progress['startedCount'] = max(int(progress.get('startedCount') or 0), len(items))
    return progress


def merge_openclaw_worker_subagent_progress(*payloads: Optional[dict]) -> Optional[dict]:
    items_by_session: dict[str, dict[str, str]] = {}
    reported_started_counts: list[int] = []

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        started_count = payload.get('startedCount')
        if isinstance(started_count, int):
            reported_started_counts.append(started_count)
        items = payload.get('items')
        if not isinstance(items, list):
            continue

        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            session_key = str(raw_item.get('sessionKey') or '').strip()
            if not session_key:
                continue

            item = {
                'sessionKey': session_key,
                'agentId': str(raw_item.get('agentId') or '').strip(),
                'task': str(raw_item.get('task') or '').strip(),
                'state': str(raw_item.get('state') or '').strip(),
                'status': str(raw_item.get('status') or '').strip(),
                'resultPreview': str(raw_item.get('resultPreview') or '').strip(),
            }
            current = items_by_session.get(session_key)
            if current is None:
                items_by_session[session_key] = item
                continue

            merged = dict(current)
            if item['state'] == 'completed' or not merged.get('state'):
                merged['state'] = item['state'] or merged.get('state', '')
            for field in ('agentId', 'task', 'status', 'resultPreview'):
                candidate = item.get(field, '')
                existing = merged.get(field, '')
                if candidate and (not existing or len(candidate) > len(existing)):
                    merged[field] = candidate
            items_by_session[session_key] = merged

    if not items_by_session and not reported_started_counts:
        return None

    items = list(items_by_session.values())
    items.sort(key=lambda item: item.get('sessionKey') or '')
    completed_count = sum(1 for item in items if item.get('state') == 'completed')
    active_count = sum(1 for item in items if item.get('state') != 'completed')
    started_count = max([len(items), *reported_started_counts]) if items or reported_started_counts else 0
    return {
        'startedCount': started_count,
        'completedCount': completed_count,
        'activeCount': active_count,
        'items': items,
    }


def build_openclaw_worker_fallback_result(progress: Optional[dict], note: str = '') -> str:
    if not isinstance(progress, dict):
        return ''

    items = progress.get('items')
    if not isinstance(items, list):
        return ''

    ordered_agent_ids = ['visual', 'heavy', 'coder', 'release', 'ops', 'main']
    normalized_items = [
        item
        for item in items
        if isinstance(item, dict) and str(item.get('agentId') or '').strip()
    ]
    if not normalized_items:
        return ''

    agent_ids = {str(item.get('agentId') or '').strip() for item in normalized_items}
    unordered_agent_ids = sorted(agent_id for agent_id in agent_ids if agent_id not in ordered_agent_ids)
    display_agent_ids = [agent_id for agent_id in ordered_agent_ids if agent_id in agent_ids] + unordered_agent_ids

    lines = ['多角色协作已收口。']
    if note.strip():
        lines.append(note.strip())

    for display_agent_id in display_agent_ids:
        matching_items = [
            item for item in normalized_items if str(item.get('agentId') or '').strip() == display_agent_id
        ]
        if not matching_items:
            continue
        item = matching_items[-1]
        result_preview = summarize_openclaw_worker_text(str(item.get('resultPreview') or '').strip(), limit=220)
        task_text = summarize_openclaw_worker_text(str(item.get('task') or '').strip(), limit=80)
        status_text = str(item.get('status') or '').strip()
        state = str(item.get('state') or '').strip()
        outcome = openclaw_worker_subagent_status_outcome(status_text)
        meaningful_preview = normalize_openclaw_worker_child_result_preview(result_preview)

        if outcome == 'failed':
            summary = meaningful_preview or (
                f'未成功返回可展示内容。状态：{status_text}' if status_text else '未成功返回可展示内容。'
            )
        elif outcome == 'timed_out':
            summary = meaningful_preview or (f'处理超时。状态：{status_text}' if status_text else '处理超时。')
        elif outcome == 'cancelled':
            summary = meaningful_preview or (f'任务已取消。状态：{status_text}' if status_text else '任务已取消。')
        elif state == 'completed':
            summary = meaningful_preview or '已完成，但没有返回可展示内容。'
        else:
            summary = meaningful_preview or status_text or '仍在进行中。'

        line = f'- {display_agent_id}：{summary}'
        if task_text and task_text not in summary:
            line += f' 任务：{task_text}'
        lines.append(line)

    return '\n'.join(lines).strip()


def normalize_openclaw_worker_job_payload(payload: dict) -> dict:
    transcript_progress = build_openclaw_worker_subagent_progress(payload)
    payload['subagent_progress'] = merge_openclaw_worker_subagent_progress(
        payload.get('subagent_progress') if isinstance(payload.get('subagent_progress'), dict) else None,
        transcript_progress,
    )
    payload['subagent_progress'] = enrich_openclaw_worker_subagent_progress(payload, payload.get('subagent_progress'))

    progress = payload.get('subagent_progress') if isinstance(payload.get('subagent_progress'), dict) else None
    active_count = int(progress.get('activeCount') or 0) if isinstance(progress, dict) else 0
    completed_count = int(progress.get('completedCount') or 0) if isinstance(progress, dict) else 0
    final_text = str(payload.get('final_visible_text') or '').strip()
    phase = str(payload.get('phase') or '').strip().lower()
    status = str(payload.get('status') or '').strip().lower()
    is_terminal = phase in {'completed', 'failed', 'timed_out', 'cancelled'} or status in {
        'succeeded',
        'failed',
        'timed_out',
        'cancelled',
    }
    looks_waiting = openclaw_worker_result_looks_waiting(final_text)
    looks_thin = openclaw_worker_result_looks_thin(final_text)

    if active_count > 0 and looks_waiting:
        payload['phase'] = 'running'
        payload['status'] = 'running'
        payload['final_visible_text'] = ''
        return payload

    if is_terminal and completed_count > 0 and (not final_text or looks_waiting or looks_thin):
        note = (
            '主会话给出的最终结果过于简略，以下内容根据子任务结果整理。'
            if looks_thin
            else '主会话没有留下可直接展示的最终结果，以下内容根据已完成角色结果整理。'
        )
        payload['final_visible_text'] = build_openclaw_worker_fallback_result(
            progress,
            note=note,
        )

    payload['resolved_artifacts'] = build_openclaw_worker_resolved_artifacts(payload)
    return payload


def infer_openclaw_root_from_worker_job(job: dict) -> Optional[Path]:
    candidate_fields = (
        'report_json',
        'report_markdown',
        'prompt_file',
        'result_file',
        'log_file',
    )
    for field in candidate_fields:
        raw_path = str(job.get(field) or '').strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        parents = [path] if path.is_dir() else []
        parents.extend(path.parents)
        for parent in parents:
            if (parent / 'config' / 'openclaw.json').is_file():
                return parent
    return None


def infer_openclaw_root_from_path(path: Path) -> Optional[Path]:
    candidate = path.expanduser()
    parents = [candidate] if candidate.is_dir() else []
    parents.extend(candidate.parents)
    for parent in parents:
        if (parent / 'config' / 'openclaw.json').is_file():
            return parent
    return None


def resolve_openclaw_worker_artifact_file_path(path: str) -> Optional[Path]:
    raw_path = str(path or '').strip()
    if not raw_path:
        return None

    artifact_path = Path(raw_path).expanduser()
    if not artifact_path.is_absolute():
        return None

    resolved_path = artifact_path.resolve(strict=False)
    if not resolved_path.is_file():
        return None

    root = infer_openclaw_root_from_path(resolved_path)
    if root is None:
        return None

    if not openclaw_worker_path_is_within_root(resolved_path, root / 'work'):
        return None

    return resolved_path


def guess_openclaw_worker_artifact_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == '.md':
        return 'text/markdown'
    if suffix == '.csv':
        return 'text/csv'
    if suffix == '.tsv':
        return 'text/tab-separated-values'
    if suffix == '.json':
        return 'application/json'
    if suffix == '.svg':
        return 'image/svg+xml'

    media_type, _ = mimetypes.guess_type(str(path))
    return media_type or 'application/octet-stream'


def openclaw_worker_artifact_media_type_should_inline(media_type: str) -> bool:
    normalized = str(media_type or '').split(';', 1)[0].strip().lower()
    return normalized.startswith(('text/', 'image/', 'audio/', 'video/')) or normalized in (
        OPENCLAW_WORKER_ARTIFACT_INLINE_MEDIA_TYPES
    )


def build_openclaw_worker_artifact_response_headers(
    path: Path,
    *,
    download: bool = False,
) -> tuple[str, dict[str, str]]:
    media_type = guess_openclaw_worker_artifact_media_type(path)
    encoded_filename = quote(path.name)
    disposition = (
        'attachment'
        if download or not openclaw_worker_artifact_media_type_should_inline(media_type)
        else 'inline'
    )
    return media_type, {
        'Content-Disposition': f"{disposition}; filename*=UTF-8''{encoded_filename}"
    }


def resolve_openclaw_worker_transcript_path(job: dict) -> Optional[Path]:
    root = infer_openclaw_root_from_worker_job(job)
    agent_id = str(job.get('agent_id') or '').strip()
    session_key = str(job.get('worker_session_key') or '').strip()
    job_id = str(job.get('id') or '').strip()
    if root is None or not agent_id or not session_key:
        return None

    store_path = root / 'config' / 'agents' / agent_id / 'sessions' / 'sessions.json'
    if not store_path.is_file():
        return None

    try:
        store = json.loads(store_path.read_text(encoding='utf-8'))
    except Exception:
        return None

    def resolve_entry_path(entry: dict) -> Optional[Path]:
        session_file = str(entry.get('sessionFile') or '').strip()
        if session_file:
            path = Path(session_file)
            if path.is_file():
                return path

        session_id = str(entry.get('sessionId') or '').strip()
        if not session_id:
            return None

        transcript_path = root / 'config' / 'agents' / agent_id / 'sessions' / f'{session_id}.jsonl'
        return transcript_path if transcript_path.is_file() else None

    def parse_job_timestamp(value: object) -> Optional[float]:
        raw_value = str(value or '').strip()
        if not raw_value:
            return None
        if raw_value.endswith('Z'):
            raw_value = f'{raw_value[:-1]}+00:00'
        try:
            return datetime.fromisoformat(raw_value).timestamp()
        except Exception:
            return None

    entry = store.get(session_key) if isinstance(store, dict) else None
    primary_path = resolve_entry_path(entry) if isinstance(entry, dict) else None
    primary_text = read_openclaw_worker_transcript_text(primary_path) if primary_path else ''
    if primary_path and openclaw_worker_transcript_has_subagent_activity(primary_text):
        return primary_path

    if not isinstance(store, dict):
        return primary_path

    job_timestamp = (
        parse_job_timestamp(job.get('started_at'))
        or parse_job_timestamp(job.get('created_at'))
        or parse_job_timestamp(job.get('updated_at'))
    )
    prefix = f'agent:{agent_id}:openresponses:'
    candidates: list[tuple[int, float, float, Path]] = []

    for candidate_key, candidate_entry in store.items():
        if not isinstance(candidate_key, str) or not candidate_key.startswith(prefix):
            continue
        if not isinstance(candidate_entry, dict):
            continue

        transcript_path = resolve_entry_path(candidate_entry)
        if transcript_path is None:
            continue
        transcript_text = read_openclaw_worker_transcript_text(transcript_path)
        if not openclaw_worker_transcript_has_subagent_activity(transcript_text):
            continue
        if openclaw_worker_transcript_is_auxiliary_metadata(transcript_text):
            continue
        if not openclaw_worker_transcript_mentions_job_id(transcript_text, job_id):
            continue

        started_at_ms = candidate_entry.get('startedAt')
        updated_at_ms = candidate_entry.get('updatedAt')
        try:
            started_at = float(started_at_ms) / 1000 if started_at_ms is not None else None
        except Exception:
            started_at = None
        try:
            updated_at = float(updated_at_ms) / 1000 if updated_at_ms is not None else None
        except Exception:
            updated_at = None

        reference_ts = started_at or updated_at or 0.0
        if job_timestamp and reference_ts and reference_ts < job_timestamp - 300:
            continue

        candidates.append(
            (
                abs(reference_ts - job_timestamp) if job_timestamp and reference_ts else float('inf'),
                -(updated_at or 0.0),
                transcript_path,
            )
        )

    candidates.sort()
    if candidates:
        return candidates[0][2]

    return primary_path


def openclaw_worker_path_is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def openclaw_worker_dedupe_existing_paths(paths: list[Path]) -> list[Path]:
    unique_paths: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = raw_path.expanduser()
        if not path.is_file():
            continue
        normalized = str(path.resolve(strict=False))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_paths.append(path.resolve(strict=False))
    return unique_paths


def openclaw_worker_looks_like_artifact_reference(token: str) -> bool:
    candidate = str(token or '').strip()
    if not candidate or any(char in candidate for char in ('*', '?', '\n', '\r')):
        return False
    if candidate.startswith(('http://', 'https://', 'file://')):
        return False
    return Path(candidate).suffix.lower() in OPENCLAW_WORKER_ARTIFACT_FILE_EXTENSIONS


def infer_openclaw_worker_artifact_context_agent(final_text: str, token: str) -> Optional[str]:
    if not final_text or not token:
        return None

    for raw_line in final_text.splitlines():
        if token not in raw_line:
            continue
        match = OPENCLAW_WORKER_ARTIFACT_CONTEXT_AGENT_RE.search(raw_line)
        if match:
            return match.group(1).strip().lower()
    return None


def resolve_openclaw_worker_artifact_path(
    root: Path,
    token: str,
    *,
    preferred_agent: Optional[str] = None,
) -> Optional[Path]:
    if root is None:
        return None

    candidate = str(token or '').strip()
    if not openclaw_worker_looks_like_artifact_reference(candidate):
        return None

    work_root = root / 'work'
    agent_workspaces_root = work_root / 'agent-workspaces'
    reports_root = work_root / 'reports'
    preferred_workspace = agent_workspaces_root / preferred_agent if preferred_agent else None

    def score_path(path: Path) -> tuple[int, int]:
        score = 0
        if preferred_workspace and openclaw_worker_path_is_within_root(path, preferred_workspace):
            score += 60
        if openclaw_worker_path_is_within_root(path, agent_workspaces_root / 'release'):
            score += 30
        if openclaw_worker_path_is_within_root(path, reports_root):
            score += 20
        if openclaw_worker_path_is_within_root(path, agent_workspaces_root):
            score += 10
        return score, -len(path.parts)

    path_token = Path(candidate).expanduser()
    direct_candidates: list[Path] = []

    if path_token.is_absolute():
        if path_token.is_file() and openclaw_worker_path_is_within_root(path_token, root):
            return path_token.resolve(strict=False)
        return None

    relative_bases = [base for base in (preferred_workspace, agent_workspaces_root, reports_root, work_root, root) if base]
    if len(path_token.parts) > 1:
        for base in relative_bases:
            direct_candidates.append(base / path_token)
    else:
        for base in relative_bases:
            direct_candidates.append(base / candidate)

    resolved_direct = openclaw_worker_dedupe_existing_paths(direct_candidates)
    if len(resolved_direct) == 1:
        return resolved_direct[0]

    search_roots = [base for base in (preferred_workspace, agent_workspaces_root, reports_root) if base and base.is_dir()]
    search_matches: list[Path] = []
    name = path_token.name
    for search_root in search_roots:
        search_matches.extend(search_root.rglob(name))

    resolved_matches = openclaw_worker_dedupe_existing_paths(search_matches)
    all_candidates = openclaw_worker_dedupe_existing_paths(resolved_direct + resolved_matches)
    if not all_candidates:
        return None
    if len(all_candidates) == 1:
        return all_candidates[0]

    ranked: dict[tuple[int, int], list[Path]] = {}
    for path in all_candidates:
        ranked.setdefault(score_path(path), []).append(path)

    best_score = max(ranked)
    best_paths = ranked[best_score]
    if len(best_paths) == 1:
        return best_paths[0]
    return None


def build_openclaw_worker_resolved_artifacts(job: dict) -> list[dict[str, str]]:
    root = infer_openclaw_root_from_worker_job(job)
    final_text = str(job.get('final_visible_text') or '').strip()
    if root is None:
        return []

    artifacts: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    seen_paths: set[str] = set()

    if final_text:
        for match in OPENCLAW_WORKER_ARTIFACT_CODE_SPAN_RE.finditer(final_text):
            label = str(match.group(1) or '').strip()
            if not label or label in seen_labels:
                continue
            preferred_agent = infer_openclaw_worker_artifact_context_agent(final_text, label)
            resolved_path = resolve_openclaw_worker_artifact_path(
                root,
                label,
                preferred_agent=preferred_agent,
            )
            if resolved_path is None:
                continue
            resolved_path_str = str(resolved_path)
            artifacts.append({'label': label, 'path': resolved_path_str})
            seen_labels.add(label)
            seen_paths.add(resolved_path_str)

    media_candidates = job.get('media_urls')
    if not isinstance(media_candidates, list):
        media_candidates = job.get('mediaUrls')
    if isinstance(media_candidates, list):
        for candidate in media_candidates:
            resolved_path = resolve_openclaw_worker_artifact_file_path(str(candidate or '').strip())
            if resolved_path is None:
                continue
            resolved_path_str = str(resolved_path)
            if resolved_path_str in seen_paths:
                continue
            label = resolved_path.name
            if not label or label in seen_labels:
                label = resolved_path_str
            artifacts.append({'label': label, 'path': resolved_path_str})
            seen_labels.add(label)
            seen_paths.add(resolved_path_str)

    return artifacts


def build_openclaw_worker_subagent_progress(job: dict) -> Optional[dict]:
    transcript_path = resolve_openclaw_worker_transcript_path(job)
    if transcript_path is None or not transcript_path.is_file():
        return None

    pending_calls: dict[str, dict[str, str]] = {}
    started_events: list[dict[str, str]] = []
    completed_by_session: dict[str, dict[str, str]] = {}
    seen_started: set[str] = set()

    for raw_line in transcript_path.read_text(encoding='utf-8', errors='replace').splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except Exception:
            continue
        if record.get('type') != 'message':
            continue

        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        role = message.get('role')

        if role == 'assistant':
            contents = message.get('content') if isinstance(message.get('content'), list) else []
            for item in contents:
                if not isinstance(item, dict):
                    continue
                if item.get('type') != 'toolCall' or item.get('name') != 'sessions_spawn':
                    continue
                call_id = str(item.get('id') or '').strip()
                arguments = item.get('arguments') if isinstance(item.get('arguments'), dict) else {}
                pending_calls[call_id] = {
                    'agentId': str(arguments.get('agentId') or '').strip(),
                    'task': str(arguments.get('task') or '').strip(),
                }
            continue

        if role == 'toolResult' and message.get('toolName') == 'sessions_spawn':
            details = message.get('details') if isinstance(message.get('details'), dict) else {}
            status = str(details.get('status') or '').strip().lower()
            if status not in {'accepted', 'ok'}:
                continue
            session_key = str(details.get('childSessionKey') or '').strip()
            if not session_key or session_key in seen_started:
                continue
            call_id = str(message.get('toolCallId') or '').strip()
            call_meta = pending_calls.get(call_id) or {}
            agent_id = str(call_meta.get('agentId') or '').strip()
            if not agent_id:
                match = re.match(r'agent:([^:]+):subagent:', session_key)
                agent_id = match.group(1).strip() if match else ''
            if not agent_id or agent_id == 'main':
                continue
            seen_started.add(session_key)
            started_events.append(
                {
                    'sessionKey': session_key,
                    'agentId': agent_id,
                    'task': summarize_openclaw_worker_text(call_meta.get('task') or '', limit=120),
                }
            )
            continue

        if role != 'user':
            continue

        contents = message.get('content') if isinstance(message.get('content'), list) else []
        text_blocks = [item.get('text') for item in contents if isinstance(item, dict) and isinstance(item.get('text'), str)]
        combined_text = '\n'.join(text_blocks)
        if '[Internal task completion event]' not in combined_text or 'source: subagent' not in combined_text:
            continue

        session_key_match = re.search(r'session_key:\s*(.+)', combined_text)
        task_match = re.search(r'\ntask:\s*(.+)', combined_text)
        status_match = re.search(r'\nstatus:\s*(.+)', combined_text)
        result_match = re.search(
            r'<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>\n?(.*?)\n?<<<END_UNTRUSTED_CHILD_RESULT>>>',
            combined_text,
            flags=re.DOTALL,
        )
        session_key = session_key_match.group(1).strip() if session_key_match else ''
        if not session_key:
            continue
        match = re.match(r'agent:([^:]+):subagent:', session_key)
        agent_id = match.group(1).strip() if match else ''
        if not agent_id or agent_id == 'main':
            continue
        completed_by_session[session_key] = {
            'sessionKey': session_key,
            'agentId': agent_id,
            'task': summarize_openclaw_worker_text(task_match.group(1).strip() if task_match else '', limit=120),
            'status': summarize_openclaw_worker_text(status_match.group(1).strip() if status_match else '', limit=80),
            'resultPreview': summarize_openclaw_worker_text(result_match.group(1).strip() if result_match else '', limit=180),
        }

    if not started_events and not completed_by_session:
        return None

    items: list[dict[str, str]] = []
    started_keys: set[str] = set()
    for event in started_events:
        session_key = event.get('sessionKey') or ''
        completed = completed_by_session.get(session_key)
        items.append(
            {
                'sessionKey': session_key,
                'agentId': event.get('agentId') or '',
                'task': completed.get('task') if completed and completed.get('task') else event.get('task') or '',
                'state': 'completed' if completed else 'running',
                'status': completed.get('status') if completed else '',
                'resultPreview': completed.get('resultPreview') if completed else '',
            }
        )
        started_keys.add(session_key)

    for session_key, completed in completed_by_session.items():
        if session_key in started_keys:
            continue
        items.append(
            {
                'sessionKey': session_key,
                'agentId': completed.get('agentId') or '',
                'task': completed.get('task') or '',
                'state': 'completed',
                'status': completed.get('status') or '',
                'resultPreview': completed.get('resultPreview') or '',
            }
        )

    active_count = sum(1 for item in items if item.get('state') != 'completed')
    return {
        'startedCount': len(started_events),
        'completedCount': sum(1 for item in items if item.get('state') == 'completed'),
        'activeCount': active_count,
        'items': items,
    }


def should_dispatch_openclaw_worker(prompt: str, estimate: Optional[dict], has_visual_attachments: bool = False) -> bool:
    normalized_prompt = prompt.strip()
    if not normalized_prompt and not has_visual_attachments:
        return False

    if normalized_prompt and is_openclaw_worker_internal_metadata_prompt(normalized_prompt):
        return False

    if has_visual_attachments:
        return True

    if not isinstance(estimate, dict):
        return looks_like_openclaw_worker_candidate(normalized_prompt)

    if (
        estimate.get('requiresOrchestration')
        or estimate.get('isMultiAgent')
        or estimate.get('isLongTask')
        or estimate.get('needsFreshVerification')
    ):
        return True

    if estimate.get('recommendedJobType') == 'visual_batch' and looks_like_openclaw_visual_generation_candidate(normalized_prompt):
        return True

    return looks_like_openclaw_worker_candidate(normalized_prompt)


def render_openclaw_worker_ack(job: dict, source_channel: str = 'openresponses') -> str:
    estimate = job.get('estimate') or {}
    first_batch = [item for item in (estimate.get('preferredInitialBatch') or []) if isinstance(item, str) and item]
    first_batch_text = f"我会先安排 {'、'.join(first_batch)} 开始。" if first_batch else '我会先拆分任务，再安排第一步。'

    return '\n'.join(
        [
            f"<!-- OpenClaw Worker | job id: `{job.get('id') or '-'}` -->",
            '',
            '已接到你的请求，正在按协作方式处理。',
            first_batch_text,
            '当前安排、子任务进度和最终结果会继续显示在这条消息里。',
        ]
    )


def build_openclaw_worker_response(model: str, ack_text: str) -> dict:
    return {
        'id': f'resp_worker_{uuid4().hex}',
        'object': 'response',
        'created_at': int(time.time()),
        'status': 'completed',
        'model': model,
        'output': [
            {
                'id': f'msg_worker_{uuid4().hex}',
                'type': 'message',
                'role': 'assistant',
                'status': 'completed',
                'content': [{'type': 'output_text', 'text': ack_text}],
            }
        ],
        'usage': {
            'input_tokens': 0,
            'output_tokens': 0,
            'total_tokens': 0,
        },
    }


async def maybe_dispatch_openclaw_worker(
    *,
    model: str,
    payload: dict,
    url: str,
    api_config: Optional[dict],
    source_channel: str,
    user: Optional[UserModel] = None,
    db: Optional[AsyncSession] = None,
) -> Optional[dict]:
    if not model.startswith('openclaw/'):
        return None

    if not openclaw_worker_endpoint_is_local(url):
        return None

    worker_api_base_url, worker_api_token = resolve_openclaw_worker_api_config(api_config)
    if not worker_api_base_url:
        return None

    attachments = await resolve_openclaw_worker_attachments(payload, user=user, db=db)
    has_visual_attachments = openclaw_worker_has_visual_attachments(attachments)
    has_local_path_attachments = any(str(item.get('path') or '').strip() for item in attachments if isinstance(item, dict))
    if has_local_path_attachments and not openclaw_worker_endpoint_is_local(worker_api_base_url):
        log.warning('Skipping OpenClaw worker dispatch for path-backed attachments because worker endpoint is not local')
        return None

    prompt = extract_openclaw_worker_prompt(payload)
    if not prompt and has_visual_attachments:
        prompt = '请基于用户上传图片完成图像处理请求，并直接返回最终图片。'
    if not prompt:
        return None
    prompt_looks_multi_agent = looks_like_openclaw_worker_candidate(prompt)

    metadata = {
        'source': {
            'channel': source_channel,
        },
        'allowBackgroundWorkerSubagents': True,
    }
    if attachments:
        metadata['attachments'] = [
            {
                'name': item.get('name'),
                'type': item.get('type'),
                'path': item.get('path'),
                'url': item.get('url'),
                'id': item.get('id'),
            }
            for item in attachments
        ]

    estimate = None
    try:
        estimate = await fetch_openclaw_worker_json(
            worker_api_base_url,
            worker_api_token,
            'POST',
            '/estimate',
            {
                'prompt': prompt,
                'metadata': metadata,
                'agent_id': 'main',
            },
        )
    except Exception:
        estimate = None

    if not should_dispatch_openclaw_worker(prompt, estimate, has_visual_attachments=has_visual_attachments):
        return None

    if prompt_looks_multi_agent or (estimate or {}).get('requiresOrchestration') or (estimate or {}).get('isMultiAgent'):
        agent_id = 'main'
        job_type = 'agent_task'
    else:
        default_agent = 'visual' if has_visual_attachments else 'ops'
        default_job_type = 'visual_batch' if has_visual_attachments else 'agent_task'
        estimate_agent = str((estimate or {}).get('selectedAgent') or '').strip().lower()
        estimate_job_type = str((estimate or {}).get('recommendedJobType') or '').strip()

        if has_visual_attachments and estimate_agent in {'', 'main'}:
            agent_id = default_agent
        else:
            agent_id = estimate_agent or default_agent

        if has_visual_attachments and estimate_job_type in {'', 'agent_task'}:
            job_type = default_job_type
        else:
            job_type = estimate_job_type or default_job_type

    job = await fetch_openclaw_worker_json(
        worker_api_base_url,
        worker_api_token,
        'POST',
        '/jobs',
        {
            'job_type': job_type,
            'prompt': prompt,
            'agent_id': agent_id,
            'metadata': metadata,
        },
    )
    ack = render_openclaw_worker_ack(job, source_channel=source_channel)

    return {
        'handled': True,
        'ack': ack,
        'response': build_openclaw_worker_response(model, ack),
        'job': job,
    }


async def resolve_openai_model_connection(
    request: Request,
    user: UserModel,
    model_id: str,
) -> tuple[int, dict, str, str, dict]:
    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS

    model = models.get(model_id)
    if not model:
        raise HTTPException(
            status_code=404,
            detail='Model not found',
        )

    idx = model['urlIdx']
    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(url, {}),
    )

    return idx, model, url, key, api_config


##########################################
#
# API routes
#
##########################################

router = APIRouter()


@router.get('/config')
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        'ENABLE_OPENAI_API': request.app.state.config.ENABLE_OPENAI_API,
        'OPENAI_API_BASE_URLS': request.app.state.config.OPENAI_API_BASE_URLS,
        'OPENAI_API_KEYS': request.app.state.config.OPENAI_API_KEYS,
        'OPENAI_API_CONFIGS': request.app.state.config.OPENAI_API_CONFIGS,
    }


class OpenAIConfigForm(BaseModel):
    ENABLE_OPENAI_API: Optional[bool] = None
    OPENAI_API_BASE_URLS: list[str]
    OPENAI_API_KEYS: list[str]
    OPENAI_API_CONFIGS: dict


@router.post('/config/update')
async def update_config(request: Request, form_data: OpenAIConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.ENABLE_OPENAI_API = form_data.ENABLE_OPENAI_API
    request.app.state.config.OPENAI_API_BASE_URLS = form_data.OPENAI_API_BASE_URLS
    request.app.state.config.OPENAI_API_KEYS = form_data.OPENAI_API_KEYS

    # Check if API KEYS length is same than API URLS length
    if len(request.app.state.config.OPENAI_API_KEYS) != len(request.app.state.config.OPENAI_API_BASE_URLS):
        if len(request.app.state.config.OPENAI_API_KEYS) > len(request.app.state.config.OPENAI_API_BASE_URLS):
            request.app.state.config.OPENAI_API_KEYS = request.app.state.config.OPENAI_API_KEYS[
                : len(request.app.state.config.OPENAI_API_BASE_URLS)
            ]
        else:
            request.app.state.config.OPENAI_API_KEYS += [''] * (
                len(request.app.state.config.OPENAI_API_BASE_URLS) - len(request.app.state.config.OPENAI_API_KEYS)
            )

    request.app.state.config.OPENAI_API_CONFIGS = form_data.OPENAI_API_CONFIGS

    # Remove the API configs that are not in the API URLS
    keys = list(map(str, range(len(request.app.state.config.OPENAI_API_BASE_URLS))))
    request.app.state.config.OPENAI_API_CONFIGS = {
        key: value for key, value in request.app.state.config.OPENAI_API_CONFIGS.items() if key in keys
    }

    return {
        'ENABLE_OPENAI_API': request.app.state.config.ENABLE_OPENAI_API,
        'OPENAI_API_BASE_URLS': request.app.state.config.OPENAI_API_BASE_URLS,
        'OPENAI_API_KEYS': request.app.state.config.OPENAI_API_KEYS,
        'OPENAI_API_CONFIGS': request.app.state.config.OPENAI_API_CONFIGS,
    }


@router.post('/audio/speech')
async def speech(request: Request, user=Depends(get_verified_user)):
    idx = None
    try:
        idx = request.app.state.config.OPENAI_API_BASE_URLS.index('https://api.openai.com/v1')

        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = CACHE_DIR / 'audio' / 'speech'
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f'{name}.mp3')
        file_body_path = SPEECH_CACHE_DIR.joinpath(f'{name}.json')

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
        key = request.app.state.config.OPENAI_API_KEYS[idx]
        api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
            str(idx),
            request.app.state.config.OPENAI_API_CONFIGS.get(url, {}),  # Legacy support
        )

        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        r = None
        try:
            session = await get_session()
            r = await session.post(
                url=f'{url}/audio/speech',
                data=body,
                headers=headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            )

            r.raise_for_status()

            # Save the streaming content to a file
            with open(file_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(8192):
                    f.write(chunk)

            with open(file_body_path, 'w') as f:
                json.dump(json.loads(body.decode('utf-8')), f)

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = await r.json()
                    if 'error' in res:
                        detail = f'External: {res["error"]}'
                except Exception:
                    detail = f'External: {e}'

            raise HTTPException(
                status_code=r.status if r else 500,
                detail=detail if detail else 'Open WebUI: Server Connection Error',
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.OPENAI_NOT_FOUND)


async def get_all_models_responses(request: Request, user: UserModel) -> list:
    if not request.app.state.config.ENABLE_OPENAI_API:
        return []

    # Cache config values locally to avoid repeated Redis lookups.
    # Each access to request.app.state.config.<KEY> triggers a Redis GET;
    # caching here avoids hundreds of redundant round-trips.
    api_base_urls = request.app.state.config.OPENAI_API_BASE_URLS
    api_keys = list(request.app.state.config.OPENAI_API_KEYS)
    api_configs = request.app.state.config.OPENAI_API_CONFIGS

    # Check if API KEYS length is same than API URLS length
    num_urls = len(api_base_urls)
    num_keys = len(api_keys)

    if num_keys != num_urls:
        # if there are more keys than urls, remove the extra keys
        if num_keys > num_urls:
            api_keys = api_keys[:num_urls]
            request.app.state.config.OPENAI_API_KEYS = api_keys
        # if there are more urls than keys, add empty keys
        else:
            api_keys += [''] * (num_urls - num_keys)
            request.app.state.config.OPENAI_API_KEYS = api_keys

    request_tasks = []
    for idx, url in enumerate(api_base_urls):
        if (str(idx) not in api_configs) and (url not in api_configs):  # Legacy support
            request_tasks.append(get_models_request(request, url, api_keys[idx], user=user))
        else:
            api_config = api_configs.get(
                str(idx),
                api_configs.get(url, {}),  # Legacy support
            )

            enable = api_config.get('enable', True)
            model_ids = api_config.get('model_ids', [])

            if enable:
                if len(model_ids) == 0:
                    request_tasks.append(get_models_request(request, url, api_keys[idx], user=user, config=api_config))
                else:
                    model_list = {
                        'object': 'list',
                        'data': [
                            {
                                'id': model_id,
                                'name': model_id,
                                'owned_by': 'openai',
                                'openai': {'id': model_id},
                                'urlIdx': idx,
                            }
                            for model_id in model_ids
                        ],
                    }

                    request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, model_list)))
            else:
                request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

    responses = await asyncio.gather(*request_tasks)

    for idx, response in enumerate(responses):
        if response:
            url = api_base_urls[idx]
            api_config = api_configs.get(
                str(idx),
                api_configs.get(url, {}),  # Legacy support
            )

            connection_type = api_config.get('connection_type', 'external')
            prefix_id = api_config.get('prefix_id', None)
            tags = api_config.get('tags', [])

            model_list = response if isinstance(response, list) else response.get('data', [])
            if not isinstance(model_list, list):
                # Catch non-list responses
                model_list = []

            for model in model_list:
                # Remove name key if its value is None #16689
                if 'name' in model and model['name'] is None:
                    del model['name']

                if prefix_id:
                    model['id'] = f'{prefix_id}.{model.get("id", model.get("name", ""))}'

                if tags:
                    model['tags'] = tags

                if connection_type:
                    model['connection_type'] = connection_type

    log.debug(f'get_all_models:responses() {responses}')
    return responses


async def get_filtered_models(models, user, db=None):
    # Filter models based on user access control
    model_ids = [model['id'] for model in models.get('data', [])]
    model_infos = {model_info.id: model_info for model_info in await Models.get_models_by_ids(model_ids, db=db)}
    user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user.id, db=db)}

    # Batch-fetch accessible resource IDs in a single query instead of N has_access calls
    accessible_model_ids = await AccessGrants.get_accessible_resource_ids(
        user_id=user.id,
        resource_type='model',
        resource_ids=list(model_infos.keys()),
        permission='read',
        user_group_ids=user_group_ids,
        db=db,
    )

    filtered_models = []
    for model in models.get('data', []):
        model_info = model_infos.get(model['id'])
        if model_info:
            if user.id == model_info.user_id or model_info.id in accessible_model_ids:
                filtered_models.append(model)
    return filtered_models


@cached(
    ttl=MODELS_CACHE_TTL,
    key=lambda _, user: f'openai_all_models_{user.id}' if user else 'openai_all_models',
)
async def get_all_models(request: Request, user: UserModel) -> dict[str, list]:
    log.info('get_all_models()')

    if not request.app.state.config.ENABLE_OPENAI_API:
        return {'data': []}

    # Cache config value locally to avoid repeated Redis lookups inside
    # the nested loop in get_merged_models (one GET per model otherwise).
    api_base_urls = request.app.state.config.OPENAI_API_BASE_URLS

    responses = await get_all_models_responses(request, user=user)

    def extract_data(response):
        if response and 'data' in response:
            return response['data']
        if isinstance(response, list):
            return response
        return None

    def is_supported_openai_models(model_id):
        if any(
            name in model_id
            for name in [
                'babbage',
                'dall-e',
                'davinci',
                'embedding',
                'tts',
                'whisper',
            ]
        ):
            return False
        return True

    def get_merged_models(model_lists):
        log.debug(f'merge_models_lists {model_lists}')
        models = {}

        for idx, model_list in enumerate(model_lists):
            if model_list is not None and 'error' not in model_list:
                for model in model_list:
                    model_id = model.get('id') or model.get('name')

                    base_url = api_base_urls[idx]
                    hostname = urlparse(base_url).hostname if base_url else None
                    if hostname == 'api.openai.com' and not is_supported_openai_models(model_id):
                        # Skip unwanted OpenAI models
                        continue

                    if model_id and model_id not in models:
                        models[model_id] = {
                            **model,
                            'name': model.get('name', model_id),
                            'owned_by': 'openai',
                            'openai': model,
                            'connection_type': model.get('connection_type', 'external'),
                            'urlIdx': idx,
                        }

        return models

    models = get_merged_models(map(extract_data, responses))
    log.debug(f'models: {models}')

    request.app.state.OPENAI_MODELS = models
    return {'data': list(models.values())}


@router.get('/models')
@router.get('/models/{url_idx}')
async def get_models(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if not request.app.state.config.ENABLE_OPENAI_API:
        raise HTTPException(status_code=503, detail='OpenAI API is disabled')

    models = {
        'data': [],
    }

    if url_idx is None:
        models = await get_all_models(request, user=user)
    else:
        url = request.app.state.config.OPENAI_API_BASE_URLS[url_idx]
        key = request.app.state.config.OPENAI_API_KEYS[url_idx]

        api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
            str(url_idx),
            request.app.state.config.OPENAI_API_CONFIGS.get(url, {}),  # Legacy support
        )

        r = None
        async with aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST),
        ) as session:
            try:
                headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

                if api_config.get('azure', False):
                    models = {
                        'data': api_config.get('model_ids', []) or [],
                        'object': 'list',
                    }
                elif is_anthropic_url(url):
                    models = await get_anthropic_models(url, key, user=user)
                    if models is None:
                        raise Exception('Failed to connect to Anthropic API')
                else:
                    async with session.get(
                        f'{url}/models',
                        headers=headers,
                        cookies=cookies,
                        ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    ) as r:
                        if r.status != 200:
                            error_detail = f'HTTP Error: {r.status}'
                            try:
                                res = await r.json()
                                if 'error' in res:
                                    error_detail = f'External Error: {res["error"]}'
                            except Exception:
                                pass
                            raise Exception(error_detail)

                        response_data = await r.json()

                        if 'api.openai.com' in url:
                            response_data['data'] = [
                                model
                                for model in response_data.get('data', [])
                                if not any(
                                    name in model['id']
                                    for name in [
                                        'babbage',
                                        'dall-e',
                                        'davinci',
                                        'embedding',
                                        'tts',
                                        'whisper',
                                    ]
                                )
                            ]

                        models = response_data
            except aiohttp.ClientError as e:
                # ClientError covers all aiohttp requests issues
                log.exception(f'Client error: {str(e)}')
                raise HTTPException(status_code=500, detail='Open WebUI: Server Connection Error')
            except Exception as e:
                log.exception(f'Unexpected error: {e}')
                error_detail = f'Unexpected error: {str(e)}'
                raise HTTPException(status_code=500, detail=error_detail)

    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        models['data'] = await get_filtered_models(models, user)

    return models


@router.get('/worker/jobs/{job_id}')
async def get_openclaw_worker_job(
    request: Request,
    job_id: str,
    model: str,
    user=Depends(get_verified_user),
):
    _, _, _, _, api_config = await resolve_openai_model_connection(request, user, model)

    worker_api_base_url, worker_api_token = resolve_openclaw_worker_api_config(api_config)
    if not worker_api_base_url:
        raise HTTPException(status_code=503, detail='Worker API is not configured.')

    headers = {}
    if worker_api_token:
        headers['Authorization'] = f'Bearer {worker_api_token}'

    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f'{worker_api_base_url}/jobs/{job_id}',
                headers=headers,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = None

                if not response.ok:
                    detail = (
                        payload.get('detail') if isinstance(payload, dict) else text or f'HTTP Error: {response.status}'
                    )
                    raise HTTPException(status_code=response.status, detail=detail)

                if not isinstance(payload, dict):
                    raise HTTPException(status_code=502, detail='Worker API returned an invalid response.')

                return normalize_openclaw_worker_job_payload(payload)
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f'Failed to fetch OpenClaw worker job {job_id}: {e}')
        raise HTTPException(status_code=502, detail='Failed to fetch worker status.')


@router.get('/worker/artifacts/content')
async def get_openclaw_worker_artifact_content(
    path: str,
    download: bool = False,
    user=Depends(get_verified_user),
):
    artifact_path = resolve_openclaw_worker_artifact_file_path(path)
    if artifact_path is None:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)

    media_type, headers = build_openclaw_worker_artifact_response_headers(
        artifact_path,
        download=download,
    )
    return FileResponse(artifact_path, media_type=media_type, headers=headers)


class OpenClawWorkerSubmitForm(BaseModel):
    model_config = ConfigDict(extra='allow')

    model: str
    payload: dict


@router.post('/worker/submit')
async def submit_openclaw_worker(
    request: Request,
    form_data: OpenClawWorkerSubmitForm,
    user=Depends(get_verified_user),
):
    _, _, url, _, api_config = await resolve_openai_model_connection(request, user, form_data.model)

    result = await maybe_dispatch_openclaw_worker(
        model=form_data.model,
        payload=form_data.payload,
        url=url,
        api_config=api_config,
        source_channel='openresponses',
        user=user,
    )
    if not result:
        return {'handled': False}
    return result


class ConnectionVerificationForm(BaseModel):
    url: str
    key: str

    config: Optional[dict] = None


@router.post('/verify')
async def verify_connection(
    request: Request,
    form_data: ConnectionVerificationForm,
    user=Depends(get_admin_user),
):
    url = form_data.url
    key = form_data.key

    api_config = form_data.config or {}

    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST),
    ) as session:
        try:
            headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

            if api_config.get('azure', False):
                # Only set api-key header if not using Azure Entra ID authentication
                auth_type = api_config.get('auth_type', 'bearer')
                if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                    headers['api-key'] = key

                api_version = api_config.get('api_version', '') or '2023-03-15-preview'
                async with session.get(
                    url=f'{url}/openai/models?api-version={api_version}',
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    try:
                        response_data = await r.json()
                    except Exception:
                        response_data = await r.text()

                    if r.status != 200:
                        if isinstance(response_data, (dict, list)):
                            return JSONResponse(status_code=r.status, content=response_data)
                        else:
                            return PlainTextResponse(status_code=r.status, content=response_data)

                    return response_data
            elif is_anthropic_url(url):
                result = await get_anthropic_models(url, key)
                if result is None:
                    raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)
                if 'error' in result:
                    raise HTTPException(status_code=500, detail=result['error'])
                return result
            else:
                async with session.get(
                    f'{url}/models',
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    try:
                        response_data = await r.json()
                    except Exception:
                        response_data = await r.text()

                    if r.status != 200:
                        if isinstance(response_data, (dict, list)):
                            return JSONResponse(status_code=r.status, content=response_data)
                        else:
                            return PlainTextResponse(status_code=r.status, content=response_data)

                    return response_data

        except aiohttp.ClientError as e:
            # ClientError covers all aiohttp requests issues
            log.exception(f'Client error: {str(e)}')
            raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)
        except Exception as e:
            log.exception(f'Unexpected error: {e}')
            raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)


def get_azure_allowed_params(api_version: str) -> set[str]:
    allowed_params = {
        'messages',
        'temperature',
        'role',
        'content',
        'contentPart',
        'contentPartImage',
        'enhancements',
        'dataSources',
        'n',
        'stream',
        'stop',
        'max_tokens',
        'presence_penalty',
        'frequency_penalty',
        'logit_bias',
        'user',
        'function_call',
        'functions',
        'tools',
        'tool_choice',
        'top_p',
        'log_probs',
        'top_logprobs',
        'response_format',
        'seed',
        'max_completion_tokens',
        'reasoning_effort',
    }

    try:
        if api_version >= '2024-09-01-preview':
            allowed_params.add('stream_options')
    except ValueError:
        log.debug(f'Invalid API version {api_version} for Azure OpenAI. Defaulting to allowed parameters.')

    return allowed_params


def is_openai_new_model(model: str) -> bool:
    model_lower = model.lower()
    # o-series models (o1, o3, o4, o5, ...)
    if re.match(r'^o\d+', model_lower):
        return True
    # gpt-N where N >= 5 (gpt-5, gpt-5.2, gpt-6, ...)
    m = re.match(r'^gpt-(\d+)', model_lower)
    if m and int(m.group(1)) >= 5:
        return True
    return False


def _sanitize_model_for_url(model: str) -> str:
    """Sanitize a model name before interpolating it into a URL path.

    Rejects path traversal attempts (../, /, \\) and percent-encodes
    the name so it is safe to use as a single URL path segment
    (e.g. Azure deployment name).
    """
    if not model or '..' in model or '/' in model or '\\' in model:
        raise HTTPException(
            status_code=400,
            detail='Invalid model name: must not be empty or contain path separators or traversal sequences',
        )
    return quote(model, safe='')


def convert_to_azure_payload(url, payload: dict, api_version: str):
    model = payload.get('model', '')

    # Filter allowed parameters based on Azure OpenAI API
    allowed_params = get_azure_allowed_params(api_version)

    # Special handling for o-series models
    if is_openai_new_model(model):
        # Convert max_tokens to max_completion_tokens for o-series models
        if 'max_tokens' in payload:
            payload['max_completion_tokens'] = payload['max_tokens']
            del payload['max_tokens']

        # Remove temperature if not 1 for o-series models
        if 'temperature' in payload and payload['temperature'] != 1:
            log.debug(
                f'Removing temperature parameter for o-series model {model} as only default value (1) is supported'
            )
            del payload['temperature']

    # Filter out unsupported parameters
    payload = {k: v for k, v in payload.items() if k in allowed_params}

    # Sanitize model name to prevent path traversal in the deployment URL
    model = _sanitize_model_for_url(model)

    url = f'{url}/openai/deployments/{model}'
    return url, payload


# Fields accepted by the Responses API for each input item type.
RESPONSES_ALLOWED_FIELDS: dict[str, set[str]] = {
    'message': {'type', 'role', 'content'},
    'function_call': {'type', 'call_id', 'name', 'arguments', 'id'},
    'function_call_output': {'type', 'call_id', 'output'},
}


def _normalize_stored_item(item: dict) -> dict:
    """Strip local-only fields from a stored output item before replaying it.

    Open WebUI stores extra bookkeeping fields (``id``, ``status``,
    ``started_at``, ``ended_at``, ``duration``, ``_tag_type``,
    ``attributes``, ``summary``, etc.) that the Responses API does
    not accept.  This helper returns a copy containing only the
    fields the API understands.
    """
    item_type = item.get('type', '')
    allowed = RESPONSES_ALLOWED_FIELDS.get(item_type)
    if allowed is None:
        # Unknown type — pass through as-is (e.g. reasoning, extension items).
        return item
    return {k: v for k, v in item.items() if k in allowed}


def convert_to_responses_payload(payload: dict) -> dict:
    """
    Convert Chat Completions payload to Responses API format.

    Chat Completions: { messages: [{role, content}], ... }
    Responses API: { input: [{type: "message", role, content: [...]}], instructions: "system" }
    """
    messages = payload.pop('messages', [])

    system_content = ''
    input_items = []

    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        # Check for stored output items (from previous Responses API turn)
        stored_output = msg.get('output')
        if stored_output and isinstance(stored_output, list):
            input_items.extend(_normalize_stored_item(item) for item in stored_output)
            continue

        if role == 'system':
            if isinstance(content, str):
                system_content = content
            elif isinstance(content, list):
                system_content = '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
            continue

        # Handle assistant messages with tool_calls (from convert_output_to_messages)
        if role == 'assistant' and msg.get('tool_calls'):
            # Add text content as message if present
            if content:
                text = (
                    content
                    if isinstance(content, str)
                    else '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
                )
                if text.strip():
                    input_items.append(
                        {
                            'type': 'message',
                            'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': text}],
                        }
                    )
            # Convert each tool_call to a function_call input item
            for tool_call in msg['tool_calls']:
                func = tool_call.get('function', {})
                input_items.append(
                    {
                        'type': 'function_call',
                        'call_id': tool_call.get('id', ''),
                        'name': func.get('name', ''),
                        'arguments': func.get('arguments', '{}'),
                    }
                )
            continue

        # Handle tool result messages
        if role == 'tool':
            input_items.append(
                {
                    'type': 'function_call_output',
                    'call_id': msg.get('tool_call_id', ''),
                    'output': msg.get('content', ''),
                }
            )
            continue

        # Convert content format
        text_type = 'output_text' if role == 'assistant' else 'input_text'

        if isinstance(content, str):
            content_parts = [{'type': text_type, 'text': content}]
        elif isinstance(content, list):
            content_parts = []
            for part in content:
                if part.get('type') == 'text':
                    content_parts.append({'type': text_type, 'text': part.get('text', '')})
                elif part.get('type') == 'image_url':
                    url_data = part.get('image_url', {})
                    url = url_data.get('url', '') if isinstance(url_data, dict) else url_data
                    content_parts.append({'type': 'input_image', 'image_url': url})
        else:
            content_parts = [{'type': text_type, 'text': str(content)}]

        input_items.append({'type': 'message', 'role': role, 'content': content_parts})

    responses_payload = {**payload, 'input': input_items}

    # Forward previous_response_id when the middleware has set it
    # (only used when ENABLE_RESPONSES_API_STATEFUL is enabled).
    previous_response_id = responses_payload.pop('previous_response_id', None)
    if previous_response_id:
        responses_payload['previous_response_id'] = previous_response_id

    if system_content:
        responses_payload['instructions'] = system_content

    if 'max_tokens' in responses_payload:
        responses_payload['max_output_tokens'] = responses_payload.pop('max_tokens')

    if 'max_completion_tokens' in responses_payload:
        responses_payload['max_output_tokens'] = responses_payload.pop('max_completion_tokens')

    # Remove Chat Completions-only parameters not supported by the Responses API
    for unsupported_key in (
        'stream_options',
        'logit_bias',
        'frequency_penalty',
        'presence_penalty',
        'stop',
    ):
        responses_payload.pop(unsupported_key, None)

    # Convert Chat Completions tools format to Responses API format
    # Chat Completions: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    # Responses API:    {"type": "function", "name": ..., "description": ..., "parameters": ...}
    if 'tools' in responses_payload and isinstance(responses_payload['tools'], list):
        converted_tools = []
        for tool in responses_payload['tools']:
            if isinstance(tool, dict) and 'function' in tool:
                func = tool['function']
                converted_tool = {'type': tool.get('type', 'function')}
                if isinstance(func, dict):
                    converted_tool['name'] = func.get('name', '')
                    if 'description' in func:
                        converted_tool['description'] = func['description']
                    if 'parameters' in func:
                        converted_tool['parameters'] = func['parameters']
                    if 'strict' in func:
                        converted_tool['strict'] = func['strict']
                converted_tools.append(converted_tool)
            else:
                # Already in correct format or unknown format, pass through
                converted_tools.append(tool)
        responses_payload['tools'] = converted_tools

    return responses_payload


def convert_responses_result(response: dict) -> dict:
    """
    Convert non-streaming Responses API result to Chat Completions format.

    Extracts text from message output items so all downstream consumers
    (frontend tasks, get_content_from_response) work without modification.
    """
    status = response.get('status')
    error = response.get('error')

    if isinstance(error, dict) and error.get('message'):
        normalized_error = {k: v for k, v in error.items() if v is not None}
        if status:
            normalized_error.setdefault('status', status)
        return {'error': normalized_error}

    if isinstance(error, str) and error:
        normalized_error = {
            'message': error,
            'type': 'responses_api_error',
        }
        if status:
            normalized_error['status'] = status
        return {'error': normalized_error}

    if status and status != 'completed':
        return {
            'error': {
                'message': f"Responses API request ended with status '{status}'.",
                'type': 'responses_api_error',
                'status': status,
            }
        }

    output_items = response.get('output', [])

    content = ''
    for item in output_items:
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    content += part.get('text', '')

    return {
        'id': response.get('id', ''),
        'object': 'chat.completion',
        'model': response.get('model', ''),
        'choices': [
            {
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': content,
                },
                'finish_reason': 'stop',
            }
        ],
        'usage': response.get('usage', {}),
    }


@router.post('/chat/completions')
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_system_prompt: bool = False,
):
    # NOTE: We intentionally do NOT use Depends(get_async_session) here.
    # Database operations (get_model_by_id, AccessGrants.has_access) manage their own short-lived sessions.
    # This prevents holding a connection during the entire LLM call (30-60+ seconds),
    # which would exhaust the connection pool under concurrent load.

    # bypass_filter is read from request.state to prevent external clients from
    # setting it via query parameter (CVE fix). Only internal server-side callers
    # (e.g. utils/chat.py) should set request.state.bypass_filter = True.
    bypass_filter = getattr(request.state, 'bypass_filter', False)
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    idx = 0
    requested_model_id = form_data.get('model', '')

    payload = {**form_data}
    metadata = payload.pop('metadata', None)

    model_id = form_data.get('model')
    model_info = await Models.get_model_by_id(model_id)

    # Check model info and override the payload
    if model_info:
        if model_info.base_model_id:
            base_model_id = (
                request.base_model_id if hasattr(request, 'base_model_id') else model_info.base_model_id
            )  # Use request's base_model_id if available
            payload['model'] = base_model_id
            model_id = base_model_id

        params = model_info.params.model_dump()

        if params:
            system = params.pop('system', None)

            payload = apply_model_params_to_body_openai(params, payload)
            if not bypass_system_prompt:
                payload = apply_system_prompt_to_body(system, payload, metadata, user)

        await check_model_access(user, model_info, bypass_filter)
    else:
        await check_model_access(user, None, bypass_filter)

    # Check if model is already in app state cache to avoid expensive get_all_models() call
    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS
    model = models.get(model_id)

    if model:
        idx = model['urlIdx']
    else:
        raise HTTPException(
            status_code=404,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    # Get the API config for the model
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(
            request.app.state.config.OPENAI_API_BASE_URLS[idx], {}
        ),  # Legacy support
    )

    prefix_id = api_config.get('prefix_id', None)
    if prefix_id:
        payload['model'] = payload['model'].replace(f'{prefix_id}.', '')

    # Add user info to the payload if the model is a pipeline
    if 'pipeline' in model and model.get('pipeline'):
        payload['user'] = {
            'name': user.name,
            'id': user.id,
            'email': user.email,
            'role': user.role,
        }

    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]

    # Check if model is a reasoning model that needs special handling
    if is_openai_new_model(payload['model']):
        payload = openai_reasoning_model_handler(payload)
    elif 'api.openai.com' not in url:
        # Remove "max_completion_tokens" from the payload for backward compatibility
        if 'max_completion_tokens' in payload:
            payload['max_tokens'] = payload['max_completion_tokens']
            del payload['max_completion_tokens']

    if 'max_tokens' in payload and 'max_completion_tokens' in payload:
        del payload['max_tokens']

    # Convert the modified body back to JSON
    if 'logit_bias' in payload and payload['logit_bias']:
        logit_bias = convert_logit_bias_input_to_json(payload['logit_bias'])

        if logit_bias:
            payload['logit_bias'] = json.loads(logit_bias)

    headers, cookies = await get_headers_and_cookies(request, url, key, api_config, metadata, user=user)

    is_responses = api_config.get('api_type') == 'responses'

    if api_config.get('azure', False):
        # Only set api-key header if not using Azure Entra ID authentication
        auth_type = api_config.get('auth_type', 'bearer')
        if auth_type not in ('azure_ad', 'microsoft_entra_id'):
            headers['api-key'] = key

        # Azure v1 format: base URL already ends with /openai/v1,
        # model stays in the payload, no deployment URL rewriting.
        is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

        if is_azure_v1:
            if is_responses:
                payload = convert_to_responses_payload(payload)
                request_url = f'{url.rstrip("/")}/responses'
            else:
                request_url = f'{url.rstrip("/")}/chat/completions'
        else:
            api_version = api_config.get('api_version', '2023-03-15-preview')
            request_url, payload = convert_to_azure_payload(url, payload, api_version)
            headers['api-version'] = api_version

            if is_responses:
                payload = convert_to_responses_payload(payload)
                request_url = f'{request_url}/responses?api-version={api_version}'
            else:
                request_url = f'{request_url}/chat/completions?api-version={api_version}'
    else:
        if is_responses:
            payload = convert_to_responses_payload(payload)
            worker_dispatch = await maybe_dispatch_openclaw_worker(
                model=requested_model_id,
                payload=payload,
                url=url,
                api_config=api_config,
                source_channel='openresponses',
                user=user,
            )
            if worker_dispatch:
                return convert_responses_result(worker_dispatch['response'])
            request_url = f'{url}/responses'
        else:
            worker_dispatch = await maybe_dispatch_openclaw_worker(
                model=requested_model_id,
                payload=payload,
                url=url,
                api_config=api_config,
                source_channel='openresponses',
                user=user,
            )
            if worker_dispatch:
                return convert_responses_result(worker_dispatch['response'])
            request_url = f'{url}/chat/completions'
    # For Chat Completions, strip image parts from multimodal tool messages
    # (Chat Completions doesn't support images in tool content).
    if not is_responses and 'messages' in payload:
        for message in payload['messages']:
            if message.get('role') == 'tool' and isinstance(message.get('content'), list):
                message['content'] = ''.join(
                    part.get('text', '') for part in message['content'] if part.get('type') in ('input_text', 'text')
                )

    payload = json.dumps(payload)

    r = None
    streaming = False
    response = None

    try:
        session = await get_session()

        r = await session.request(
            method='POST',
            url=request_url,
            data=payload,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            # If the provider returned an error status with SSE content-type,
            # read the body and return a proper error response instead of
            # streaming the error back (which hides the error from logs).
            if r.status >= 400:
                error_body = await r.text()
                log.error(
                    'Provider returned HTTP %d with SSE content-type: %s',
                    r.status,
                    error_body[:1000],
                )
                try:
                    error_json = json.loads(error_body)
                    return JSONResponse(status_code=r.status, content=error_json)
                except json.JSONDecodeError:
                    return JSONResponse(
                        status_code=r.status,
                        content={'error': {'message': error_body, 'code': r.status}},
                    )

            streaming = True
            return StreamingResponse(
                stream_wrapper(r, content_handler=stream_chunks_handler),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response = await r.json()
            except Exception as e:
                log.error(e)
                response = await r.text()

            if r.status >= 400:
                if isinstance(response, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response)
                else:
                    return PlainTextResponse(status_code=r.status, content=response)

            # Convert Responses API result to simple format
            if is_responses and isinstance(response, dict):
                response = convert_responses_result(response)

            return response
    except Exception as e:
        log.exception(e)

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


async def embeddings(request: Request, form_data: dict, user):
    """
    Calls the embeddings endpoint for OpenAI-compatible providers.

    Args:
        request (Request): The FastAPI request context.
        form_data (dict): OpenAI-compatible embeddings payload.
        user (UserModel): The authenticated user.

    Returns:
        dict: OpenAI-compatible embeddings response.
    """
    idx = 0
    # Prepare payload/body
    body = json.dumps(form_data)
    # Find correct backend url/key based on model
    model_id = form_data.get('model')
    # Check if model is already in app state cache to avoid expensive get_all_models() call
    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS
    if model_id in models:
        idx = models[model_id]['urlIdx']

    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(url, {}),  # Legacy support
    )

    r = None
    streaming = False

    headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)
    try:
        session = await get_session()
        r = await session.request(
            method='POST',
            url=f'{url}/embeddings',
            data=body,
            headers=headers,
            cookies=cookies,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )

        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json()
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


class ResponsesForm(BaseModel):
    model_config = ConfigDict(extra='allow')

    model: str
    input: Optional[list | str] = None
    instructions: Optional[str] = None
    stream: Optional[bool] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    tools: Optional[list] = None
    tool_choice: Optional[str | dict] = None
    text: Optional[dict] = None
    truncation: Optional[str] = None
    metadata: Optional[dict] = None
    store: Optional[bool] = None
    reasoning: Optional[dict] = None
    previous_response_id: Optional[str] = None


@router.post('/responses')
async def responses(
    request: Request,
    form_data: ResponsesForm,
    user=Depends(get_verified_user),
):
    """
    Forward requests to the OpenAI Responses API endpoint.
    Routes to the correct upstream backend based on the model field.
    """
    payload = form_data.model_dump(exclude_none=True)

    idx = 0
    model_id = form_data.model

    # Enforce per-model access control
    await check_model_access(user, await Models.get_model_by_id(model_id), BYPASS_MODEL_ACCESS_CONTROL)

    body = json.dumps(payload)

    if model_id:
        models = request.app.state.OPENAI_MODELS
        if not models or model_id not in models:
            await get_all_models(request, user=user)
            models = request.app.state.OPENAI_MODELS
        if model_id in models:
            idx = models[model_id]['urlIdx']

    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(url, {}),  # Legacy support
    )

    r = None
    streaming = False

    try:
        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        if api_config.get('azure', False):
            auth_type = api_config.get('auth_type', 'bearer')
            if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                headers['api-key'] = key

            is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

            if is_azure_v1:
                request_url = f'{url.rstrip("/")}/responses'
            else:
                api_version = api_config.get('api_version', '2023-03-15-preview')
                headers['api-version'] = api_version
                model = _sanitize_model_for_url(payload.get('model', ''))
                request_url = f'{url}/openai/deployments/{model}/responses?api-version={api_version}'
        else:
            request_url = f'{url}/responses'

        session = await get_session()
        r = await session.request(
            method='POST',
            url=request_url,
            data=body,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json()
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data

    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


@router.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    """
    Deprecated: proxy all requests to OpenAI API.
    Disabled by default. Set ENABLE_OPENAI_API_PASSTHROUGH=True to enable.
    """

    if not ENABLE_OPENAI_API_PASSTHROUGH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Direct API passthrough is disabled. Set ENABLE_OPENAI_API_PASSTHROUGH=True to enable.',
        )

    body = await request.body()

    # Parse JSON body to resolve model-based routing
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            payload = None

    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
        models = request.app.state.OPENAI_MODELS
        if not models or model_id not in models:
            await get_all_models(request, user=user)
            models = request.app.state.OPENAI_MODELS
        if model_id in models:
            idx = models[model_id]['urlIdx']

    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(
            request.app.state.config.OPENAI_API_BASE_URLS[idx], {}
        ),  # Legacy support
    )

    r = None
    streaming = False

    try:
        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        if api_config.get('azure', False):
            # Only set api-key header if not using Azure Entra ID authentication
            auth_type = api_config.get('auth_type', 'bearer')
            if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                headers['api-key'] = key

            is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

            if is_azure_v1:
                qs = request.url.query
                request_url = f'{url.rstrip("/")}/{path}' + (f'?{qs}' if qs else '')
            else:
                api_version = api_config.get('api_version', '2023-03-15-preview')
                headers['api-version'] = api_version

                payload = json.loads(body)
                url, payload = convert_to_azure_payload(url, payload, api_version)
                body = json.dumps(payload).encode()

                request_url = f'{url}/{path}?api-version={api_version}'
        else:
            request_url = f'{url}/{path}'

        session = await get_session()
        r = await session.request(
            method=request.method,
            url=request_url,
            data=body,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json()
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data

    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail='Open WebUI: Server Connection Error',
        )
    finally:
        if not streaming:
            await cleanup_response(r)
