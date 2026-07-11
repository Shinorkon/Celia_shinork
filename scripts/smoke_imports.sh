#!/usr/bin/env bash
# Quick smoke test — import each service and check it boots
set -e
ROOT="/home/falulaan/Dev/agent_orchestration_platform"
VENV="$ROOT/.venv/bin/python"
export PYTHONPATH="$ROOT"
export DATABASE_URL="postgresql://agent_user:agent_pass@localhost:5432/agent_platform"
export REDIS_URL="redis://localhost:6379/0"
export SERVICE_NAME="smoke-test"

echo "=== Smoke Testing All Services ==="

test_one() {
    local name="$1"
    local app_dir="$2"
    echo -n "  $name ... "
    if cd "$ROOT/$app_dir" 2>/dev/null && timeout 8 "$VENV" -c "
import importlib, sys
sys.path.insert(0, '.')
m = importlib.import_module('main')
print('OK')
" 2>&1; then
        echo "✅"
    else
        echo "❌ FAILED"
        return 1
    fi
}

test_one "policy-gateway"     "services/policy-gateway/app"
test_one "admin-console-api"  "services/admin-console-api/app"
test_one "telegram-ingress"   "services/telegram-ingress/app"
test_one "orchestrator"       "services/orchestrator/app"
test_one "scheduler"          "services/scheduler/app"
test_one "worker-runtime"     "services/worker-runtime/app"

echo ""
echo "=== All services import OK ==="
