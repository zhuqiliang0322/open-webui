# Open WebUI Local Agent Notes

Last checked: 2026-05-02

## Project Identity

This checkout is the local customized Open WebUI project used as the browser frontend for OpenClaw.

- Repository path: `/Users/panda/open-webui`
- Current branch: `dev`
- Remote fork: `https://github.com/zhuqiliang0322/open-webui.git`
- Upstream remote: `https://github.com/open-webui/open-webui.git`
- Current package version: `0.9.2`
- Local UI: `http://127.0.0.1:3000`

This is not a plain upstream checkout. It contains local OpenClaw patches for Responses routing, Worker dispatch, result cards, artifact preview, and attachment forwarding.

## Current Runtime Shape

Main chat path:

```text
Open WebUI -> OpenClaw Gateway -> LM Studio -> local GGUF models
```

Long task / visual task path:

```text
Open WebUI -> OpenClaw Worker API -> OpenClaw main agent -> LM Studio -> local GGUF models
```

Runtime ports:

| Service | URL | Purpose |
| --- | --- | --- |
| Open WebUI | `http://127.0.0.1:3000` | Browser UI |
| OpenClaw Gateway | `http://127.0.0.1:18789/v1` | OpenAI-compatible Responses gateway |
| OpenClaw Worker API | `http://127.0.0.1:8090` | Background Worker jobs |
| LM Studio | `http://127.0.0.1:1234` | Local model server |

The OpenClaw Gateway is loopback-only and token protected. A plain unauthenticated request to `/v1/models` can return `401`; that is expected.

## Local Configuration

The local `.env` file is ignored by Git and contains the active Open WebUI connection settings.

Important effective values:

```text
ENABLE_OPENAI_API=true
ENABLE_OLLAMA_API=false
OPENAI_API_BASE_URL=http://127.0.0.1:18789/v1
OPENAI_API_CONFIGS={"0":{"api_type":"responses","connection_type":"local"}}
DEFAULT_MODELS=openclaw/main
DATA_DIR=/Users/panda/open-webui/.data
```

The OpenClaw Worker API defaults to:

```text
OPENCLAW_WORKER_API_BASE_URL=http://127.0.0.1:8090
```

The project configuration has also persisted the Worker base URL in Open WebUI's config state:

```json
{
  "0": {
    "api_type": "responses",
    "connection_type": "local",
    "worker_api_base_url": "http://127.0.0.1:8090"
  }
}
```

Do not commit `.env`, `.data/`, `.run/`, `.venv/`, `backend/data/webui.db`, or local generated assets.

## OpenClaw Status

OpenClaw service configuration in use:

```text
OPENCLAW_CONFIG_PATH=/Users/panda/OpenClaw/config/openclaw.json
OPENCLAW_STATE_DIR=/Users/panda/OpenClaw/config
OPENCLAW_GATEWAY_PORT=18789
```

The service-side OpenClaw config is the source of truth for this UI path.

Current OpenClaw model entry:

```text
openclaw/main
```

Current `main` agent model from OpenClaw config:

```text
lmstudio/qwen/qwen3.6-35b-a3b
```

Recent LM Studio model state:

| Model | Profile | Mode |
| --- | --- | --- |
| `qwen/qwen3.6-35b-a3b` | `main-fast` | pinned |
| `lmstudio-community/qwen3.5-122b-a10b` | `heavy-122b` | jit |
| `text-embedding-qwen3-embedding-0.6b` | `embed` | pinned |

Recent Worker queue state when checked:

```text
queued: 0
active: 1
completed today: 8
failed today: 0
```

One `heavy-122b` Worker job was still running during the check. It had not failed, but logs showed retry/context pressure, so long Worker jobs should be watched from the Worker queue.

## OpenClaw Integration Files

Backend Worker integration:

- `/Users/panda/open-webui/backend/open_webui/routers/openai.py`
- `/Users/panda/open-webui/backend/open_webui/config.py`
- `/Users/panda/open-webui/backend/open_webui/test/util/test_openai_worker.py`

Frontend request and status integration:

- `/Users/panda/open-webui/src/routes/+layout.svelte`
- `/Users/panda/open-webui/src/lib/apis/openai/index.ts`
- `/Users/panda/open-webui/src/lib/utils/openclaw-worker.ts`
- `/Users/panda/open-webui/src/lib/utils/openclaw-worker.test.ts`
- `/Users/panda/open-webui/src/lib/components/chat/Messages/ResponseMessage.svelte`
- `/Users/panda/open-webui/src/lib/components/chat/Messages/ResponseMessage/OpenClawWorkerStatus.svelte`
- `/Users/panda/open-webui/src/lib/components/chat/Messages/Markdown/MarkdownInlineTokens.svelte`

