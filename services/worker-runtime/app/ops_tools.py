"""Phase D ops agent — Celia self-ops tools (multi-step loop).

Scoped to this VPS's Celia/AOP stack only (aop-*/celia-* containers,
/root/Celia logs, nginx edge for celia.falulaan.com, local health ports).
No arbitrary remote SSH fleet. No Shnuk/Oreuda/Budgy/Directors Eye/Shino-chan.

Reads run after ingress auto/confirm. Restarts require mutate_confirmed
(from an ingress confirm on a write/deploy ask).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

SSH_HOST = os.getenv("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "root")
SSH_KEY_FILE = os.getenv("SSH_KEY_FILE", "") or None

# Own stack only — never operate on other apps on this host.
ALLOWED_CONTAINERS: frozenset[str] = frozenset(
    {
        "aop-ingress",
        "aop-orchestrator",
        "aop-worker",
        "aop-scheduler",
        "aop-policy",
        "aop-admin-api",
        "celia-litellm",
        "celia-redis",
    }
)
ALLOWED_PREFIXES: tuple[str, ...] = ("aop-", "celia-")

REFUSED_NAME_RE = re.compile(
    r"(?i)\b(?:"
    r"shnuk|oreuda|budgy|budget[-_]?tracker|directors?\s*eye|"
    r"shino[-_]?chan|/opt/shino|/opt/flutter|/home/shino"
    r")\b"
)

# LiteLLM /health requires the master key (401 without Authorization).
# /health/liveliness is the unauthenticated process probe LiteLLM documents
# for k8s — Celia-only change; shared litellm config left untouched.
CELIA_HEALTH_URLS: dict[str, str] = {
    "ingress": "http://127.0.0.1:8101/health",
    "orchestrator": "http://127.0.0.1:8102/health",
    "worker": "http://127.0.0.1:8103/health",
    "scheduler": "http://127.0.0.1:8104/health",
    "policy": "http://127.0.0.1:8105/health",
    "admin-api": "http://127.0.0.1:8106/health",
    "litellm": "http://127.0.0.1:4000/health/liveliness",
}

OPS_TOOL_NAMES = frozenset(
    {
        "ops_stack_status",
        "ops_service_health",
        "ops_host_resources",
        "ops_container_logs",
        "ops_edge_status",
        "ops_restart_container",
    }
)

ShellRunner = Callable[[str], str]


def _default_ssh_run(command: str) -> str:
    """Run a fixed template command on the host via SSH (127.0.0.1)."""
    if not SSH_HOST:
        return "SSH not configured — cannot run host command."
    try:
        from ssh_executor import SSHConfig, SSHExecutor
    except ImportError:
        from app.ssh_executor import SSHConfig, SSHExecutor  # type: ignore

    cfg = SSHConfig(
        host=SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        key_file=SSH_KEY_FILE,
        command_timeout=25.0,
    )
    try:
        with SSHExecutor(cfg) as ssh:
            result = ssh.run(command)
    except Exception as exc:
        logger.warning("ops_ssh_error: %s", exc)
        return f"SSH error: {exc}"
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"stderr: {err}")
    parts.append(f"exit={result.exit_code}")
    text = "\n".join(parts)
    if result.truncated:
        text += "\n(truncated)"
    # Cap for model context / Telegram later
    if len(text) > 6000:
        text = text[:6000] + "\n…(truncated)"
    return text


def is_allowed_container(name: str) -> bool:
    n = (name or "").strip()
    if not n or REFUSED_NAME_RE.search(n):
        return False
    if n in ALLOWED_CONTAINERS:
        return True
    return any(n.startswith(p) for p in ALLOWED_PREFIXES)


def refuse_if_other_app(text: str) -> Optional[str]:
    if REFUSED_NAME_RE.search(text or ""):
        return "Refused — out of policy (other apps / non-Celia targets)."
    return None


def ops_stack_status(run_shell: ShellRunner) -> str:
    cmd = (
        "docker ps -a --filter name=aop- --filter name=celia- "
        "--format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"
    )
    return run_shell(cmd)


def ops_service_health(services: Optional[list[str]] = None) -> str:
    """Hit Celia health endpoints via host network (no shell)."""
    wanted = services or list(CELIA_HEALTH_URLS.keys())
    lines: list[str] = []
    with httpx.Client(timeout=3.0) as client:
        for key in wanted:
            url = CELIA_HEALTH_URLS.get(key)
            if not url:
                lines.append(f"{key}: unknown service key")
                continue
            try:
                r = client.get(url)
                body = (r.text or "")[:120].replace("\n", " ")
                lines.append(f"{key}: HTTP {r.status_code} {body}")
            except Exception as exc:
                lines.append(f"{key}: error {exc}")
    return "\n".join(lines) if lines else "(no services checked)"


def ops_host_resources(run_shell: ShellRunner) -> str:
    return run_shell("uptime && echo '---' && free -h && echo '---' && df -h / /root 2>/dev/null | head -20")


def ops_container_logs(
    run_shell: ShellRunner, container: str, tail: int = 80
) -> str:
    name = (container or "").strip()
    blocked = refuse_if_other_app(name)
    if blocked:
        return blocked
    if not is_allowed_container(name):
        return (
            f"Refused — '{name}' is not in the Celia allowlist "
            f"({', '.join(sorted(ALLOWED_CONTAINERS))})."
        )
    try:
        n = max(10, min(int(tail or 80), 200))
    except (TypeError, ValueError):
        n = 80
    # Fixed template — no user string interpolation into shell beyond validated name
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        return "Refused — invalid container name."
    return run_shell(f"docker logs --tail {n} {name} 2>&1")


def ops_edge_status(run_shell: ShellRunner) -> str:
    """nginx (not caddy) fronts celia.falulaan.com on this host."""
    return run_shell(
        "systemctl is-active nginx 2>/dev/null || true; "
        "nginx -t 2>&1 | tail -5; "
        "echo '---'; "
        "curl -s -o /dev/null -w 'local_health=%{http_code}\\n' http://127.0.0.1:8101/health; "
        "curl -s -o /dev/null -w 'celia_https=%{http_code}\\n' "
        "--connect-timeout 5 https://celia.falulaan.com/health || echo celia_https=err"
    )


def ops_restart_container(
    run_shell: ShellRunner,
    container: str,
    *,
    mutate_confirmed: bool = False,
) -> str:
    name = (container or "").strip()
    blocked = refuse_if_other_app(name)
    if blocked:
        return blocked
    if not is_allowed_container(name):
        return (
            f"Refused — '{name}' is not in the Celia allowlist. "
            "Won't restart other apps."
        )
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        return "Refused — invalid container name."
    if not mutate_confirmed:
        return (
            "CONFIRM_REQUIRED: restart is gated. Ask Falulaan to confirm a "
            f"restart of {name} first (ingress confirm), then retry."
        )
    return run_shell(f"docker restart {name}")


def execute_ops_tool(
    name: str,
    args: dict[str, Any],
    *,
    run_shell: Optional[ShellRunner] = None,
    mutate_confirmed: bool = False,
) -> str:
    if name not in OPS_TOOL_NAMES:
        return f"(unsupported ops tool: {name})"
    runner = run_shell or _default_ssh_run
    try:
        if name == "ops_stack_status":
            return ops_stack_status(runner)
        if name == "ops_service_health":
            svcs = args.get("services")
            if isinstance(svcs, str):
                svcs = [s.strip() for s in svcs.split(",") if s.strip()]
            if svcs is not None and not isinstance(svcs, list):
                svcs = None
            return ops_service_health(svcs)
        if name == "ops_host_resources":
            return ops_host_resources(runner)
        if name == "ops_container_logs":
            return ops_container_logs(
                runner,
                str(args.get("container") or ""),
                tail=int(args.get("tail") or 80),
            )
        if name == "ops_edge_status":
            return ops_edge_status(runner)
        if name == "ops_restart_container":
            return ops_restart_container(
                runner,
                str(args.get("container") or ""),
                mutate_confirmed=mutate_confirmed,
            )
    except Exception as exc:
        logger.exception("ops_tool_error name=%s", name)
        return f"ops tool error: {exc}"
    return f"(unsupported ops tool: {name})"
