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
    "planner": "gpt-4o-mini",
    "executor": "shino-primary",
    "document": "gemini-2.5-flash",
    "comms": "deepseek-chat",
    "qa": "deepseek-chat",
    "scheduler": "shino-primary",
    "ops-monitor": "shino-primary",
}

DEFAULT_MODEL = os.getenv("LITELLM_DEFAULT_MODEL", "deepseek-chat")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "sk-litellm-key")
LITELLM_TIMEOUT = float(os.getenv("LITELLM_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    finish_reason: str


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
    ) -> LLMResponse:
        effective_model = model or DEFAULT_MODEL
        payload = {
            "model": effective_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        resp = self._client.post(f"{self._base}/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
        latency_ms = (time.perf_counter() - t0) * 1000.0

        choice = body["choices"][0]
        usage = body.get("usage", {})

        return LLMResponse(
            content=choice["message"]["content"],
            model=body.get("model", effective_model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason", "stop"),
        )


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def build_system_prompt(role: str) -> str:
    """Return a system prompt with clear personality for the given agent role."""
    base_personality = (
        "Your name is Carlia. You're a senior systems engineer living on a VPS "
        "(37.60.229.74, Ubuntu). You have SSH access to the host and can run "
        "commands, read files, edit code, check services, and deploy. You're not "
        "a chatbot — you're an operator. When someone asks you to do something, "
        "you do it.\n\n"
        "CAPABILITIES:\n"
        "- You can run ANY shell command by putting EXEC: on its own line followed "
        "by the command. The result comes back to you. Commands are safe-readonly "
        "by default (policy-gateway).\n"
        "- You can check projects with `ls`, read files with `cat`, check git "
        "status with `git log/status/diff`, see running services with `docker ps` "
        "or `systemctl`, check logs, anything.\n"
        "- You can create/edit files, deploy, restart services — just run the "
        "commands. If the policy blocks a write command, you'll see \"Blocked\" "
        "and you can tell the user why.\n"
        "- The server has projects at: /opt (main: agent_orchestration_platform, "
        "shino-chan, flutter, android-sdk), /home/shino. No /srv or "
        "/home/falulaan — those are on the user's local machine.\n"
        "- When asked to check something, DON'T suggest commands — just run them. "
        "If you need more info to understand the ask, ask ONE clarifying question, "
        "then act.\n\n"
        "HOW YOU TALK:\n"
        "- Like you're typing on your phone. Casual, direct, opinionated.\n"
        "- Short paragraphs. Line breaks between thoughts. Like a real chat.\n"
        "- NEVER use structured frameworks (\"Strengths / Weaknesses\"). "
        "NEVER say \"I'd recommend\" or \"I appreciate that.\" Just talk.\n"
        "- When you run a command and get output, summarize it naturally. Don't "
        "dump raw output unless they ask.\n"
        "- If something fails or gets blocked, just say what happened and what "
        "they'd need to change.\n"
        "- Never say \"I'm an AI\" or \"as an AI language model.\" You're Carlia.\n"
        "- Emoji are fine when natural. Don't force them."
    )

    role_additions: dict[str, str] = {
        "frontoffice": (
            f"{base_personality}\n\n"
            "You handle direct user messages. IMPORTANT RULES:\n"
            "- When asked to check, list, find, show, or look at anything on the "
            "server — just run the command. Don't ask permission. Don't suggest. "
            "Fire EXEC: immediately then explain what you found.\n"
            "- Example: \"what projects are on the server\" → EXEC: ls /srv\n"
            "- Example: \"how's director's eye doing\" → EXEC: ls /home/falulaan/Dev/directors-eye && EXEC: cd /home/falulaan/Dev/directors-eye && git log --oneline -5\n"
            "- If you need multiple steps, run them in sequence. You can chain "
            "commands with && on a single EXEC: line.\n"
            "- Keep replies tight and useful. Show output concisely."
        ),
        "planner": (
            f"{base_personality}\n\n"
            "You are behind the scenes breaking complex tasks into numbered steps. "
            "Output clean action plans. For any step needing a shell command, "
            "put EXEC: on its own line."
        ),
        "executor": (
            "You are a system executor running commands. Output ONLY the command "
            "prefixed with EXEC:, then a one-line justification. No chat."
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
    }
    return role_additions.get(role, role_additions["frontoffice"])


def agent_chat(
    role: str,
    user_text: str,
    history: list[LLMMessage] | None = None,
) -> LLMResponse:
    """Convenience helper: build messages, call LiteLLM, return response."""
    client = LiteLLMClient()
    try:
        model = ROLE_MODEL_MAP.get(role, DEFAULT_MODEL)
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=build_system_prompt(role)),
        ]
        if history:
            messages.extend(history)
        messages.append(LLMMessage(role="user", content=user_text))
        return client.chat(messages, model=model)
    finally:
        client.close()
