from __future__ import annotations

import json
import urllib.error
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

    # Idempotent DELETE: first cancels, second must not 500
    def delete_job(jid: str) -> tuple[int, str]:
        req = urllib.request.Request(f"{SCHEDULER_URL}/jobs/{jid}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    code1, body1 = delete_job(job_id)
    assert code1 < 400, (code1, body1)
    code2, body2 = delete_job(job_id)
    assert code2 < 400, (code2, body2)

    print("scheduler_smoke_ok")


if __name__ == "__main__":
    main()
