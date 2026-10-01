"""Owner Telegram relay + voice: no capability-menu refusal, guests stay locked."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.intent_router import classify_intent, looks_like_message_relay  # noqa: E402
from app.quiet_mode import quiet_strip_chat  # noqa: E402
from app.side_effect_policy import classify_action, policy_for  # noqa: E402
from packages.telegram_relay import (  # noqa: E402
    clear_pending,
    load_pending,
    parse_known_contacts,
    plan_relay,
    resolve_recipient,
    save_pending,
)
from packages.voice_guard import guard_reply, is_capability_refusal  # noqa: E402

SCREENSHOT_REFUSAL = (
    "Hey there! My capabilities are more on the conversational side – like "
    "discussing ideas, giving advice, or just chatting. Are you seeing any "
    "specific issues? We can definitely talk through ideas or challenges "
    "you're facing with it!"
)
RELAY_ASK = "Will you pass on a message to Raaish?"


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_relay_voice"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    if "httpx" not in sys.modules:
        sys.modules["httpx"] = types.ModuleType("httpx")
        sys.modules["httpx"].Client = object  # type: ignore
        sys.modules["httpx"].Timeout = object  # type: ignore
        sys.modules["httpx"].HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _MemRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key, ttl, val):
        self.store[key] = val

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


class PromptVoiceTests(unittest.TestCase):
    def test_owner_prompts_are_agentic_not_cramped(self):
        llm = _load_llm_client()
        for role in ("frontoffice", "life", "comms"):
            prompt = llm.build_system_prompt(role, audience="owner").lower()
            self.assertNotIn("conversational side", prompt)
            self.assertNotIn("only conversational", prompt)
            self.assertNotIn("capabilities are", prompt)
            self.assertNotIn("chat only", prompt)
            self.assertIn("send_telegram_message", prompt)
            self.assertIn("canned greeting", prompt)
        life = llm.build_system_prompt("life")
        self.assertIn("log_spend", life)
        self.assertIn("reminder", life.lower())
        self.assertIn("calendar", life.lower())
        self.assertIn("memory", life.lower())
        self.assertNotIn("run_shell_command", life)

    def test_guest_prompt_cannot_relay_or_ops(self):
        llm = _load_llm_client()
        prompt = llm.build_system_prompt("frontoffice", audience="guest").lower()
        self.assertNotIn("send_telegram_message", prompt)
        self.assertNotIn("only conversational", prompt)
        self.assertNotIn("conversational side", prompt)
        self.assertIn("own spending", prompt)
        self.assertIn("do not message other people", prompt)
        self.assertIn("draft", prompt)

    def test_guest_does_not_see_relay_tool(self):
        llm = _load_llm_client()
        self.assertIn("send_telegram_message", llm.registered_tool_names("life"))
        self.assertIn("send_telegram_message", llm.registered_tool_names("frontoffice"))
        self.assertEqual(llm.TOOL_POLICY_KEYS["send_telegram_message"], "comms.third_party")
        guest_front = llm.tools_for_role("frontoffice", audience="guest")
        guest_life = llm.tools_for_role("life", audience="guest")
        self.assertEqual(guest_front, [])
        self.assertEqual(guest_life, [])
        guest_reflect = {
            (t.get("function") or {}).get("name")
            for t in llm.tools_for_role("life-reflect", audience="guest")
        }
        self.assertEqual(guest_reflect, {"recall_memory", "notify_user"})
        owner_front = {
            (t.get("function") or {}).get("name")
            for t in llm.tools_for_role("frontoffice", audience="owner")
        }
        self.assertIn("send_telegram_message", owner_front)


class RelayIntentTests(unittest.TestCase):
    def test_pass_message_is_relay_not_chat(self):
        self.assertTrue(looks_like_message_relay(RELAY_ASK))
        self.assertEqual(classify_intent(RELAY_ASK), "relay")
        action, pol = classify_action("relay", RELAY_ASK)
        self.assertEqual(action, "comms.third_party")
        self.assertEqual(pol, "confirm")
        self.assertEqual(policy_for("comms.third_party"), "confirm")

    def test_reply_to_the_other_person_is_relay(self):
        text = "Reply to the guy who's texting u Not me"
        self.assertEqual(classify_intent(text), "relay")

    def test_greeting_and_reminder_unchanged(self):
        self.assertEqual(classify_intent("hey"), "chat")
        self.assertEqual(classify_intent("Hey! What's up?"), "chat")
        self.assertEqual(
            classify_intent("remind me to send a message to Sam tomorrow at 5"),
            "reminder",
        )

    def test_orchestrator_routes_relay_to_life(self):
        src = (ROOT / "services" / "orchestrator" / "app" / "main.py").read_text()
        self.assertIn('return "life", "relay_message"', src)
        ingress = (ROOT / "services" / "telegram-ingress" / "app" / "main.py").read_text()
        self.assertIn('intent == "relay"', ingress)
        self.assertIn('"audience": "guest" if guest else "owner"', ingress)


class RelayPlanTests(unittest.TestCase):
    CONTACTS = {"raaish": 1210484792}

    def test_parse_contacts(self):
        book = parse_known_contacts("Raaish:1210484792, Sam=55555")
        self.assertEqual(book["raaish"], 1210484792)
        self.assertEqual(book["sam"], 55555)

    def test_unknown_name_asks_for_id(self):
        plan = plan_relay(
            audience="owner",
            recipient="Raaish",
            text="You around?",
            contacts={},
        )
        self.assertEqual(plan["status"], "need_recipient")
        msg = plan["tool_message"].lower()
        self.assertIn("need_recipient", msg)
        self.assertNotIn("conversational side", msg)
        self.assertNotIn("only chat", msg)

    def test_known_name_stages_confirm_without_sending(self):
        plan = plan_relay(
            audience="owner",
            recipient="Raaish",
            text="You around?",
            confirmed=True,  # ignored until a matching pending exists
            contacts=self.CONTACTS,
        )
        self.assertEqual(plan["status"], "pending_confirm")
        self.assertEqual(plan["pending"]["chat_id"], "1210484792")
        self.assertNotEqual(plan["status"], "send")

    def test_confirm_sends_only_after_pending_match(self):
        pending = {"chat_id": "1210484792", "label": "Raaish", "text": "You around?"}
        plan = plan_relay(
            audience="owner",
            recipient="Raaish",
            text="You around?",
            confirmed=True,
            pending=pending,
            contacts=self.CONTACTS,
        )
        self.assertEqual(plan["status"], "send")
        self.assertEqual(plan["chat_id"], "1210484792")
        self.assertEqual(plan["text"], "You around?")

    def test_explicit_id_does_not_need_a_contact_book(self):
        resolved = resolve_recipient("1210484792", contacts={})
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["reason"], "explicit_id")
        self.assertEqual(resolved["chat_id"], "1210484792")

    def test_guest_relay_refused_by_tool(self):
        plan = plan_relay(
            audience="guest",
            recipient="1210484792",
            text="hey",
            confirmed=True,
            pending={"chat_id": "1210484792", "label": "1210484792", "text": "hey"},
            contacts=self.CONTACTS,
        )
        self.assertEqual(plan["status"], "refused")
        self.assertNotIn("chat_id", plan)
        self.assertNotIn("conversational", plan["tool_message"].lower())

    def test_pending_roundtrip_does_not_touch_users(self):
        src = (ROOT / "packages" / "telegram_relay.py").read_text()
        self.assertNotIn("INSERT INTO users", src)
        self.assertNotIn("role =", src)
        redis = _MemRedis()
        save_pending("929388047", {"chat_id": "1210484792", "text": "hi", "label": "Raaish"}, redis)
        self.assertEqual(load_pending("929388047", redis)["chat_id"], "1210484792")
        clear_pending("929388047", redis)
        self.assertIsNone(load_pending("929388047", redis))


class CapabilitySpeechTests(unittest.TestCase):
    def test_screenshot_refusal_is_rewritten(self):
        self.assertTrue(is_capability_refusal(SCREENSHOT_REFUSAL))
        out = guard_reply(SCREENSHOT_REFUSAL, user_text=RELAY_ASK, has_context=True)
        low = out.lower()
        self.assertNotIn("conversational side", low)
        self.assertNotIn("capabilities", low)
        self.assertNotIn("discussing ideas", low)
        self.assertIn("pass that on", low)

    def test_quiet_strip_rewrites_capability_menu(self):
        out = quiet_strip_chat(SCREENSHOT_REFUSAL)
        self.assertNotIn("conversational", out.lower())
        self.assertNotIn("capabilities", out.lower())
        self.assertTrue(out.strip())

    def test_plain_greeting_still_passes_quiet_strip(self):
        self.assertEqual(quiet_strip_chat("Hey! What's up?"), "Hey! What's up?")

    def test_canned_greeting_dropped_when_context_and_real_ask(self):
        out = guard_reply(
            "Hey! What's up?",
            user_text="Reply to the guy who's texting u Not me",
            has_context=True,
        )
        self.assertNotEqual(out, "Hey! What's up?")
        self.assertIn("pass that on", out.lower())

    def test_greeting_kept_when_they_just_said_hey(self):
        self.assertEqual(
            guard_reply("Hey! What's up?", user_text="hey", has_context=True),
            "Hey! What's up?",
        )

    def test_srv_dump_sentence_stripped(self):
        raw = (
            "Hey! What's up? Directors-Eye is chugging along too. "
            "You want a list of projects in `/srv`?"
        )
        out = quiet_strip_chat(raw)
        self.assertNotIn("Directors", out)
        self.assertNotIn("/srv", out)
        self.assertNotIn("projects", out.lower())


if __name__ == "__main__":
    unittest.main()