Local runtime helpers:

- `/Users/panda/open-webui/scripts/start-local-open-webui.sh`
- `/Users/panda/open-webui/scripts/stop-local-open-webui.sh`
- `/Users/panda/open-webui/scripts/verify-dev-stack.sh`
- `/Users/panda/open-webui/docs/local-patch-maintenance.md`

## Behavior Summary

When the selected model starts with `openclaw/`, the frontend first tries the Worker path for tasks that should run in the background.

The backend decides whether to create a Worker job by checking:

- model id starts with `openclaw/`
- OpenClaw endpoint is local
- Worker API is configured
- prompt or attachments look like a Worker-suitable task
- `/estimate` says orchestration or long-running handling is useful

If handled, Open WebUI returns a short Worker acknowledgement into the chat. The UI then polls the Worker job and replaces the status card with final text, images, and artifacts when available.

If not handled by Worker, the request continues through the normal Responses path to OpenClaw Gateway.

## Validation Commands

Use these from `/Users/panda/open-webui`.

```bash
curl -fsS http://127.0.0.1:3000/health
curl -fsS http://127.0.0.1:3000/api/version
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:8090/queue | jq '{queuedCount, activeCount, completedToday, failedToday}'
curl -fsS -H "Authorization: Bearer $(awk -F= '/^OPENAI_API_KEY=/{print $2}' .env)" \
  http://127.0.0.1:18789/v1/models | jq '[.data[].id]'
```

Targeted regression checks:

```bash
./.venv/bin/python -m pytest backend/open_webui/test/util/test_openai_worker.py
npx vitest run src/lib/utils/openclaw-worker.test.ts
```

Branch rule check:

```bash
./scripts/verify-dev-stack.sh
```

OpenClaw checks from `/Users/panda/OpenClaw`:

```bash
OPENCLAW_CONFIG_PATH=/Users/panda/OpenClaw/config/openclaw.json \
OPENCLAW_STATE_DIR=/Users/panda/OpenClaw/config \
openclaw gateway status

OPENCLAW_CONFIG_PATH=/Users/panda/OpenClaw/config/openclaw.json \
OPENCLAW_STATE_DIR=/Users/panda/OpenClaw/config \
openclaw health
```

## Latest Verification Result

The following checks passed on 2026-05-02:

- Open WebUI `/health`
- Open WebUI `/api/version`, returning `0.9.2`
- OpenClaw Gateway connectivity
- Worker API `/health`
- Worker API queue inspection
- OpenClaw model list with local token
- `./.venv/bin/python -m pytest backend/open_webui/test/util/test_openai_worker.py`
- `npx vitest run src/lib/utils/openclaw-worker.test.ts`

Regression count:

```text
backend Worker tests: 39 passed
frontend Worker display tests: 19 passed
```

## Branch Maintenance

The documented maintenance model is:

- `main`: upstream release baseline only
- `codex/openclaw-patch-stack`: source-of-truth local patch stack
- `dev`: working/release mirror of `codex/openclaw-patch-stack`

Current issue:

```text
./scripts/verify-dev-stack.sh
error: dev does not match codex/openclaw-patch-stack
```

Reason:

```text
dev has one extra commit:
0d478df20 fix(openclaw): 修复上传文件未完整传给 Worker 的问题
```

Files touched by that commit:

```text
backend/open_webui/routers/openai.py
backend/open_webui/test/util/test_openai_worker.py
```

This does not block the currently running local UI, but it should be fixed before treating the patch stack as clean.

## Operational Notes

- Prefer `scripts/start-local-open-webui.sh` and `scripts/stop-local-open-webui.sh` for local service control.
- Do not assume Docker is the active runtime here.
- Use the repo `.venv` for Python checks.
- If result cards look stale after frontend changes, rebuild and restart the local service before judging the UI.
- For image or artifact display issues, verify the actual chat card at `127.0.0.1:3000`, not only backend JSON.
- For attachment problems, test with the same uploaded file path or file id when possible.
- Keep Open WebUI routed through OpenClaw unless the user explicitly asks for direct LM Studio access.
