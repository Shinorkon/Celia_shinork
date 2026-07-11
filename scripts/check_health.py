from __future__ import annotations

import os
import urllib.request


def check(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return True, body[:200]
    except Exception as exc:
        return False, str(exc)


services = {
    "telegram-ingress": f"http://localhost:{os.getenv('TELEGRAM_INGRESS_PORT', '8101')}/health",
    "orchestrator": f"http://localhost:{os.getenv('ORCHESTRATOR_PORT', '8102')}/health",
    "worker-runtime": f"http://localhost:{os.getenv('WORKER_RUNTIME_PORT', '8103')}/health",
    "scheduler": f"http://localhost:{os.getenv('SCHEDULER_PORT', '8104')}/health",
    "policy-gateway": f"http://localhost:{os.getenv('POLICY_GATEWAY_PORT', '8105')}/health",
    "admin-console-api": f"http://localhost:{os.getenv('ADMIN_API_PORT', '8106')}/health",
    "admin-console-web": f"http://localhost:{os.getenv('ADMIN_WEB_PORT', '8107')}/",
    "litellm": f"http://localhost:{os.getenv('LITELLM_PORT', '4000')}/health/liveliness",
}

any_fail = False
for name, url in services.items():
    ok, detail = check(url)
    status = "OK" if ok else "FAIL"
    print(f"{status:4} {name:18} {url} -> {detail}")
    any_fail = any_fail or (not ok)

if any_fail:
    raise SystemExit(1)
