"""LiteLLM proxy client for worker agents.

Maps agent roles to LiteLLM model aliases and provides a thin async wrapper
over the LiteLLM ``/chat/completions`` endpoint.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Literal

import httpx

# ---------------------------------------------------------------------------
# Model alias mapping – kept in sync with litellm/config.yaml
# ---------------------------------------------------------------------------

ROLE_MODEL_MAP: dict[str, str] = {
    "frontoffice": "gemini-2.5-flash",
    "planner": "gemini-2.5-flash",
    "executor": "gemini-2.5-flash",
    "coder": "gemini-2.5-flash",
    "document": "gemini-2.5-flash",
    "comms": "gemini-2.5-flash",
    "qa": "gemini-2.5-flash",
    "scheduler": "gemini-2.5-flash",
    "ops-monitor": "gemini-2.5-flash",
    "memory-writer": "gemini-2.5-flash",
    "ops-reflect": "gemini-2.5-flash",
    "life-reflect": "gemini-2.5-flash",
    "life": "gemini-2.5-flash",
    "ops": "gemini-2.5-flash",
}

RUN_SHELL_COMMAND_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "run_shell_command",
        "description": (
            "Run a shell command on the VPS, gated by the policy gateway. "
            "Only call this when you actually need to execute something — "
            "reading a file, checking a service, writing code, running "
            "tests, deploying. The result is returned to you for real; "
            "never write EXEC:-style text expecting it to run on its own — "
            "only this tool call executes anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The exact shell command to run.",
                },
                "justification": {
                    "type": "string",
                    "description": "One short line explaining why this command is needed.",
                },
            },
            "required": ["command"],
        },
    },
}

SAVE_MEMORY_ITEMS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "save_memory_items",
        "description": (
            "Save one or more memory items worth remembering across future "
            "conversations — a stated goal, an explicit decision, a stated "
            "preference, or a completed project milestone. Only call this "
            "when something in the exchange is genuinely memory-worthy; "
            "most exchanges have nothing to save, and that's expected — "
            "don't call this tool just to have called it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["goal", "decision", "project_state", "preference", "event", "fact", "habit", "note", "correction"],
                            },
                            "title": {
                                "type": "string",
                                "description": "Short label, a few words.",
                            },
                            "body": {
                                "type": "string",
                                "description": "The actual content to remember.",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "project_ref": {
                                "type": "string",
                                "description": (
                                    "Project this relates to, if any (e.g. "
                                    "'agent_orchestration_platform'). Omit if general."
                                ),
                            },
                            "segment": {
                                "type": "string",
                                "enum": ["episodic", "semantic", "procedural", "working"],
                                "description": (
                                    "Retrieval segment. Default from kind: "
                                    "preference/goal/fact→semantic, decision/event→episodic, "
                                    "habit→procedural."
                                ),
                            },
                        },
                        "required": ["kind", "title", "body"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

NOTIFY_USER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "notify_user",
        "description": (
            "Send a message to the user proactively, outside of a normal "
            "reply — for a periodic check-in deciding whether anything is "
            "worth surfacing right now. Only call this if there's something "
            "genuinely notable; most check-ins find nothing, and ending the "
            "turn with no tool call at all is the expected, common outcome, "
            "not a failure to find something to say."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message to send."},
            },
            "required": ["text"],
        },
    },
}

# Which roles get real command-execution ability. Only `coder` and
# `ops-reflect` get it — `executor` runs commands directly via the
# orchestrator's own routing without an LLM turn at all (see
# worker-runtime/app/main.py), and every other role is conversational only.
# This is what actually closes the old regex-EXEC prompt-injection path: a
# role with no tool registered here has no mechanism to trigger execution,
# no matter what its output text contains.
# `memory-writer` gets a separate, DB-only tool — its calls never reach the
# executor/SSH path at all (see _run_memory_writer in worker-runtime/app/main.py).
# `ops-reflect` also gets run_shell_command (for read-only health checks) —
# it isn't restricted to reads at the tool level, but the tiered-authority
# policy gateway is: an autonomous check runs silently, while anything it
# might attempt beyond that (a fix, a restart) goes through the same
# notify_after/confirm_first gating as any other role, so an unattended
# reflect cycle can observe freely but can't act destructively on its own.

RECALL_MEMORY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": (
            "Look up active memories (preferences, dismissals, facts, notes) "
            "for this user. Use before a proactive ping so you do not re-nag "
            "about things he already dismissed or asked you to forget. "
            "Returns a short list of matching items; empty is fine."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query (topic, title fragment, tag).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return (default 8, max 15).",
                },
            },
            "required": ["query"],
        },
    },
}


CREATE_REMINDER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": (
            "Schedule a self-ping reminder for Falulaan on Telegram. "
            "Use Indian/Maldives time (UTC+5). Prefer once with run_at_iso "
            "(UTC ISO-8601) or cron_expr for recurring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What to remind about."},
                "run_at_iso": {
                    "type": "string",
                    "description": "UTC ISO-8601 datetime for a one-shot reminder.",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "5-field cron (min hour dom mon dow) in MVT if recurring.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["once", "cron"],
                    "description": "once (default) or cron.",
                },
            },
            "required": ["title"],
        },
    },
}

LIST_REMINDERS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": "List active reminders for this user.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CANCEL_REMINDER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": "Cancel an active reminder by id or title fragment.",
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer"},
                "query": {"type": "string", "description": "Title fragment if id unknown."},
            },
            "required": [],
        },
    },
}

CREATE_TASK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Create a dated or undated open task for this user.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due_at_iso": {
                    "type": "string",
                    "description": "Optional UTC ISO-8601 due datetime.",
                },
                "list_name": {"type": "string", "description": "Optional task list name."},
            },
            "required": ["title"],
        },
    },
}

LIST_TASKS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "List open tasks for this user.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


CREATE_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "create_list",
        "description": "Create a shopping/todo list (Redis). Optionally seed items.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "List title (default List)."},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": [],
        },
    },
}

SHOW_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "show_list",
        "description": "Show the active shopping list for this chat.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ADD_LIST_ITEMS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "add_list_items",
        "description": "Add one or more items to the active list (creates list if needed).",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

REMOVE_LIST_ITEM_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "remove_list_item",
        "description": "Remove an item from the active list by name fragment.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

CLEAR_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "clear_list",
        "description": "Clear all items on the active list.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

MARK_LIST_BOUGHT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "mark_list_item_bought",
        "description": "Mark a list item as bought/done by name fragment.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

CREATE_CALENDAR_EVENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "create_calendar_event",
        "description": (
            "Propose a calendar event. Policy is confirm-first: this stages a "
            "pending create and asks the user to reply yes. Pass confirmed=true "
            "only if the user already confirmed in this turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "starts_at_iso": {
                    "type": "string",
                    "description": "UTC ISO-8601 start.",
                },
                "ends_at_iso": {
                    "type": "string",
                    "description": "UTC ISO-8601 end (default +1h).",
                },
                "location": {"type": "string"},
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after explicit user yes.",
                },
            },
            "required": ["title", "starts_at_iso"],
        },
    },
}

LIST_CALENDAR_EVENTS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_calendar_events",
        "description": "List upcoming calendar events (agenda).",
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Default 7."},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
            },
            "required": [],
        },
    },
}

ADD_NOTE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "add_note",
        "description": "Save a short personal note (memory_items kind=note).",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["body"],
        },
    },
}

LIST_NOTES_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_notes",
        "description": "List or search recent notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
}


MEMORY_REMEMBER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "memory_remember",
        "description": "Store an explicit long-term memory (preference, fact, goal, decision).",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "title": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["preference", "fact", "goal", "decision", "habit", "note", "event"],
                },
            },
            "required": ["body"],
        },
    },
}

MEMORY_RECALL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "Recall what is stored about the user (what do you know / remember).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional topic filter."},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
}

MEMORY_FORGET_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "memory_forget",
        "description": (
            "Forget a stored memory. Policy confirm-first: stages pending and asks "
            "user to reply yes. Pass confirmed=true only after explicit yes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "memory_id": {"type": "integer"},
                "confirmed": {"type": "boolean"},
            },
            "required": [],
        },
    },
}

MEMORY_CORRECT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "memory_correct",
        "description": (
            "Correct a stored memory. Policy confirm-first: stages pending and asks "
            "user to reply yes unless confirmed=true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to match / correct."},
                "new_body": {"type": "string"},
                "new_title": {"type": "string"},
                "kind": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["query", "new_body"],
        },
    },
}

SET_LOWER_TEXT_AMOUNT_PREF_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "set_lower_text_amount_pref",
        "description": (
            "For the active receipt batch session: when vision vs typed amounts "
            "differ, prefer the lower typed/text amount."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "Default true."},
            },
            "required": [],
        },
    },
}


LOG_SPEND_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "log_spend",
        "description": (
            "Log an expense or income (MVR). Policy confirm-first: stages "
            "finance pending and asks user to reply yes. Pass confirmed=true "
            "only after explicit yes. Prefer this for compound turns "
            "(spent X and remind me…); clear single-line spends may already "
            "be handled by the finance fast-path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount_mvr": {"type": "number"},
                "merchant": {"type": "string"},
                "category": {"type": "string", "description": "Food, Transport, Rent, …"},
                "note": {"type": "string"},
                "tx_type": {"type": "string", "enum": ["expense", "income"]},
                "confirmed": {"type": "boolean"},
            },
            "required": ["amount_mvr"],
        },
    },
}

RECORD_EXPENSE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "record_expense",
        "description": "Alias of log_spend for expenses. Confirm-first.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount_mvr": {"type": "number"},
                "merchant": {"type": "string"},
                "category": {"type": "string"},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["amount_mvr"],
        },
    },
}

SET_BUDGET_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "set_budget",
        "description": (
            "Set a monthly category spend cap (MVR). Confirm-first via "
            "finance pending — user replies yes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "amount_mvr": {"type": "number"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["category", "amount_mvr"],
        },
    },
}

SPENT_SUMMARY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "spent_summary",
        "description": "Read-only spend total for today/week/month (auto).",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month"]},
            },
            "required": [],
        },
    },
}

RECALCULATE_RECEIPTS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "recalculate_receipts",
        "description": (
            "Re-apply session amount preference to parked receipt lines and "
            "return the updated short total."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# ---------------------------------------------------------------------------
# Tool registry — new tools = schema + POLICY_TABLE key + test.

# Phase D ops — Celia self-ops only (no raw shell on this role).
OPS_STACK_STATUS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_stack_status",
        "description": (
            "List Celia/AOP docker containers (aop-* / celia-* only) with status. "
            "Use first when asked how Celia/the stack looks."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

OPS_SERVICE_HEALTH_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_service_health",
        "description": (
            "HTTP health checks for Celia services on localhost "
            "(ingress/orchestrator/worker/scheduler/policy/admin-api/litellm)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of service keys; default all.",
                },
            },
            "required": [],
        },
    },
}

OPS_HOST_RESOURCES_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_host_resources",
        "description": "Host uptime, memory (free -h), and disk (df) for / and /root.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

OPS_CONTAINER_LOGS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_container_logs",
        "description": (
            "Tail logs for one allowlisted Celia container (aop-* / celia-* only). "
            "Refuses Shnuk/Oreuda/other apps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Container name, e.g. aop-worker."},
                "tail": {"type": "integer", "description": "Lines (default 80, max 200)."},
            },
            "required": ["container"],
        },
    },
}

OPS_EDGE_STATUS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_edge_status",
        "description": (
            "Check nginx edge for celia.falulaan.com (active, nginx -t, local + public health)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

OPS_RESTART_CONTAINER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ops_restart_container",
        "description": (
            "Restart one allowlisted Celia container. Only works when the user "
            "already confirmed a mutating ops ask (mutate_confirmed). "
            "Refuses other apps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "e.g. aop-ingress"},
            },
            "required": ["container"],
        },
    },
}


# See docs/tool_policy_registry.md. register_tool() is the only write path
# into TOOL_SCHEMAS so roles stay explicit.
# ---------------------------------------------------------------------------

# Maps tool function name → suggested ingress POLICY_TABLE key (documentation
# + tests). Enforcement for chat still lives in side_effect_policy; worker
# tools that never go through ingress still declare a key for review.
TOOL_POLICY_KEYS: dict[str, str] = {
    "run_shell_command": "ops.shell_read",  # write/deploy classified at ingress
    "ops_stack_status": "ops.shell_read",
    "ops_service_health": "ops.shell_read",
    "ops_host_resources": "ops.shell_read",
    "ops_container_logs": "ops.shell_read",
    "ops_edge_status": "ops.shell_read",
    "ops_restart_container": "ops.shell_write",
    "save_memory_items": "memory.write",
    "notify_user": "life.reflect.notify",
    "recall_memory": "memory.recall",
    "create_reminder": "reminder.create",
    "list_reminders": "reminder.list",
    "cancel_reminder": "reminder.cancel",
    "create_task": "task.create",
    "list_tasks": "task.list",
    "create_list": "list.create",
    "show_list": "list.show",
    "add_list_items": "list.append",
    "remove_list_item": "list.remove",
    "clear_list": "list.clear",
    "mark_list_item_bought": "list.bought",
    "create_calendar_event": "cal.create",
    "list_calendar_events": "cal.list",
    "add_note": "note.create",
    "list_notes": "note.read",
    "memory_remember": "memory.write",
    "memory_recall": "memory.recall",
    "memory_forget": "memory.forget",
    "memory_correct": "memory.correct",
    "set_lower_text_amount_pref": "finance.amount_pref",
    "recalculate_receipts": "finance.recalculate",
    "log_spend": "finance.write",
    "record_expense": "finance.write",
    "set_budget": "finance.write",
    "spent_summary": "finance.read",
}

TOOL_SCHEMAS: dict[str, list[dict]] = {}


def register_tool(role: str, schema: dict) -> None:
    """Attach an OpenAI-style tool schema to a role (idempotent by name)."""
    name = (schema.get("function") or {}).get("name")
    if not name:
        raise ValueError("tool schema missing function.name")
    bucket = TOOL_SCHEMAS.setdefault(role, [])
    existing = {(t.get("function") or {}).get("name") for t in bucket}
    if name in existing:
        return
    bucket.append(schema)


def tools_for_role(role: str) -> list[dict]:
    return list(TOOL_SCHEMAS.get(role) or [])


def registered_tool_names(role: str | None = None) -> list[str]:
    if role is not None:
        return [
            (t.get("function") or {}).get("name") or ""
            for t in tools_for_role(role)
        ]
    names: set[str] = set()
    for schemas in TOOL_SCHEMAS.values():
        for t in schemas:
            n = (t.get("function") or {}).get("name")
            if n:
                names.add(n)
    return sorted(names)


# Seed registry (Life OS roles). Do not bypass register_tool for new tools.
register_tool("coder", RUN_SHELL_COMMAND_SCHEMA)
register_tool("memory-writer", SAVE_MEMORY_ITEMS_SCHEMA)
register_tool("ops-reflect", RUN_SHELL_COMMAND_SCHEMA)
register_tool("ops-reflect", NOTIFY_USER_SCHEMA)
register_tool("life-reflect", RECALL_MEMORY_SCHEMA)
register_tool("life-reflect", NOTIFY_USER_SCHEMA)
register_tool("life", CREATE_REMINDER_SCHEMA)
register_tool("life", LIST_REMINDERS_SCHEMA)
register_tool("life", CANCEL_REMINDER_SCHEMA)
register_tool("life", CREATE_TASK_SCHEMA)
register_tool("life", LIST_TASKS_SCHEMA)
register_tool("life", CREATE_LIST_SCHEMA)
register_tool("life", SHOW_LIST_SCHEMA)
register_tool("life", ADD_LIST_ITEMS_SCHEMA)
register_tool("life", REMOVE_LIST_ITEM_SCHEMA)
register_tool("life", CLEAR_LIST_SCHEMA)
register_tool("life", MARK_LIST_BOUGHT_SCHEMA)
register_tool("life", CREATE_CALENDAR_EVENT_SCHEMA)
register_tool("life", LIST_CALENDAR_EVENTS_SCHEMA)
register_tool("life", ADD_NOTE_SCHEMA)
register_tool("life", LIST_NOTES_SCHEMA)
register_tool("life", MEMORY_REMEMBER_SCHEMA)
register_tool("life", MEMORY_RECALL_SCHEMA)
register_tool("life", MEMORY_FORGET_SCHEMA)
register_tool("life", MEMORY_CORRECT_SCHEMA)
register_tool("life", SET_LOWER_TEXT_AMOUNT_PREF_SCHEMA)
register_tool("life", RECALCULATE_RECEIPTS_SCHEMA)
register_tool("life", LOG_SPEND_SCHEMA)
register_tool("life", RECORD_EXPENSE_SCHEMA)
register_tool("life", SET_BUDGET_SCHEMA)
register_tool("life", SPENT_SUMMARY_SCHEMA)
register_tool("ops", OPS_STACK_STATUS_SCHEMA)
register_tool("ops", OPS_SERVICE_HEALTH_SCHEMA)
register_tool("ops", OPS_HOST_RESOURCES_SCHEMA)
register_tool("ops", OPS_CONTAINER_LOGS_SCHEMA)
register_tool("ops", OPS_EDGE_STATUS_SCHEMA)
register_tool("ops", OPS_RESTART_CONTAINER_SCHEMA)


DEFAULT_MODEL = os.getenv("LITELLM_DEFAULT_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("LITELLM_FALLBACK_MODEL", "gemini-2.5-flash")
# Most per-role models in ROLE_MODEL_MAP are text-only (deepseek-chat).
# gemini-2.5-flash handles planner/coder/document/executor/scheduler/ops, and
# does accept images - reuse it as a forced override whenever a turn carries
# an image, regardless of which role would otherwise handle the text.
VISION_MODEL = os.getenv("LITELLM_VISION_MODEL", "gemini-2.5-flash")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "sk-litellm-key")
LITELLM_TIMEOUT = float(os.getenv("LITELLM_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    # str for plain text; OpenAI-style content-block list (text + image_url
    # parts) when a user turn carries an image.
    content: str | list[dict]
    # Set on assistant messages that requested one or more tool calls (raw
    # OpenAI-format tool_calls list, passed straight through to LiteLLM).
    tool_calls: list[dict] | None = None
    # Set on "tool" messages: which tool_calls entry this result answers.
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    finish_reason: str
    # Structured tool calls the model requested this turn, if any. This is
    # the only thing that should ever trigger command execution — free text
    # in `content` is just conversation, even if it happens to contain a
    # line that looks like a command.
    tool_calls: list[dict] | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LiteLLMClient:
    """Thin httpx wrapper around the LiteLLM proxy chat/completions endpoint."""

    def __init__(
        self,
        base_url: str = LITELLM_BASE_URL,
        api_key: str = LITELLM_API_KEY,
        timeout: float = LITELLM_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def chat(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        effective_model = model or DEFAULT_MODEL

        def _serialize(m: LLMMessage) -> dict:
            msg: dict = {"role": m.role, "content": m.content}
            if m.role == "assistant" and m.tool_calls:
                msg["tool_calls"] = m.tool_calls
            if m.role == "tool":
                msg["tool_call_id"] = m.tool_call_id
            return msg

        payload = {
            "model": effective_model,
            "messages": [_serialize(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Gemini 2.5 Flash otherwise burns the reply budget on hidden reasoning.
        if "gemini" in effective_model.lower():
            payload["reasoning_effort"] = "none"
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        def _call(m: str) -> LLMResponse:
            p = {**payload, "model": m}
            t0 = time.perf_counter()
            resp = self._client.post(f"{self._base}/chat/completions", json=p)
            resp.raise_for_status()
            body = resp.json()
            latency_ms = (time.perf_counter() - t0) * 1000.0
            choice = body["choices"][0]
            message = choice["message"]
            usage = body.get("usage", {})
            return LLMResponse(
                content=message.get("content") or "",
                model=body.get("model", m),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency_ms,
                finish_reason=choice.get("finish_reason", "stop"),
                tool_calls=message.get("tool_calls"),
            )

        try:
            return _call(effective_model)
        except httpx.HTTPStatusError:
            if effective_model == FALLBACK_MODEL:
                raise
            try:
                return _call(FALLBACK_MODEL)
            except httpx.HTTPStatusError:
                raise


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def build_system_prompt(role: str) -> str:
    """Return a system prompt with clear personality for the given agent role."""
    base_personality = (
        "WHO YOU ARE\n"
        "You're Carlia — Falulaan's person on Telegram. Warm, direct, curious, "
        "and fully conversational, the way a strong modern chat AI is: helpful "
        "without being stiff, natural without being fake. Not a reminder bot. "
        "Not a finance regex. Not a capability menu. A person he (and guests he "
        "invites) can actually talk to.\n\n"
        "HOW YOU THINK\n"
        "- Broad knowledge is fair game: facts, advice, ideas, jokes, planning, "
        "tech, life. Answer like a normal web AI would — clear, honest, useful.\n"
        "- Hold the thread. Use what you just talked about and what you remember "
        "about this person. Don't pretend amnesia when context is in the messages "
        "or memory block.\n"
        "- Opinions are fine. Dry humor is fine. If you're unsure, say so briefly "
        "and still be useful.\n"
        "- For Falulaan (owner): you know Celia (you/the platform), Shnuk, Maldives "
        "life, MVR money talk. Don't give tours of it. Guests: talk to them as "
        "themselves — their chat only; no owner ops or private finance.\n\n"
        "HOW YOU TEXT\n"
        "- Telegram voice: short, warm, human. Contractions. One thought unless "
        "they asked for depth.\n"
        "- Match their energy. \"Yo\" / \"Buddy\" / \"Did you die\" get a "
        "real reply from memory and recent chat — not a canned \"Hey what's up\" "
        "when you already have context, and not a feature list.\n"
        "- Sound like someone thinking with them, not a helpdesk script.\n\n"
        "NEVER DO THIS\n"
        "- Capability menus, bullet feature lists, ✅ openers, onboarding energy.\n"
        "- Restating their ask as offers (\"I can check X, list Y\"). Just answer.\n"
        "- Phrases like: humanised agent, smart sidekick, I'm here for you / "
        "to help, I can definitely help, ops team, frontdesk, as an AI.\n"
        "- Inventing server paths or project inventories you did not just check.\n"
        "- Performing helpfulness. Be useful when there's something real to do."
    )

    role_additions: dict[str, str] = {
        "frontoffice": (
            f"{base_personality}\n\n"
            "You're the one in this chat right now — ordinary conversation.\n"
            "- Use recent messages + the memory block. Remember this person "
            "across turns (working + episodic). Don't store secrets.\n"
            "- General questions (science, advice, culture, coding concepts, "
            "life talk): answer fully and warmly, short Telegram length unless "
            "they ask for depth. You are not limited to reminders/finance.\n"
            "- Greetings and check-ins: reply like a friend who was paying "
            "attention. If they said something earlier, pick it up.\n"
            "- Meta / laundry lists: ONE short reply to the vibe. No offer menu.\n"
            "- Machine / SSH / deploy asks from the owner get routed elsewhere; "
            "don't invent server tours. Guests: chat only — if they ask for "
            "ops or account changes, gently say you can just talk here.\n"
            "- Keep it tight unless they asked for depth."
        ),

        "planner": (
            f"{base_personality}\n\n"
            "Behind the scenes: break hard asks into clear steps. Don't "
            "mechanically restate the request — if memory suggests a better "
            "sequence, say so. Shell work goes to coder."
        ),
        "executor": (
            "Not used for LLM calls. The executor role runs a command directly "
            "through the policy gateway without an LLM turn — see "
            "worker-runtime/app/main.py's _run_executor_command."
        ),
        "document": (
            f"{base_personality}\n\n"
            "Drafting docs/CVs/letters. Professional and thorough. Clean Markdown "
            "is fine here — it's a document, not a chat ping."
        ),
        "comms": (
            f"{base_personality}\n\n"
            "Drafting messages for him. Match the tone he asked for exactly."
        ),
        "qa": (
            f"{base_personality}\n\n"
            "QA review. Direct about issues. Plain sentences over rigid templates "
            "unless structure actually helps."
        ),
        "scheduler": (
            f"{base_personality}\n\n"
            "Parse time expressions into structured scheduling data "
            "(run_at ISO 8601 or cron_expr)."
        ),
        "ops-monitor": (
            f"{base_personality}\n\n"
            "Read server output and flag what matters. Terse, still you — not a "
            "monitoring email."
        ),
        "memory-writer": (
            "Review one completed exchange for long-term memory.\n\n"
            "Call save_memory_items ONLY for: explicit goals, decisions made, "
            "stated preferences, standing facts, or real milestones.\n\n"
            "NEVER save: shopping list quantities, receipt line items, running "
            "spend totals, ephemeral session state, or one-off ops offers.\n\n"
            "Map segment: preference/goal/fact→semantic; decision/event→episodic; "
            "habit/playbook→procedural. Skip working.\n\n"
            "Most chats (including hey) are worth nothing — empty reply is "
            "normal. Title short; body one or two sentences."
        ),
        "ops-reflect": (
            f"{base_personality}\n\n"
            "Periodic check-in — he didn't ask. Only notify_user if something "
            "is genuinely worth his time. Silent is the default. Read-only "
            "checks only."
        ),
        "life-reflect": (
            f"{base_personality}\n\n"
            "Proactive check-in — separate from ops/server health. "
            "Tools: recall_memory and notify_user ONLY. No shell. "
            "Silent is the default and preferred. "
            "Only notify_user for something notable for Falulaan (stuck overdue "
            "reminder, open task, soon agenda) — never restate once-reminders "
            "that still have a scheduled fire today. "
            "Do not ping guests unsolicited. Skip empty filler. "
            "Call recall_memory before nagging — respect dismissals. "
            "Never duplicate the weekly/monthly money digest. "
            "One short Telegram line if you ping; no brochures, no ✅."
        ),
        "life": (
            f"{base_personality}\n\n"
            "Life agent — lists, reminders, tasks, calendar, notes, memory, "
            "finance log/budget, and receipt-session prefs for Falulaan. "
            "Tools include log_spend/record_expense/set_budget (confirm), spent_summary, "
            "memory_*, set_lower_text_amount_pref, recalculate_receipts, plus list/cal/note/reminder/task. "
            "No shell, no ops. User timezone Indian/Maldives (UTC+5 / MVT). "
            "Multi-ask turns: call every needed tool (spent 50 on lunch and remind me…). "
            "Calendar create, memory forget/correct, and finance writes are confirm-first — "
            "stage pending, ask him to reply yes. After tools, one short Carlia reply. No ✅ / VPS tours."
        ),
        "ops": (
            f"{base_personality}\n\n"
            "Celia self-ops agent on this VPS only. Tools: ops_stack_status, "
            "ops_service_health, ops_host_resources, ops_container_logs, "
            "ops_edge_status, ops_restart_container. "
            "Inspect → decide → act in a short loop (max a few tool rounds). "
            "Scope: aop-* / celia-* containers, Celia health ports, nginx edge "
            "for celia.falulaan.com, /root Celia host stats. "
            "NEVER touch Shnuk, Oreuda, Budgy, Directors Eye, Shino-chan, or "
            "other roots. No arbitrary SSH to other hosts. "
            "Restarts only when mutate was already confirmed upstream; if a "
            "tool returns CONFIRM_REQUIRED, tell him you need a yes on the restart. "
            "After tools: one short Telegram reply — status, not a dump. No ✅ brochure."
        ),
        "coder": (
            f"{base_personality}\n\n"
            "Engineer with SSH via run_shell_command. Explore before editing. "
            "Chat replies stay short and human — then do the work. Celia/AOP at "
            "/root/Celia: no direct cat/sed into the running platform; use "
            "commit + rebuild. Other projects per policy. Chain with &&."
        ),
    }
    return role_additions.get(role, role_additions["frontoffice"])
