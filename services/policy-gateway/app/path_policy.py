"""Explicit path-scoping rules for write operations reached via the executor.

Kept separate from the shell-parsing/AST logic in main.py so "what Carlia can
write to" is a reviewable config artifact, not buried inside parsing code.
All checks here are static/lexical (no filesystem access) — this module runs
inside the policy-gateway container, which has no access to the target host's
filesystem, so it can only reason about the command text itself.
"""

from __future__ import annotations

import os

# Paths Carlia may write within (subject to GIT_ONLY_PATHS / denylist below).
WRITE_PATH_ALLOWLIST: tuple[str, ...] = (
    "/opt/agent_orchestration_platform",
    "/opt/shino-chan",
    "/opt/flutter",
    "/opt/android-sdk",
    "/home/shino",
    "/tmp",
)

# Never writable via the executor, regardless of the allowlist above.
WRITE_PATH_DENYLIST: tuple[str, ...] = (
    "/etc",
    "/boot",
    "/root/.ssh",
    "/home/shino/.ssh",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/lib/systemd",
    "/etc/systemd",
    "/usr/lib/systemd",
    "/proc",
    "/sys",
)

# Within WRITE_PATH_ALLOWLIST, these specific project roots may only be
# modified via git commit + redeploy, never a direct executor write — this
# is the platform's own safety-critical code (prompts, policy, auth).
# Carlia's other VPS projects are not subject to this stricter rule.
GIT_ONLY_PATHS: tuple[str, ...] = (
    "/opt/agent_orchestration_platform",
)


def _normalize(path: str) -> str:
    """Lexically normalize a path. Returns '' for anything that can't be
    statically resolved (relative paths, since we don't know cwd) so callers
    fail closed rather than guessing."""
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    if not expanded.startswith("/"):
        return ""
    return os.path.normpath(expanded)


def is_path_resolvable(path: str) -> bool:
    return bool(_normalize(path))


def _is_under(path: str, prefix: str) -> bool:
    norm_path = _normalize(path)
    norm_prefix = _normalize(prefix)
    if not norm_path or not norm_prefix:
        return False
    return norm_path == norm_prefix or norm_path.startswith(norm_prefix.rstrip("/") + "/")


def is_denylisted(path: str) -> bool:
    return any(_is_under(path, deny) for deny in WRITE_PATH_DENYLIST)


def is_allowlisted(path: str) -> bool:
    return any(_is_under(path, allow) for allow in WRITE_PATH_ALLOWLIST)


def is_git_only_scope(path: str) -> bool:
    return any(_is_under(path, scope) for scope in GIT_ONLY_PATHS)


def is_allowlist_root(path: str) -> bool:
    """True if *path* IS one of the allowlisted roots itself (not a subpath) —
    used to stop `rm -rf` from deleting an entire project directory."""
    norm_path = _normalize(path)
    return any(norm_path == _normalize(root) for root in WRITE_PATH_ALLOWLIST)


def check_write_target(path: str) -> tuple[bool, str]:
    """Returns (allowed, reason_code) for a proposed write to *path*."""
    if not is_path_resolvable(path):
        return False, "deny_unresolvable_write_target"
    if is_denylisted(path):
        return False, "deny_write_path_denylisted"
    if not is_allowlisted(path):
        return False, "deny_write_path_outside_scope"
    if is_git_only_scope(path):
        return False, "deny_git_only_scope_direct_write"
    return True, "allow_write_path_scoped"
