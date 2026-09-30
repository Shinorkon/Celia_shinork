"""Guest vs owner access (chat-only guests).

Owner (ALLOWED_TELEGRAM_USER_IDS) — full tools / life / ops.
Guest (GUEST_TELEGRAM_USER_IDS env or DB role='guest') — chat ingress only;
mutating tools and ops are refused. Guests must NOT be on the owner allowlist.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import psycopg

from packages.config import _parse_int_set

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

OWNER_TELEGRAM_USER_IDS: set[int] = _parse_int_set(
    os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
)
GUEST_TELEGRAM_USER_IDS: set[int] = _parse_int_set(
    os.getenv("GUEST_TELEGRAM_USER_IDS", "")
)

# Actions guests may take (chat / read-only memory recall). Everything else → refuse.
GUEST_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "chat.reply",
        "chat.clarify",
        # Own-books finance only (handlers key off telegram_user_id — never owner books)
        "finance.read",
        "finance.write",
        "finance.amount_pref",
        "finance.recalculate",
        "memory.read",
        "memory.recall",
        "memory.write",  # remember about themselves in their chat
        "note.read",
        "note.create",
        "list.show",
        "task.list",
        "reminder.list",
        "cal.list",
        "life.reflect",
        "life.reflect.notify",
    }
)

# Hard refuse for guests regardless of base policy
GUEST_REFUSE_PREFIXES: tuple[str, ...] = (
    "ops.",
    "policy.secrets_exfil",
    "policy.other_apps",
    "comms.third_party",
    "memory.forget",
    "memory.forget_all",
    "memory.correct",
)


def is_owner(telegram_user_id: int) -> bool:
    return int(telegram_user_id) in OWNER_TELEGRAM_USER_IDS


def is_guest_env(telegram_user_id: int) -> bool:
    return int(telegram_user_id) in GUEST_TELEGRAM_USER_IDS


def is_guest_db(telegram_user_id: int) -> bool:
    """True if users.role = 'guest' and is_active."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT is_active FROM users
                    WHERE telegram_user_id = %s AND role = 'guest'
                    """,
                    (int(telegram_user_id),),
                )
                row = cur.fetchone()
                return row is not None and bool(row[0])
    except Exception as exc:
        logger.error(
            "guest_db_check_error: denying guest-db path user_id=%s err=%s",
            telegram_user_id,
            exc,
        )
        return False


def is_guest(telegram_user_id: int) -> bool:
    """Guest if on GUEST_TELEGRAM_USER_IDS or active DB role=guest.

    Owner allowlist never counts as guest (owner bypass stays owner-only).
    """
    tid = int(telegram_user_id)
    if tid in OWNER_TELEGRAM_USER_IDS:
        return False
    if tid in GUEST_TELEGRAM_USER_IDS:
        return True
    return is_guest_db(tid)


def is_authorized_full(telegram_user_id: int) -> bool:
    """Owner or active role in (authorized, admin, owner) — full tool access."""
    tid = int(telegram_user_id)
    if tid in OWNER_TELEGRAM_USER_IDS:
        return True
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT is_active FROM users
                    WHERE telegram_user_id = %s
                      AND role IN ('authorized', 'admin', 'owner')
                    """,
                    (tid,),
                )
                row = cur.fetchone()
                return row is not None and bool(row[0])
    except Exception as exc:
        logger.error(
            "auth_full_db_error: denying non-owner user_id=%s err=%s", tid, exc
        )
        return False


def is_chat_authorized(telegram_user_id: int) -> bool:
    """Ingress gate: owner, full-authorized, or guest may receive replies.

    Fails closed on DB errors for non-owners. Guests pass via env without DB.
    """
    tid = int(telegram_user_id)
    if tid in OWNER_TELEGRAM_USER_IDS:
        return True
    if tid in GUEST_TELEGRAM_USER_IDS:
        return True
    if is_guest_db(tid):
        return True
    return is_authorized_full(tid)


def guest_policy_for(action: str) -> str:
    """For guests: chat + own finance allowlisted; ops/server hard-refuse."""
    if action.startswith(GUEST_REFUSE_PREFIXES) or action.startswith("ops."):
        return "refuse"
    if action in GUEST_ALLOWED_ACTIONS:
        # Match owner confirm tier for writes (finance handlers already confirm).
        if action in ("finance.write", "memory.forget", "memory.correct", "cal.create", "cal.update"):
            return "confirm"
        return "auto"
    return "refuse"


def guest_may_use_action(action: str) -> bool:
    return guest_policy_for(action) == "auto"


def enforce_user_policy(telegram_user_id: int, action: str, base_policy: str) -> str:
    """Owner/full-auth keep base policy; guests refuse mutating tools."""
    if is_owner(telegram_user_id) or (
        not is_guest(telegram_user_id) and is_authorized_full(telegram_user_id)
    ):
        return base_policy
    if is_guest(telegram_user_id):
        return guest_policy_for(action)
    # Unauthorized — callers should have dropped already; refuse anyway.
    return "refuse"


def ensure_guest_role(telegram_user_id: int) -> Optional[int]:
    """Upsert user as role=guest (chat-only). Never promotes to authorized."""
    tid = int(telegram_user_id)
    if tid in OWNER_TELEGRAM_USER_IDS:
        return None
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users(telegram_user_id, role, is_active)
                    VALUES (%s, 'guest', TRUE)
                    ON CONFLICT (telegram_user_id) DO UPDATE
                      SET role = 'guest', is_active = TRUE
                    RETURNING id
                    """,
                    (tid,),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("ensure_guest_role_error: %s", exc)
        return None
