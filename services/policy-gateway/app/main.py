from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field

from packages.telemetry import init_logging, counter

SERVICE_NAME = os.getenv("SERVICE_NAME", "policy-gateway")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class CommandEvalRequest(BaseModel):
    command: str = Field(min_length=1)
    context: dict[str, str] | None = None


class CommandEvalResponse(BaseModel):
    allowed: bool
    reason_code: str
    normalized_command: str


DENY_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\brm\s+-r\b",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\b:(){:|:&};:\b",  # fork bomb
    r"\bcurl\b.*\|\s*(bash|sh|zsh)\b",
    r"\bwget\b.*\|\s*(bash|sh|zsh)\b",
    r"\bnc\b.*\s-e\s",
    r"\bdd\s+if=\b",
    r"\bchmod\s+[0-7]*7",
    r"\bchown\b.*\broot\b",
    r">\s*/dev/sd",
    r"\bmount\b.*\b(/dev|remount)\b",
)

ALLOW_BINARIES = {
    # File ops
    "ls", "cat", "grep", "rg", "head", "tail", "wc", "find", "stat",
    "du", "df", "file", "tree", "zcat", "zless",
    # Navigation & shell
    "cd", "pwd", "echo", "which", "type", "env", "printenv",
    "export", "source", "unset",
    # File editing (safe within project dirs)
    "mkdir", "touch", "cp", "mv", "rm", "rmdir",
    "nano", "vim", "vi",
    # Permissions
    "chmod", "chown",
    # System info
    "whoami", "id", "groups", "free", "uptime", "uname", "hostname",
    "date", "ps", "top", "htop",
    # Process management
    "kill", "pkill", "killall", "pgrep", "pidof",
    # Networking
    "ss", "netstat", "ip", "ping", "curl", "wget", "nc",
    # System services
    "systemctl", "journalctl", "systemd-analyze",
    "timedatectl", "loginctl", "hostnamectl",
    # Package management
    "apt", "apt-get", "dpkg", "snap", "pip", "pip3", "npm", "yarn",
    # Docker & containers
    "docker", "docker-compose",
    # Git (full access)
    "git",
    # Databases
    "pg_isready", "redis-cli", "psql", "sqlite3",
    # Text processing
    "awk", "sed", "sort", "uniq", "cut", "tr", "tee",
    # Archives
    "tar", "gzip", "gunzip", "zip", "unzip",
    # SSH & remote
    "ssh", "scp", "rsync",
    # Build tools
    "make", "cargo", "go", "python", "python3", "node",
    # Scheduling
    "crontab",
}


def _normalize(command: str) -> str:
    return " ".join(command.strip().split())


def _evaluate(command: str) -> tuple[bool, str]:
    normalized = _normalize(command)
    lowered = normalized.lower()

    # 1. Check dangerous patterns (applies to entire command, even chained)
    for pattern in DENY_PATTERNS:
        if re.search(pattern, lowered):
            return False, "deny_dangerous_pattern"

    # 2. Split on && and ; to check each segment independently
    segments = re.split(r'\s*&&\s*|\s*;\s*', normalized)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        seg_lower = segment.lower()
        binary = seg_lower.split(" ", 1)[0]

        # shell builtins that are always safe to delegate
        if binary in ("cd", "export", "source", "unset", "pwd", "echo", "which", "type"):
            continue

        if binary not in ALLOW_BINARIES:
            return False, f"deny_binary_not_allowlisted:{binary}"

        # Per-binary restrictions for potentially destructive commands
        if binary == "systemctl":
            if any(d in seg_lower for d in (" disable ", " mask ")):
                return False, "deny_systemctl_destructive"

        if binary in ("apt", "apt-get"):
            if any(d in seg_lower for d in (" remove ", " purge ", " autoremove")):
                return False, "deny_apt_destructive"

        if binary == "crontab":
            if " -l" not in f" {seg_lower} " and "--list" not in seg_lower:
                return False, "deny_crontab_modify"

        if binary == "ip":
            if any(d in seg_lower for d in (" add ", " del ", " set ", " change ", " replace ", " flush ")):
                return False, "deny_ip_modify"

        if binary == "chown" and re.search(r'\bchown\b.*\broot\b', seg_lower):
            return False, "deny_chown_root"

    # 3. Whole-command checks
    if re.search(r'\brm\b', lowered):
        if re.search(r'\brm\s+.*\s+/(\s|$)', lowered) or re.search(r'\brm\s+-rf\s+/(\s|$)', lowered):
            return False, "deny_rm_root"

    return True, "allow_safe_command"


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        service=SERVICE_NAME,
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "message": "ready"}


@app.post("/v1/policy/command/evaluate", response_model=CommandEvalResponse)
def evaluate_command(payload: CommandEvalRequest) -> CommandEvalResponse:
    normalized = _normalize(payload.command)
    allowed, reason_code = _evaluate(normalized)
    return CommandEvalResponse(
        allowed=allowed,
        reason_code=reason_code,
        normalized_command=normalized,
    )
