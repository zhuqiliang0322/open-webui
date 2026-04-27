#!/usr/bin/env bash

set -euo pipefail

main_branch="${1:-main}"
patch_branch="${2:-codex/openclaw-patch-stack}"
dev_branch="${3:-dev}"

fail() {
  echo "error: $*" >&2
  exit 1
}

require_commit() {
  local ref="$1"
  git rev-parse --verify "${ref}^{commit}" >/dev/null 2>&1 || fail "missing branch or ref: ${ref}"
}

require_commit "$main_branch"
require_commit "$patch_branch"
require_commit "$dev_branch"

main_sha="$(git rev-parse "${main_branch}^{commit}")"
patch_sha="$(git rev-parse "${patch_branch}^{commit}")"
dev_sha="$(git rev-parse "${dev_branch}^{commit}")"

if [[ "$(git merge-base "$main_sha" "$patch_sha")" != "$main_sha" ]]; then
  fail "${patch_branch} is not based on ${main_branch}"
fi

if git rev-list --merges "${main_branch}..${patch_branch}" | grep -q .; then
  fail "${patch_branch} contains merge commits; keep the patch stack linear"
fi

if [[ "$patch_sha" != "$dev_sha" ]]; then
  fail "${dev_branch} does not match ${patch_branch}"
fi

echo "Patch stack is valid."
echo
echo "Patch groups:"
git log --reverse --oneline "${main_branch}..${patch_branch}"
