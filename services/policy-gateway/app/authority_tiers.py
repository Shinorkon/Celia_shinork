"""Authority tiers: how much oversight a command needs before running.

Kept separate from the AST-walking allow/deny logic in main.py, same pattern
as path_policy.py, so "what Carlia can decide alone" is a reviewable config
artifact rather than something buried in parsing code. This is a starting
proposal, not a final spec — exact tier assignment per binary is a product
call, meant to be reviewed and adjusted once it's running in practice, not
treated as settled by virtue of being code.

Tiers (least to most cautious):
  autonomous    — read-only/idempotent, runs immediately, no notification.
  notify_after  — reversible scoped write, runs immediately, a summary
                   notification goes out after the fact.
  confirm_first — destructive or hard-to-reverse; does NOT run until the
                   user explicitly confirms via Telegram.

Hard denies (DENY_PATTERNS, unallowlisted binaries, out-of-scope writes,
etc. in main.py) are evaluated first and are unaffected by tiering — a
denied command has no tier because it never runs at all.
"""

from __future__ import annotations

_TIER_ORDER = {"autonomous": 0, "notify_after": 1, "confirm_first": 2}

# Read-only / idempotent — safe to run without asking every time.
AUTONOMOUS_BINARIES: frozenset[str] = frozenset({
    "ls", "cat", "grep", "rg", "head", "tail", "wc", "find", "stat",
    "du", "df", "file", "tree", "zcat", "zless",
    "cd", "pwd", "echo", "which", "type", "env", "printenv",
    "whoami", "id", "groups", "free", "uptime", "uname", "hostname", "date",
    "ps", "top", "htop", "pgrep", "pidof",
    "ss", "netstat", "ip", "ping",
    "journalctl", "systemd-analyze", "timedatectl", "loginctl", "hostnamectl",
    "pg_isready", "redis-cli", "psql", "sqlite3",
})

# Reversible, scoped writes within the allowlisted project paths — run
# immediately, summarized after the fact.
NOTIFY_AFTER_BINARIES: frozenset[str] = frozenset({
    "mkdir", "touch", "cp", "mv", "tee", "sed", "nano", "vim", "vi",
    "tar", "gzip", "gunzip", "zip", "unzip",
    "make", "cargo", "go", "python", "python3", "node", "npm", "yarn",
    "pip", "pip3",
})

# Destructive or hard-to-reverse — always pause for confirmation first.
CONFIRM_FIRST_BINARIES: frozenset[str] = frozenset({
    "rm", "rmdir", "chmod", "chown", "kill", "pkill", "killall",
    "apt", "apt-get", "dpkg", "snap", "crontab",
    "ssh", "scp", "rsync", "curl", "wget", "nc",
})

_GIT_WRITE_SUBCOMMANDS = frozenset({
    "commit", "push", "merge", "rebase", "reset", "checkout", "branch", "tag", "stash",
})
_SYSTEMCTL_READ_SUBCOMMANDS = frozenset({
    "status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "show",
})
_DOCKER_READ_SUBCOMMANDS = frozenset({
    "ps", "logs", "inspect", "images", "version", "info", "stats", "top",
})
# This platform's own containers (see docker-compose.prod.yml container_name
# entries) — restarting these is a normal, reversible, expected operational
# action; restarting anything else on the host is not this platform's call
# to make alone.
_OWN_CONTAINER_NAMES = frozenset({
    "aop-ingress", "aop-orchestrator", "aop-worker", "aop-scheduler",
    "aop-policy", "aop-admin-api",
})


def classify(binary: str, words: list[str]) -> str:
    """Return the tier for a single already-validated command. `words` is
    the full tokenized command (binary at index 0)."""
    if binary == "git":
        subcommand = words[1] if len(words) > 1 else ""
        return "notify_after" if subcommand in _GIT_WRITE_SUBCOMMANDS else "autonomous"

    if binary == "systemctl":
        subcommand = words[1] if len(words) > 1 else ""
        return "autonomous" if subcommand in _SYSTEMCTL_READ_SUBCOMMANDS else "confirm_first"

    if binary in ("docker", "docker-compose"):
        subcommand = words[1] if len(words) > 1 else ""
        if subcommand in _DOCKER_READ_SUBCOMMANDS:
            return "autonomous"
        if any(name in words for name in _OWN_CONTAINER_NAMES):
            return "notify_after"
        # A subcommand we don't specifically recognize, or a target that
        # isn't one of this platform's own containers — be cautious.
        return "confirm_first"

    if binary in AUTONOMOUS_BINARIES:
        return "autonomous"
    if binary in NOTIFY_AFTER_BINARIES:
        return "notify_after"
    if binary in CONFIRM_FIRST_BINARIES:
        return "confirm_first"

    # Anything not explicitly classified gets the most cautious tier rather
    # than silently defaulting to "fine" — same fail-closed spirit as the
    # rest of the gateway.
    return "confirm_first"


def most_cautious(tiers: list[str]) -> str:
    """Given the tiers of every command node in a (possibly multi-segment)
    input, return the single most cautious one — the overall action's risk
    is bounded by its riskiest sub-command, not its mildest."""
    if not tiers:
        return "autonomous"
    return max(tiers, key=lambda t: _TIER_ORDER.get(t, _TIER_ORDER["confirm_first"]))
