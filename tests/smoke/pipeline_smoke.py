from __future__ import annotations

"""End-to-end pipeline smoke test: Telegram ingress → orchestrator → worker → admin.

Requires: all services running via docker-compose (including LiteLLM for LLM roles).
"""

import json
import urllib.request


INGRESS_URL = "http://localhost:8101"
ORCHESTRATOR_URL = "http://localhost:8102"
WORKER_URL = "http://localhost:8103"
ADMIN_URL = "http://localhost:8106"


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


def get_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    print("pipeline_smoke:")

    # 1. Health checks
    for name, url in [
        ("ingress", f"{INGRESS_URL}/health"),
        ("orchestrator", f"{ORCHESTRATOR_URL}/health"),
        ("worker", f"{WORKER_URL}/health"),
        ("admin-api", f"{ADMIN_URL}/health"),
    ]:
        resp = get_json(url)
        assert resp["status"] == "ok", f"{name} unhealthy: {resp}"
        print(f"  ✓ {name} health")

    # 2. Ingress: send a message through the webhook
    webhook_payload = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "text": "please remind me tomorrow to buy groceries",
            "message_thread_id": 42,
            "chat": {"id": 9001},
            "from": {"id": 111111111},
        },
    }

    ingress_result = post_json(f"{INGRESS_URL}/webhook", webhook_payload)
    assert ingress_result["accepted"] is True, ingress_result
    print(f"  ✓ ingress accepted: trigger={ingress_result.get('trigger')}")

    # 3. Orchestrator: process one message from the stream
    orch_result = post_json(f"{ORCHESTRATOR_URL}/process-next", {})
    assert orch_result["processed"] is True, orch_result
    print(f"  ✓ orchestrator dispatched: run_id={orch_result.get('run_id')}")

    # 4. Worker: process one message (calls LiteLLM for scheduler role)
    worker_result = post_json(f"{WORKER_URL}/process-next", {})
    assert worker_result["processed"] is True, worker_result
    status = worker_result.get("status", "unknown")
    # LLM may fail if LiteLLM is not configured, but the pipeline itself should work
    print(f"  ✓ worker completed: status={status}")

    # 5. Admin: verify queue metrics
    queue = get_json(f"{ADMIN_URL}/metrics/queue-depth")
    assert "ingress_pending" in queue, queue
    assert "orchestration_pending" in queue, queue
    assert "worker_pending" in queue, queue
    print(f"  ✓ admin queue-depth: {queue}")

    print("pipeline_smoke_ok")


if __name__ == "__main__":
    main()
