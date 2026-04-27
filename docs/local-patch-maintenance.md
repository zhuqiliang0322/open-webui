# Local Patch Maintenance

This repository now uses a three-branch rule for local OpenClaw customization:

- `main`: upstream release baseline only. Do not place local customization here.
- `codex/openclaw-patch-stack`: the source-of-truth patch stack for local customization. Keep it linear.
- `dev`: the working/release mirror of `codex/openclaw-patch-stack`. Do not commit directly here.

## Current Patch Groups

Keep the patch stack in this order:

1. `feat(stack): add direct connection transport base`
2. `feat(stack): add openclaw worker backend flow`
3. `feat(stack): add openai error presentation layer`
4. `feat(stack): add worker result cards and regression coverage`
5. `feat(openclaw): harden worker attachments and previews`
6. `fix(openclaw): refresh worker final results and previews`
7. `chore(stack): add local runtime helpers and refresh deps`

Why this order:

- Connection/config changes land first because later groups depend on them.
- Worker backend logic stays isolated from UI rendering changes.
- Error rendering stays separate from Worker result cards so upstream UI churn is easier to resolve.
- Attachment and preview hardening follows the Worker UI so access-control changes stay reviewable.
- Result refresh fixes stay after the hardening layer because they depend on the same local-file and final-result helpers.
- Local scripts, ignore rules, and dependency bookkeeping stay last because they are not product logic.

## Upgrade Flow

When upstream releases a new version:

1. Sync `main` to the target upstream release first.
2. Switch to `codex/openclaw-patch-stack`.
3. Rebase it onto `main`.
4. Resolve conflicts one patch group at a time, in commit order.
5. If `pyproject.toml` changes during the rebase, regenerate `uv.lock` on the target version instead of hand-editing the lockfile.
6. Run the targeted checks:

```bash
uv run --with pytest --with pytest-asyncio python -m pytest \
  backend/open_webui/test/util/test_middleware.py \
  backend/open_webui/test/util/test_openai_worker.py

npx vitest run \
  src/lib/apis/openai/direct.test.ts \
  src/lib/utils/openai-errors.test.ts \
  src/lib/utils/openclaw-worker.test.ts
```

7. Run `./scripts/verify-dev-stack.sh`.
8. Move `dev` to the validated patch-stack tip:

```bash
git switch dev
git reset --hard codex/openclaw-patch-stack
```

9. Run `./scripts/verify-dev-stack.sh` again.
10. If the remote `dev` branch should follow this history, push with `--force-with-lease`.

## Rules

- Do not merge `main` directly into `dev`.
- Do not squash all local customization back into one commit.
- Add new local customization on `codex/openclaw-patch-stack`, not on `main`.
- Keep related tests in the same patch group as the feature they protect.
- Treat `uv.lock` as generated output. Rebuild it after dependency changes.
