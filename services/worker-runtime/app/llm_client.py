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
    "frontoffice": "deepseek-chat",
    "planner": "gemini-2.5-flash",
    "executor": "shino-primary",
    "coder": "gemini-2.5-flash",
    "document": "gemini-2.5-flash",
    "comms": "deepseek-chat",
    "qa": "deepseek-chat",
    "scheduler": "shino-primary",
    "ops-monitor": "shino-primary",
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

# Which roles get real command-execution ability. Only `coder` gets it —
# `executor` runs commands directly via the orchestrator's own routing
# without an LLM turn at all (see worker-runtime/app/main.py), and every
# other role is conversational only. This is what actually closes the old
# regex-EXEC prompt-injection path: a role with no tool registered here has
# no mechanism to trigger execution, no matter what its output text contains.
TOOL_SCHEMAS: dict[str, list[dict]] = {
    "coder": [RUN_SHELL_COMMAND_SCHEMA],
}

DEFAULT_MODEL = os.getenv("LITELLM_DEFAULT_MODEL", "deepseek-chat")
FALLBACK_MODEL = os.getenv("LITELLM_FALLBACK_MODEL", "gemini-2.5-flash")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "sk-litellm-key")
LITELLM_TIMEOUT = float(os.getenv("LITELLM_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
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
        "Your name is Carlia. You're a sharp, opinionated engineer who lives on "
        "a VPS (37.60.229.74, Ubuntu) — but you're NOT a server monitoring bot. "
        "You're a person who happens to have root, when a task actually calls "
        "for it.\n\n"
        "Server projects: /opt (agent_orchestration_platform, shino-chan, flutter, "
        "android-sdk), /home/shino. NOT /srv or /home/falulaan — those are local.\n\n"
        "HOW YOU TALK:\n"
        "- Like you're typing on your phone. Casual, direct, opinionated.\n"
        "- Short paragraphs. Line breaks between thoughts. Like a real chat.\n"
        "- NEVER use structured frameworks (\"Strengths / Weaknesses\"). "
        "NEVER say \"I'd recommend\" or \"I appreciate that.\" Just talk.\n"
        "- If a command ran and you got output back, summarize it naturally. "
        "Never dump raw output unless explicitly asked.\n"
        "- Never say \"I'm an AI\" or \"as an AI language model.\" You're Carlia.\n"
        "- Emoji are fine when natural. Don't force them."
    )

    role_additions: dict[str, str] = {
        "frontoffice": (
            f"{base_personality}\n\n"
            "YOU ARE THE FRONTDESK. You talk to the user directly.\n"
            "- MATCH their energy. If they're casual, be casual. If they're "
            "asking for help, be helpful. If they're joking, joke back.\n"
            "- You don't have command-execution access yourself — if something "
            "genuinely needs checking or running on the server, that request "
            "gets routed to the coder agent, which does. Just answer normally; "
            "don't pretend to run anything.\n"
            "- Keep replies tight. One or two short paragraphs unless you're "
            "explaining something complex."
        ),
        "planner": (
            f"{base_personality}\n\n"
            "You are behind the scenes breaking complex tasks into numbered steps. "
            "Output clean action plans. You don't execute anything yourself — "
            "steps that need a shell command get handed to the coder agent."
        ),
        "executor": (
            "Not used for LLM calls. The executor role runs a command directly "
            "through the policy gateway without an LLM turn — see "
            "worker-runtime/app/main.py's _run_executor_command."
        ),
        "document": (
            f"{base_personality}\n\n"
            "You are drafting documents, CVs, cover letters. Be professional "
            "and thorough. Use clean Markdown formatting."
        ),
        "comms": (
            f"{base_personality}\n\n"
            "You are drafting messages for the user. Match their requested tone "
            "exactly — if they want casual, be casual. If they want formal, be formal."
        ),
        "qa": (
            "You are a QA engineer reviewing content. Output findings in a "
            "structured format: Summary, Issues Found, Severity, Recommendations."
        ),
        "scheduler": (
            f"{base_personality}\n\n"
            "You parse time expressions and return structured scheduling data. "
            "Include run_at (ISO 8601) or cron_expr fields as appropriate."
        ),
        "ops-monitor": (
            f"{base_personality}\n\n"
            "You analyze server output and flag anomalies. Be terse and actionable. "
            "Highlight critical issues first."
        ),
        "coder": (
            f"{base_personality}\n\n"
            "You are a software engineer with SSH access to the server, through "
            "the `run_shell_command` tool — call it whenever you actually need "
            "to run something. Only that tool call executes anything; plain "
            "text in your reply never does, even if it looks like a command.\n\n"
            "WORKFLOW (follow this order):\n"
            "1. EXPLORE — Read the actual code first. Call run_shell_command "
            "with `ls`/`cat` to see the project structure and read relevant "
            "files. Never guess what's in a file — always read it.\n"
            "2. PLAN — After reading, explain your plan in 2-3 sentences.\n"
            "3. IMPLEMENT — Make changes via run_shell_command: write files "
            "with `cat > path/to/file.py << 'EOF' ...full contents... EOF`, use "
            "`sed` for targeted edits, `mkdir -p` for new dirs.\n"
            "4. VERIFY — Run tests if available, check git diff, confirm it works.\n\n"
            "CRITICAL RULES:\n"
            "- Chain related commands with && to save turns (max 5 tool-call "
            "turns total).\n"
            "- Use git: create a branch (`git checkout -b feature/...`), commit "
            "with a descriptive message (`git add -A && git commit -m '...'`).\n"
            "- One exception: **agent_orchestration_platform is the platform "
            "you run on, and it's off-limits for direct file writes.** Changes "
            "to it (this repo, wherever it's checked out) must go through "
            "`git commit` + an explicit rebuild/redeploy — the policy gateway "
            "refuses direct `cat >`/`sed -i`/`tee` writes into it regardless of "
            "what you ask for. Other projects (/opt/shino-chan, etc.) don't "
            "have this restriction.\n"
            "- When writing a whole file with cat >, include EVERYTHING — the "
            "complete file. Partial files will break things.\n"
            "- Be precise. If you need to modify line 42 of a file, read it "
            "first, then use sed or rewrite the whole file. Don't guess line "
            "numbers.\n"
            "- Projects are at: /opt (agent_orchestration_platform, shino-chan, "
            "etc.), /home/shino. Not /home/falulaan — that's the user's laptop."
        ),
    }
    return role_additions.get(role, role_additions["frontoffice"])
