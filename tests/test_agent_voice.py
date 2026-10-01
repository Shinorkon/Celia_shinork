"""Agentic Carlia voice: no capability-refusal scripts, owner relay tool."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))

os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "929388047")
os.environ.setdefault("GUEST_TELEGRAM_USER_IDS", "1210484792")

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.intent_router import classify_intent  # noqa: E402
from app.quiet_mode import quiet_strip_chat  # noqa: E402
from app.side_effect_policy import classify_action, policy_for  # noqa: E402
from packages.reply_guard import guard_cramped_reply, looks_like_message_relay  # noqa: E402

SCREENSHOT_REFUSAL = (
    "Hey there! My capabilities are more on the conversational side – like "
    "discussing ideas, giving advice, or just chatting. Are you seeing any "
    "specific issues? We can definitely talk through ideas or challenges "
    "you're facing with it!"
)


def _load(path: Path, name: str):
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


def _load_llm():
    return _load(ROOT / "services" / "worker-runtime" / "app" / "llm_client.py", "llm_client_agent_voice")


def _load_life():
    return _load(ROOT / "services" / "worker-runtime" / "app" / "life_tools.py", "life_tools_agent_voice")


class PromptVoiceTests(unittest.TestCase):
    def test_owner_prompt_is_agentic_and_bans_cramped_refusal(self):
        llm = _load_llm()
        front = llm.build_system_prompt("frontoffice")
        life = llm.build_system_prompt("life")
        for prompt in (front, life):
            self.assertIn("on the conversational side", prompt)
            self.assertIn("Never answer", prompt)
            self.assertNotIn("My capabilities are more on the conversational side", prompt)
            self.assertIn("relay_telegram_message", prompt)
            self.assertIn("log_spend", prompt)
            low = prompt.lower()
            for word in ("reminders", "tasks", "lists", "calendar", "notes", "memory", "finance"):
                self.assertIn(word, low)
        self.assertNotIn("run_shell_command", life)
        self.assertNotIn("ops_stack_status", front)

    def test_guest_prompt_blocks_ops_and_relay(self):
        llm = _load_llm()
        guest = llm.build_system_prompt("frontoffice", audience="guest")
        self.assertIn("GUEST", guest)
        self.assertIn("do not touch the server", guest.lower())
        self.assertIn("message other people", guest.lower())
        self.assertIn("on the conversational side", guest)


class ToolSurfaceTests(unittest.TestCase):
    def test_frontoffice_has_life_bundle_not_ops(self):
        llm = _load_llm()
        names = set(llm.registered_tool_names("frontoffice"))
        for required in (
            "create_reminder",
            "create_task",
            "create_list",
            "create_calendar_event",
            "add_note",
            "memory_remember",
            "log_spend",
            "relay_telegram_message",
        ):
            self.assertIn(required, names)
        self.assertNotIn("run_shell_command", names)
        self.assertNotIn("ops_stack_status", names)
        self.assertNotIn("ops_restart_container", names)
        self.assertEqual(llm.TOOL_POLICY_KEYS["relay_telegram_message"], "comms.third_party")

    def test_ops_role_unchanged_and_owner_only(self):
        llm = _load_llm()
        ops = set(llm.registered_tool_names("ops"))
        self.assertIn("ops_stack_status", ops)
        self.assertIn("ops_restart_container", ops)
        self.assertNotIn("relay_telegram_message", ops)
        guest_ops = llm.filter_tools_for_audience(llm.tools_for_role("ops"), audience="guest")
        self.assertIsNone(guest_ops)

    def test_guest_tools_are_chat_safe(self):
        llm = _load_llm()
        guest = llm.filter_tools_for_audience(
            llm.tools_for_role("frontoffice"), audience="guest"
        )
        names = {(t.get("function") or {}).get("name") for t in guest}
        self.assertIn("memory_remember", names)
        self.assertIn("log_spend", names)
        self.assertNotIn("relay_telegram_message", names)
        self.assertNotIn("memory_forget", names)
        self.assertNotIn("memory_correct", names)
        self.assertNotIn("clear_list", names)
        owner = llm.filter_tools_for_audience(
            llm.tools_for_role("life"), audience="owner"
        )
        owner_names = {(t.get("function") or {}).get("name") for t in owner}
        self.assertIn("relay_telegram_message", owner_names)

    def test_life_names_include_relay(self):
        lt = _load_life()
        self.assertIn("relay_telegram_message", lt.LIFE_TOOL_NAMES)


class RefusalGuardTests(unittest.TestCase):
    def test_screenshot_refusal_becomes_relay_next_step(self):
        user = "Will you pass on a message to Raaish?"
        out = guard_cramped_reply(user, SCREENSHOT_REFUSAL, has_context=True)
        low = out.lower()
        self.assertNotIn("conversational side", low)
        self.assertNotIn("my capabilities", low)
        self.assertNotIn("only conversational", low)
        self.assertIn("telegram id", low)
        self.assertTrue(looks_like_message_relay(user))

    def test_canned_greeting_with_context_does_not_ship(self):
        user = "Reply to the guy who's texting u Not me"
        out = guard_cramped_reply(user, "Hey! What's up?", has_context=True)
        self.assertNotIn("what's up", out.lower())
        self.assertIn("telegram id", out.lower())

    def test_infra_tour_with_canned_greeting_is_replaced(self):
        user = "Why no replies to him?"
        raw = (
            "Hey! What's up? Directors-Eye is chugging along too. "
            "You want a list of projects in `/srv`?"
        )
        out = guard_cramped_reply(user, raw, has_context=True)
        low = out.lower()
        self.assertNotIn("directors", low)
        self.assertNotIn("/srv", low)
        self.assertNotIn("what's up", low)
        self.assertIn("telegram id", low)

    def test_first_hello_kept(self):
        out = guard_cramped_reply("hey", "Hey! What's up?", has_context=False)
        self.assertEqual(out, "Hey! What's up?")

    def test_quiet_strip_drops_refusal_and_other_apps(self):
        refused = quiet_strip_chat(SCREENSHOT_REFUSAL)
        self.assertNotIn("capabilities", refused.lower())
        self.assertNotIn("conversational", refused.lower())
        tour = quiet_strip_chat(
            "Directors-Eye is chugging along. You want projects in /srv?"
        )
        self.assertNotIn("Directors", tour)
        self.assertNotIn("/srv", tour)
        self.assertEqual(quiet_strip_chat("Hey! What's up?"), "Hey! What's up?")


class RelayRoutingTests(unittest.TestCase):
    def test_intent_and_policy(self):
        text = "Will you pass on a message to Raaish?"
        self.assertEqual(classify_intent(text), "relay")
        action, pol = classify_action("relay", text)
        self.assertEqual(action, "comms.third_party")
        self.assertEqual(pol, "confirm")
        self.assertEqual(policy_for("comms.third_party"), "confirm")
        self.assertEqual(classify_intent("Reply to the guy who's texting u Not me"), "relay")
        self.assertEqual(classify_intent("hey"), "chat")
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")

    def test_orchestrator_routes_relay_to_life(self):
        orch = _load(ROOT / "services" / "orchestrator" / "app" / "main.py", "orch_agent_voice")
        role, reason = orch._route_text("Will you pass on a message to Raaish?")
        self.assertEqual(role, "life")
        self.assertEqual(reason, "relay_intent")
        role2, _ = orch._route_text("hey how are you")
        self.assertEqual(role2, "frontoffice")


class RelayToolTests(unittest.TestCase):
    def setUp(self):
        self.lt = _load_life()
        self._env = mock.patch.dict(os.environ, {"ALLOWED_TELEGRAM_USER_IDS": "929388047"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_guest_refused(self):
        with mock.patch.object(self.lt, "set_relay_pending") as pending:
            out = self.lt.relay_telegram_message(
                caller_telegram_id="1210484792",
                chat_id="1210484792",
                recipient_name="Raaish",
                text="hi",
            )
        self.assertTrue(out.startswith("REFUSED"))
        pending.assert_not_called()

    def test_unknown_name_asks_for_id(self):
        with mock.patch.object(self.lt, "set_relay_pending") as pending:
            out = self.lt.relay_telegram_message(
                caller_telegram_id="929388047",
                chat_id="929388047",
                recipient_name="Raaish",
                text="running late",
            )
        self.assertIn("NEED_RECIPIENT", out)
        self.assertIn("Raaish", out)
        payload = pending.call_args.args[1]
        self.assertEqual(payload["status"], "need_id")
        self.assertEqual(payload["text"], "running late")
        self.assertIsNone(payload["telegram_user_id"])

    def test_known_id_stages_confirm_does_not_send(self):
        with mock.patch.object(self.lt, "set_relay_pending") as pending:
            out = self.lt.relay_telegram_message(
                caller_telegram_id="929388047",
                chat_id="929388047",
                recipient_name="Raaish",
                telegram_user_id=555001,
                text="running late",
            )
        self.assertIn("CONFIRM_REQUIRED", out)
        self.assertIn("555001", out)
        self.assertNotIn("SENT", out)
        payload = pending.call_args.args[1]
        self.assertEqual(payload["status"], "confirm")
        self.assertEqual(payload["telegram_user_id"], 555001)

    def test_latest_other_chat(self):
        with mock.patch.object(self.lt, "lookup_latest_other_chat", return_value=4242):
            with mock.patch.object(self.lt, "set_relay_pending") as pending:
                out = self.lt.relay_telegram_message(
                    caller_telegram_id="929388047",
                    chat_id="929388047",
                    text="you there?",
                    use_latest_other_chat=True,
                )
        self.assertIn("CONFIRM_REQUIRED", out)
        self.assertIn("4242", out)
        self.assertEqual(pending.call_args.args[1]["telegram_user_id"], 4242)


class RelayIngressTests(unittest.TestCase):
    def test_yes_sends_to_recipient_not_only_owner(self):
        import app.relay_handlers as relay

        sent: list[tuple[str, str]] = []

        def send(cid, msg, tid=""):
            sent.append((str(cid), msg))
            return True

        pending = {
            "recipient_name": "Raaish",
            "telegram_user_id": 555001,
            "text": "running late",
            "status": "confirm",
        }
        with mock.patch.object(relay, "is_owner", return_value=True), mock.patch.object(
            relay, "get_pending", return_value=pending
        ), mock.patch.object(relay, "clear_pending") as cleared:
            reason = relay.try_handle_relay(
                text="yes",
                chat_id="929388047",
                telegram_user_id=929388047,
                thread_id="",
                chat_type="private",
                send=send,
            )
        self.assertEqual(reason, "relay_sent")
        self.assertEqual(sent[0][0], "555001")
        self.assertIn("running late", sent[0][1])
        self.assertEqual(sent[1][0], "929388047")
        cleared.assert_called_once()

    def test_guest_pending_is_dropped(self):
        import app.relay_handlers as relay

        def send(cid, msg, tid=""):
            raise AssertionError("guest must not send")

        with mock.patch.object(relay, "is_owner", return_value=False), mock.patch.object(
            relay, "get_pending", return_value={"status": "confirm", "telegram_user_id": 1, "text": "x"}
        ), mock.patch.object(relay, "clear_pending") as cleared:
            reason = relay.try_handle_relay(
                text="yes",
                chat_id="1210484792",
                telegram_user_id=1210484792,
                thread_id="",
                chat_type="private",
                send=send,
            )
        self.assertIsNone(reason)
        cleared.assert_called_once()


if __name__ == "__main__":
    unittest.main()
