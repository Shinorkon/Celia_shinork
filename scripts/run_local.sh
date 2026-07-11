#!/usr/bin/env bash
# run_local.sh — Start all Agent Orchestration Platform services locally (no Docker)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── Config ──────────────────────────────────────────────────────────────────
export DATABASE_URL="${DATABASE_URL:-postgresql://agent_user:agent_pass@localhost:5432/agent_platform}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export INGRESS_STREAM="${INGRESS_STREAM:-ingress.accepted}"
export DISPATCH_STREAM="${DISPATCH_STREAM:-orchestration.dispatched}"
export COMPLETION_STREAM="${COMPLETION_STREAM:-worker.completed}"
export NOTIFICATION_STREAM="${NOTIFICATION_STREAM:-notification.requested}"
export DEAD_LETTER_STREAM="${DEAD_LETTER_STREAM:-dead.letter}"
export LITELLM_API_KEY="${LITELLM_API_KEY:-sk-litellm-key}"
export LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000}"
export ALLOWED_TELEGRAM_USER_IDS="${ALLOWED_TELEGRAM_USER_IDS:-111111111,222222222}"
export TELEGRAM_BOT_USERNAME="${TELEGRAM_BOT_USERNAME:-Carliabot}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export POLICY_GATEWAY_URL="${POLICY_GATEWAY_URL:-http://localhost:8105/v1/policy/command/evaluate}"

# ── Python path so services can import packages.* ────────────────────────────
export PYTHONPATH="$ROOT"

# ── Ensure venv exists ──────────────────────────────────────────────────────
if [ ! -d "$ROOT/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$ROOT/.venv"
    "$ROOT/.venv/bin/pip" install -q fastapi uvicorn "redis[hiredis]" "psycopg[binary]" httpx apscheduler sqlalchemy
fi

VENV_PYTHON="$ROOT/.venv/bin/python"

# ── Verify infra ────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Agent Orchestration Platform — Local Dev Mode"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ""
echo "▶ Checking PostgreSQL..."
if pg_isready -q 2>/dev/null; then
    echo "  ✅ PostgreSQL is running"
else
    echo "  ❌ PostgreSQL is NOT running — start it first"
    exit 1
fi

echo "▶ Checking Redis/Valkey..."
if redis-cli ping >/dev/null 2>&1 || valkey-cli ping >/dev/null 2>&1; then
    echo "  ✅ Redis/Valkey is running"
else
    echo "  ❌ Redis/Valkey is NOT running"
    echo "     Run: sudo systemctl start valkey"
    exit 1
fi

echo "▶ Checking DB schema..."
TABLE_COUNT=$("$VENV_PYTHON" -c "
import psycopg, os
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    with c.cursor() as cur:
        cur.execute(\"SELECT count(*) FROM information_schema.tables WHERE table_schema='public'\")
        print(cur.fetchone()[0])
" 2>/dev/null || echo "0")
if [ "$TABLE_COUNT" -gt 5 ]; then
    echo "  ✅ Database has $TABLE_COUNT tables"
else
    echo "  ⚠️  Only $TABLE_COUNT tables found — run: psql -U agent_user -d agent_platform -f db/migrations/001_init.sql"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Starting all services..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Cleanup on exit ─────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Shutting down all services..."
    jobs -p | xargs -r kill 2>/dev/null
    wait 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

# ── Launch services (each in its own app/ dir for relative imports) ──────────

echo "  🚀 telegram-ingress    → http://localhost:8101"
(cd "$ROOT/services/telegram-ingress/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=telegram-ingress "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8101 --log-level warning) &
sleep 0.5

echo "  🚀 orchestrator        → http://localhost:8102"
(cd "$ROOT/services/orchestrator/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=orchestrator "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8102 --log-level warning) &
sleep 0.5

echo "  🚀 worker-runtime      → http://localhost:8103"
(cd "$ROOT/services/worker-runtime/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=worker-runtime "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8103 --log-level warning) &
sleep 0.5

echo "  🚀 scheduler           → http://localhost:8104"
(cd "$ROOT/services/scheduler/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=scheduler "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8104 --log-level warning) &
sleep 0.5

echo "  🚀 policy-gateway      → http://localhost:8105"
(cd "$ROOT/services/policy-gateway/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=policy-gateway "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8105 --log-level warning) &
sleep 0.5

echo "  🚀 admin-console-api   → http://localhost:8106"
(cd "$ROOT/services/admin-console-api/app" && \
 PYTHONPATH="$ROOT" SERVICE_NAME=admin-console-api "$VENV_PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 --port 8106 --log-level warning) &
sleep 0.5

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All services starting. Health check in 2s..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sleep 2

# ── Health check ────────────────────────────────────────────────────────────
for port in 8101 8102 8103 8104 8105 8106; do
    if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
        echo "  ✅ port $port healthy"
    else
        echo "  ⚠️  port $port not responding yet"
    fi
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Platform is running!"
echo ""
echo "  Admin API:    http://localhost:8106"
echo "  Health:       curl http://localhost:8101/health"
echo "  Web Dashboard: cd services/admin-console-web && npm run dev"
echo ""
echo "  Press Ctrl+C to stop all services."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Wait for any job to exit (or Ctrl+C)
wait
