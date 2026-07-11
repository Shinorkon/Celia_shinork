from __future__ import annotations

"""Smoke test for worker-runtime SSH executor + policy-gateway integration.

Requires: worker-runtime and policy-gateway services running.
Set SSH_HOST env var or export before running for real SSH testing.
Without SSH_HOST, executor returns dry-run stubs.
"""

import json
import os
import urllib.request


WORKER_URL = "http://localhost:8103"
POLICY_URL = "http://localhost:8105"


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    print("ssh_executor_smoke:")

    # 1. Health checks
    worker_health = get_json(f"{WORKER_URL}/health")
    assert worker_health["status"] == "ok", f"Worker unhealthy: {worker_health}"
    print("  ✓ worker health")

    policy_health = get_json(f"{POLICY_URL}/health")
    assert policy_health["status"] == "ok", f"Policy unhealthy: {policy_health}"
    print("  ✓ policy-gateway health")

    # 2. Allowed command via executor
    result = post_json(
        f"{WORKER_URL}/task",
        {
            "run_id": "smoke-test-allowed",
            "agent_role": "executor",
            "command": "echo hello world",
        },
    )
    assert result["status"] in ("completed", "failed"), (
        f"Expected completed/dry-run for echo, got: {result}"
    )
    print(f"  ✓ echo command: status={result['status']}")

    # 3. Blocked command via executor
    result = post_json(
        f"{WORKER_URL}/task",
        {
            "run_id": "smoke-test-blocked",
            "agent_role": "executor",
            "command": "rm -rf /etc",
        },
    )
    assert result["status"] == "blocked", (
        f"Expected blocked for rm -rf, got: {result}"
    )
    print(f"  ✓ rm -rf blocked: reason={result.get('policy_reason')}")

    # 4. LLM agent call (non-executor role; requires LiteLLM)
    ssh_host = os.environ.get("SSH_HOST", "")
    if ssh_host:
        print(f"  ℹ SSH_HOST={ssh_host} – executor will use real SSH")
    else:
        print("  ℹ SSH_HOST not set – executor runs dry-run stubs")

    # 5. Non-executor role (LLM-powered)
    result = post_json(
        f"{WORKER_URL}/task",
        {
            "run_id": "smoke-test-llm",
            "agent_role": "frontoffice",
            "text": "Say 'hello' in exactly one word.",
        },
    )
    # LLM may fail if LiteLLM is down, but the API should still return a
    # structured response (status=failed with an error message).
    assert result["status"] in ("completed", "failed"), (
        f"Expected completed/failed for LLM call, got: {result}"
    )
    print(f"  ✓ LLM frontoffice: status={result['status']}")

    print("ssh_executor_smoke_ok")


if __name__ == "__main__":
    main()
