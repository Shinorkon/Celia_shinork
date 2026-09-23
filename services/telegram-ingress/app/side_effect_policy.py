"""Side-effect policy table (Phase C).

Classifies proposed actions into:

  auto     — reversible personal ops (list create/append/show/bought/remove,
             drafts, finance reads, chat replies). Run without asking.
  confirm  — finance writes, destructive/ops shell, messaging third parties,
             deploys. Ask first (finance handlers already do this — reference).
  refuse   — hard-gate / out of policy (other apps, secrets exfil, etc.).

Lists stay auto via list_handlers. Finance writes stay confirm via
finance_handlers. This module is the single reviewable table; ingress
ops/chat paths consult it so dangerous stuff does not auto-fire.

Registration rule (see docs/tool_policy_registry.md): new tools need a
schema in worker llm_client + a key here + a test. Unknown → confirm.
"""
from __future__ import annotations

import re
from typing import Literal

Policy = Literal["auto", "confirm", "refuse"]
ActionKey = str

# ---------------------------------------------------------------------------
# Canonical table — review this, not scattered ifs.
# ---------------------------------------------------------------------------
POLICY_TABLE: dict[ActionKey, Policy] = {
    # Lists — reversible personal (ingress-local)
    "list.create": "auto",
    "list.append": "auto",
    "list.show": "auto",
    "list.bought": "auto",
    "list.remove": "auto",
    "list.rename": "auto",
    "list.done": "auto",
    # Drafts
    "draft.create": "auto",
    "draft.edit": "auto",
    # Chat (reply to user only — no side effect beyond the reply)
    "chat.reply": "auto",
    "chat.clarify": "auto",
    # Finance — writes confirm (handlers already enforce); reads auto
    "finance.write": "confirm",
    "finance.read": "auto",
    # Memory foundation (Life OS slice 1)
    "memory.read": "auto",
    "memory.write": "auto",       # explicit "remember that…"
    "memory.forget": "confirm",
    "memory.correct": "confirm",
    "memory.recall": "auto",
    # Tasks / reminders (Life OS slice 2) — self-ping auto; third-party via comms.third_party
    "task.create": "auto",
    "task.complete": "auto",
    "task.delete": "auto",
    "task.list": "auto",
    "reminder.create": "auto",    # self-chat ping
    "reminder.list": "auto",
    "reminder.cancel": "auto",
    "reminder.edit": "auto",
    "reminder.snooze": "auto",
    # Calendar (Life OS slice 4) — create/update confirm; list/agenda auto
    "cal.create": "confirm",
    "cal.update": "confirm",
    "cal.list": "auto",
    # Notes (memory_items kind=note) — private notes auto
    "note.create": "auto",
    "note.read": "auto",
    # Life-reflect (Life OS slice 5) — proactive self-ping; no shell
    "life.reflect": "auto",
    "life.reflect.notify": "auto",
    # Ops shell / deploys — never auto-fire from chat brochure path
    "ops.shell_read": "confirm",
    "ops.shell_write": "confirm",
    "ops.deploy": "confirm",
    "ops.destructive": "confirm",
    # Messaging anyone who is not the owner chat
    "comms.third_party": "confirm",
    # Hard refuse
    "policy.out_of_scope": "refuse",
    "policy.secrets_exfil": "refuse",
    "policy.other_apps": "refuse",
}

_DEPLOY_RE = re.compile(
    r"(?i)\b(?:deploy|redeploy|docker\s+compose\s+up|kubectl\s+apply|"
    r"helm\s+upgrade|terraform\s+apply)\b"
)
_DESTRUCTIVE_RE = re.compile(
    r"(?i)\b(?:rm\s+-rf|drop\s+database|mkfs|dd\s+if=|shutdown|reboot|"
    r"factory\s*reset)\b"
)
_WRITE_SHELL_RE = re.compile(
    r"(?i)\b(?:restart|stop|kill|systemctl\s+(?:restart|stop|disable)|"
    r"docker\s+(?:restart|stop|rm)|chmod|chown|crontab)\b"
)
_THIRD_PARTY_RE = re.compile(
    r"(?i)\b(?:email|text|sms|dm|message|ping|notify)\b.+\b(?:them|him|her|"
    r"the\s+team|everyone|group|channel)\b"
    r"|\bsend\s+(?:an?\s+)?(?:email|message|sms)\b"
)
_SECRETS_RE = re.compile(
    r"(?i)\b(?:dump|exfil|steal|leak)\b.+\b(?:secret|password|token|key|\.env)\b"
    r"|\b(?:cat|print|show)\b.+\b(?:\.env|id_rsa|credentials)\b"
)
_OTHER_APPS_RE = re.compile(
    r"(?i)\b(?:budget-tracker|oreuda)\b"
)


def policy_for(action: ActionKey) -> Policy:
    """Look up policy; unknown actions default to confirm (fail closed)."""
    return POLICY_TABLE.get(action, "confirm")


