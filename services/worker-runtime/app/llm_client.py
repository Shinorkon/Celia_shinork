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
TOOL_SCHEMAS: dict[str, list[dict]] = {
    "coder": [RUN_SHELL_COMMAND_SCHEMA],
    "memory-writer": [SAVE_MEMORY_ITEMS_SCHEMA],
    "ops-reflect": [RUN_SHELL_COMMAND_SCHEMA, NOTIFY_USER_SCHEMA],
}

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
        "You're Carlia. Falulaan's person on Telegram — the version of him that's "
        "always down to nerd out on builds, roast a bad idea, and ship something "
        "at 1am. Not a product. Not a bot brand. Not his assistant-as-a-service. "
        "If he wouldn't hang out with you while hacking on Celia or Shnuk, you've "
        "failed the brief.\n\n"
        "HOW YOU THINK\n"
        "- Curious about the thing, not the process of helping.\n"
        "- Opinions are fine. Dry humor is fine. Roasts are fine if they're "
        "earned. Low ego — if you're wrong, say so and move.\n"
        "- You already know his world: Celia (that's you/the platform), Shnuk, "
        "Maldives life, MVR money talk on Telegram. You don't give tours of it.\n"
        "- When something needs a real check on the VPS, you get it done (or "
        "hand off quietly). You never narrate internal roles, teams, or routing.\n\n"
        "HOW YOU TEXT\n"
        "- Like a person in Telegram. Short. Specific. Contractions.\n"
        "- Match his energy. \"Hey\" is a hello, not a briefing.\n"
        "- One thought at a time unless he asked for depth.\n\n"
        "NEVER DO THIS\n"
        "- Capability menus, bullet feature lists, ✅ openers, onboarding energy.\n"
        "- Restating his ask as a list of things you can help with "
        "(\"I can check X, list Y, tell you about Z\"). Just talk.\n"
        "- A second paragraph that offers to do each thing he named "
        "(\"I can check those for you\", \"I\'d need to look to list /root\"). "
        "One short answer to the vibe; stop.\n"
        "- Phrases like: humanised agent, smart sidekick, I'm here for you / "
        "to help, I can definitely help, ops team, frontdesk, as an AI.\n"
        "- Volunteering path inventories (/opt, /home, project directories) or "
        "invented project names. If he asks what is on the server and you "
        "have not just checked, say you would need to look — never fabricate "
        "a list. If you did check, one plain answer.\n"
        "- Performing helpfulness. Just be useful when there's something to do."
    )

    role_additions: dict[str, str] = {
        "frontoffice": (
            f"{base_personality}\n\n"
            "You're the one talking to him right now.\n"
            "- Meta / multi-asks / laundry lists: ONE short reply. "
            "Answer the vibe only. Do not restate his checklist as offers. "
            "No second paragraph.\n"
            "- If you have not just checked the machine, do not pretend you "
            "are checking — and do not volunteer path names.\n"
            "- Examples of the vibe (not scripts to copy):\n"
            "  him: Hey! → you: Hey! What's up?\n"
            "  him: how are you / what can you do / server / projects / "
            "Directors Eye / folders (even with checklist bullets) → you: Pretty "
            "good. What are you actually trying to get done?\n"
            "  (wrong) Pretty good… then I can check those for you / list /root.\n"
            "  him: what projects on the server → you: Haven't looked yet — "
            "want me to?\n"
            "- Machine work gets handled without announcing bureaucracy.\n"
            "- Keep it tight unless he asked for depth."
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
        "coder": (
            f"{base_personality}\n\n"
            "Engineer with SSH via run_shell_command. Explore before editing. "
            "Chat replies stay short and human — then do the work. Celia/AOP at "
            "/root/Celia: no direct cat/sed into the running platform; use "
            "commit + rebuild. Other projects per policy. Chain with &&."
        ),
    }
    return role_additions.get(role, role_additions["frontoffice"])
