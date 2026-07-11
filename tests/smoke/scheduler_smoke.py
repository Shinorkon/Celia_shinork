from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone


SCHEDULER_URL = "http://localhost:8104"


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    run_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    created = post_json(
        f"{SCHEDULER_URL}/jobs",
        {
            "job_type": "once",
            "text": "buy groceries",
            "run_at": run_at,
            "target_user_id": "222222222",
            "chat_id": "9001",
            "thread_id": "42",
        },
    )
    assert created["status"] == "active", created
    job_id = created["job_id"]

    jobs = get_json(f"{SCHEDULER_URL}/jobs")
    assert any(j["job_id"] == job_id for j in jobs), jobs

    paused = post_json(f"{SCHEDULER_URL}/jobs/{job_id}/pause", {})
    assert paused["status"] == "paused", paused

    resumed = post_json(f"{SCHEDULER_URL}/jobs/{job_id}/resume", {})
    assert resumed["status"] == "active", resumed

    print("scheduler_smoke_ok")


if __name__ == "__main__":
    main()