def classify_action(intent: str, text: str) -> tuple[ActionKey, Policy]:
    """Map (intent, text) → (action_key, policy).

    Intent comes from intent_router (list|finance|ops|memory|task|reminder|chat|clarify).
    Finance/list handlers usually run before this; still classifies correctly
    for tests and for any path that consults the table directly.
    """
    t = (text or "").strip()
    intent = (intent or "chat").lower()

    if _SECRETS_RE.search(t):
        return "policy.secrets_exfil", policy_for("policy.secrets_exfil")
    if _OTHER_APPS_RE.search(t):
        return "policy.other_apps", policy_for("policy.other_apps")

    if intent == "list":
        low = t.lower()
        if re.search(r"(?i)\b(?:remove|delete|drop|strike)\b", low):
            return "list.remove", policy_for("list.remove")
        if re.search(r"(?i)\b(?:bought|check(?:\s*-?\s*off)?|got)\b", low):
            return "list.bought", policy_for("list.bought")
        if re.search(r"(?i)\b(?:show|view|open|/list)\b", low):
            return "list.show", policy_for("list.show")
        if re.search(r"(?i)\b(?:make|create|start|new)\b.+\blist\b", low):
            return "list.create", policy_for("list.create")
        if re.search(r"(?i)\b(?:add|put|append)\b", low):
            return "list.append", policy_for("list.append")
        return "list.append", policy_for("list.append")

    if intent == "finance":
        if re.search(r"(?i)\b(?:spent|spend|log|receipt|budget|limit|save)\b", t):
            return "finance.write", policy_for("finance.write")
        return "finance.read", policy_for("finance.read")

    if intent in ("task", "reminder"):
        low = t.lower()
        if intent == "reminder" or re.search(r"(?i)\bremind", low):
            if re.search(r"(?i)\b(?:cancel|delete|drop|stop|remove)\b", low):
                return "reminder.cancel", policy_for("reminder.cancel")
            if re.search(r"(?i)\bsnooze\b", low):
                return "reminder.snooze", policy_for("reminder.snooze")
            if re.search(r"(?i)\b(?:edit|change|reschedule)\b", low):
                return "reminder.edit", policy_for("reminder.edit")
            if re.search(r"(?i)\b(?:list|show|what)\b", low) or low.strip() in ("/reminders", "/reminder"):
                return "reminder.list", policy_for("reminder.list")
            return "reminder.create", policy_for("reminder.create")
        if re.search(r"(?i)\b(?:complete|finish|done|check)\b", low):
            return "task.complete", policy_for("task.complete")
        if re.search(r"(?i)\b(?:delete|remove|drop)\b", low):
            return "task.delete", policy_for("task.delete")
        if re.search(r"(?i)\b(?:list|show|my)\b", low) or low.strip() in ("/tasks", "/todos"):
            return "task.list", policy_for("task.list")
        return "task.create", policy_for("task.create")

    if intent == "memory":
        low = t.lower()
        if re.search(r"(?i)\b(?:forget|don\'t\s+remember|do\s+not\s+remember|stop\s+remembering)\b", low):
            return "memory.forget", policy_for("memory.forget")
        if re.search(r"(?i)\b(?:correct|actually|update\s+memory|fix\s+memory)\b", low):
            return "memory.correct", policy_for("memory.correct")
        if re.search(r"(?i)what\s+do\s+you\s+(?:know|remember)\b", low) or low.strip() in ("/memory",):
            return "memory.read", policy_for("memory.read")
        if re.search(r"(?i)\b(?:remember|note\s+that|keep\s+in\s+mind)\b", low):
            return "memory.write", policy_for("memory.write")
        return "memory.read", policy_for("memory.read")

    if intent == "calendar":
        low = t.lower()
        if re.search(r"(?i)\b(?:move|reschedule|change|update|edit)\b", low):
            return "cal.update", policy_for("cal.update")
        if re.search(r"(?i)\b(?:agenda|what(?:'s|\s+is)\s+on|calendar|events)\b", low) or low.strip() in (
            "/agenda", "/calendar", "/events"
        ):
            return "cal.list", policy_for("cal.list")
        return "cal.create", policy_for("cal.create")

    if intent == "note":
        low = t.lower()
        if re.search(r"(?i)^(?:note\s*:|jot|save\s+this|save\s+note|quick\s+note)", low):
            return "note.create", policy_for("note.create")
        return "note.read", policy_for("note.read")

    if intent == "ops":
        if _DEPLOY_RE.search(t):
            return "ops.deploy", policy_for("ops.deploy")
        if _DESTRUCTIVE_RE.search(t):
            return "ops.destructive", policy_for("ops.destructive")
        if _WRITE_SHELL_RE.search(t):
            return "ops.shell_write", policy_for("ops.shell_write")
        return "ops.shell_read", policy_for("ops.shell_read")

    if intent == "clarify":
        return "chat.clarify", policy_for("chat.clarify")

    if _THIRD_PARTY_RE.search(t):
        return "comms.third_party", policy_for("comms.third_party")

    if re.search(r"(?i)\b(?:draft|write\s+(?:a\s+)?(?:email|message|letter))\b", t):
        return "draft.create", policy_for("draft.create")

    return "chat.reply", policy_for("chat.reply")


def is_auto(action: ActionKey) -> bool:
    return policy_for(action) == "auto"


def is_confirm(action: ActionKey) -> bool:
    return policy_for(action) == "confirm"


def is_refuse(action: ActionKey) -> bool:
    return policy_for(action) == "refuse"
