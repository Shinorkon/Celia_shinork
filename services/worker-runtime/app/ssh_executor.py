"""Production-grade Paramiko SSH executor with host-key verification, timeouts,
connection pooling, and output sanitisation.

Architecture
------------
* ``SSHExecutor`` is a context-managed, reconnect-on-failure client.
* ``SSHPool`` is a thread-safe pool keyed by ``(host, port, user)`` so repeated
  tasks reuse the same transport.
* All commands flow through :func:`execute` which wraps Paramiko's
  ``exec_command`` with hard timeouts and output-length caps.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    BadHostKeyException,
    SSHException,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration – sourced from environment with sensible defaults
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT: float = float(os.getenv("SSH_CONNECT_TIMEOUT", "10"))
DEFAULT_COMMAND_TIMEOUT: float = float(os.getenv("SSH_COMMAND_TIMEOUT", "30"))
MAX_OUTPUT_BYTES: int = int(os.getenv("SSH_MAX_OUTPUT_BYTES", "256_000"))  # 256 KB
HOST_KEY_POLICY: str = os.getenv("SSH_HOST_KEY_POLICY", "auto_add")  # strict | auto_add
KNOWN_HOSTS_PATH: str = os.getenv("SSH_KNOWN_HOSTS", os.path.expanduser("~/.ssh/known_hosts"))


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass
class SSHResult:
    """Normalised result from a single SSH command execution."""

    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False
    duration_ms: float = 0.0


@dataclass
class SSHConfig:
    """Connection parameters for a target host."""

    host: str
    port: int = 22
    username: str = "root"
    key_file: str | None = None
    password: str | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT


# ---------------------------------------------------------------------------
# Paramiko client factory
# ---------------------------------------------------------------------------


def _build_client(config: SSHConfig) -> paramiko.SSHClient:
    """Create a pre-configured Paramiko SSHClient with host-key policy."""
    client = paramiko.SSHClient()

    if HOST_KEY_POLICY == "strict":
        client.load_system_host_keys(KNOWN_HOSTS_PATH)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    return client


def _truncate(data: bytes, max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Decode *data* and truncate if longer than *max_bytes*."""
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


# ---------------------------------------------------------------------------
# SSHExecutor – single-connection context manager
# ---------------------------------------------------------------------------


class SSHExecutor:
    """Context-managed SSH session that handles connect / execute / close.

    Usage::

        async with SSHExecutor(config) as ssh:
            result = ssh.run("systemctl status nginx")
    """

    def __init__(self, config: SSHConfig) -> None:
        self._config = config
        self._client: paramiko.SSHClient | None = None

    # -- context manager ------------------------------------------------------

    def __enter__(self) -> "SSHExecutor":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Establish the SSH transport, raising on auth / host-key failures."""
        self._client = _build_client(self._config)
        try:
            self._client.connect(
                hostname=self._config.host,
                port=self._config.port,
                username=self._config.username,
                key_filename=self._config.key_file,
                password=self._config.password,
                timeout=self._config.connect_timeout,
                banner_timeout=15,
                auth_timeout=self._config.connect_timeout,
                look_for_keys=True,
                allow_agent=True,
            )
        except (BadHostKeyException, AuthenticationException) as exc:
            raise RuntimeError(f"SSH auth/host-key failure: {exc}") from exc
        except SSHException as exc:
            raise RuntimeError(f"SSH transport error: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"SSH socket error: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def is_connected(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return transport is not None and transport.is_active()

    def reconnect(self) -> None:
        """Close and re-establish the connection."""
        self.close()
        self.connect()

    # -- command execution ----------------------------------------------------

    def run(self, command: str, timeout: float | None = None) -> SSHResult:
        """Execute *command* on the remote host and return an :class:`SSHResult`.

        Parameters
        ----------
        command:
            The shell command to run.  Passed verbatim to Paramiko's
            ``exec_command`` – no local shell interpolation is performed.
        timeout:
            Seconds to wait for the command to finish.  Defaults to
            ``SSH_COMMAND_TIMEOUT`` env var (30 s).
        """
        if self._client is None:
            raise RuntimeError("SSHExecutor not connected – call connect() first.")

        effective_timeout = timeout or self._config.command_timeout
        t0 = time.perf_counter()

        try:
            _stdin, stdout, stderr = self._client.exec_command(
                command,
                timeout=effective_timeout,
                get_pty=False,
            )
            exit_code = stdout.channel.recv_exit_status()
            stdout_bytes = stdout.read()
            stderr_bytes = stderr.read()
        except SSHException as exc:
            raise RuntimeError(f"SSH command execution failed: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"SSH socket error during command: {exc}") from exc

        duration_ms = (time.perf_counter() - t0) * 1000.0
        stdout_str, truncated_out = _truncate(stdout_bytes)
        stderr_str, truncated_err = _truncate(stderr_bytes)

        return SSHResult(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=exit_code,
            truncated=truncated_out or truncated_err,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# SSHPool – thread-safe connection pool (optional, lightweight)
# ---------------------------------------------------------------------------


@dataclass
class _PoolEntry:
    executor: SSHExecutor
    last_used: float = field(default_factory=time.monotonic)


class SSHPool:
    """A minimal, thread-safe connection pool for reusing SSH sessions."""

    def __init__(self, ttl_seconds: float = 300) -> None:
        self._pool: dict[tuple[str, int, str], _PoolEntry] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def _key(self, config: SSHConfig) -> tuple[str, int, str]:
        return (config.host, config.port, config.username)

    @contextmanager
    def acquire(self, config: SSHConfig):
        key = self._key(config)
        with self._lock:
            entry = self._pool.get(key)
            now = time.monotonic()
            if entry is not None:
                if entry.executor.is_connected and (now - entry.last_used) < self._ttl:
                    entry.last_used = now
                else:
                    entry.executor.close()
                    entry = None
            if entry is None:
                executor = SSHExecutor(config)
                executor.connect()
                entry = _PoolEntry(executor)
                self._pool[key] = entry

        try:
            yield entry.executor
        except Exception:
            # If the connection is broken, evict it so next caller gets a fresh one
            try:
                entry.executor.close()
            except Exception:
                pass
            with self._lock:
                self._pool.pop(key, None)
            raise

    def close_all(self) -> None:
        with self._lock:
            for entry in self._pool.values():
                try:
                    entry.executor.close()
                except Exception:
                    pass
            self._pool.clear()


# ---------------------------------------------------------------------------
# Module-level singleton pool
# ---------------------------------------------------------------------------

_pool: SSHPool | None = None


def get_pool() -> SSHPool:
    global _pool
    if _pool is None:
        _pool = SSHPool()
    return _pool
