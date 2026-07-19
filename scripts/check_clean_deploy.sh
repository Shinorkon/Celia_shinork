#!/usr/bin/env bash
# check_clean_deploy.sh — Refuse to redeploy if the checked-out repo has
# uncommitted changes, then run the real deploy.
#
# Why this exists: the coder agent has SSH+write access to other projects on
# this VPS, and agent_orchestration_platform's own policy-gateway now denies
# it *direct* file writes into this repo specifically (see
# services/policy-gateway/app/path_policy.py's GIT_ONLY_PATHS) — but that
# only stops the executor path. This script is the other half: it stops a
# rebuild from silently picking up an out-of-band edit (made before this
# restriction existed, or made directly on the VPS outside the executor
# entirely, e.g. by hand over a raw shell) by refusing to build on a dirty
# tree. Legitimate changes go through `git commit`, which this allows.
#
# Usage: scripts/check_clean_deploy.sh [docker compose service names...]
#   No args   -> rebuilds every service in docker-compose.prod.yml
#   Some args -> rebuilds just those services
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
    echo "❌ Refusing to deploy: the working tree is not clean." >&2
    echo "   Uncommitted or untracked changes found:" >&2
    git status --porcelain | sed 's/^/     /' >&2
    echo "" >&2
    echo "   Commit (or stash) these first, then redeploy." >&2
    exit 1
fi

if [ ! -f .env.prod ]; then
    echo "❌ .env.prod not found — docker-compose.prod.yml must be run with the" >&2
    echo "   production env file, not the local-dev .env (blank API keys, blank" >&2
    echo "   SSH_HOST — that silently runs the executor in dry-run mode)." >&2
    exit 1
fi

echo "✅ Working tree is clean at $(git rev-parse --short HEAD) — proceeding with deploy."
exec docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build "$@"
