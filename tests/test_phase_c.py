"""Phase C unit tests: side-effect policy, quiet strip, ops gate; A/B regression."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.side_effect_policy import (  # noqa: E402
    POLICY_TABLE,
    classify_action,
    policy_for,
)
from app.quiet_mode import (  # noqa: E402
    completion_prefix,
    quiet_strip_chat,
    quiet_strip_completion,
    strip_status_opener,
)
from app.ops_handlers import (  # noqa: E402
    clear_memory_for_tests,
    extract_ops_topic,
    map_ops_command,
    try_handle_ops,
)
from app.intent_router import classify_intent  # noqa: E402
from app.list_handlers import try_handle_list  # noqa: E402
from app import list_store  # noqa: E402


class PolicyTableTests(unittest.TestCase):
    def test_lists_auto(self):
        for key in (
            "list.create",
            "list.append",
            "list.show",
            "list.bought",
            "list.remove",
            "list.rename",
            "list.done",
        ):
            self.assertEqual(policy_for(key), "auto", key)

    def test_finance_write_confirm(self):
        self.assertEqual(policy_for("finance.write"), "confirm")
        self.assertEqual(policy_for("finance.read"), "auto")

    def test_ops_confirm(self):
        self.assertEqual(policy_for("ops.shell_read"), "auto")
        for key in (
            "ops.shell_write",
            "ops.deploy",
            "ops.destructive",
        ):
            self.assertEqual(policy_for(key), "confirm", key)

    def test_refuse_keys(self):
        self.assertEqual(policy_for("policy.other_apps"), "refuse")
        self.assertEqual(policy_for("policy.secrets_exfil"), "refuse")

    def test_classify_list_create(self):
        action, pol = classify_action("list", "make a list")
        self.assertEqual(action, "list.create")
        self.assertEqual(pol, "auto")

    def test_classify_ops_read(self):
        action, pol = classify_action("ops", "check docker on the vps")
        self.assertEqual(action, "ops.shell_read")
        self.assertEqual(pol, "auto")

    def test_classify_ops_deploy(self):
        action, pol = classify_action("ops", "deploy the app")
        self.assertEqual(action, "ops.deploy")
        self.assertEqual(pol, "confirm")

    def test_classify_refuse_other_apps(self):
        action, pol = classify_action("ops", "restart budget-tracker")
        self.assertEqual(action, "policy.other_apps")
        self.assertEqual(pol, "refuse")

    def test_table_has_expected_tiers(self):
        self.assertIn("auto", set(POLICY_TABLE.values()))
        self.assertIn("confirm", set(POLICY_TABLE.values()))
        self.assertIn("refuse", set(POLICY_TABLE.values()))


class QuietModeTests(unittest.TestCase):
    def test_strip_checkmark_opener(self):
        self.assertEqual(strip_status_opener("✅ Hey there"), "Hey there")
        self.assertEqual(strip_status_opener("❌ failed"), "failed")

    def test_quiet_strips_brochure(self):
        raw = (
            "Pretty good. I can check the VPS, list Shnuk, or open Budgy. "
            "Also Directors Eye is on /opt."
        )
        out = quiet_strip_chat(raw)
        self.assertNotIn("Shnuk", out)
        self.assertNotIn("Budgy", out)
        self.assertNotIn("VPS", out.lower())
        self.assertNotIn("Directors", out)
        # First sentence without banned topics may survive
        self.assertTrue(out.startswith("Pretty good") or out == "")

    def test_quiet_keeps_plain_chat(self):
        self.assertEqual(quiet_strip_chat("Hey! What's up?"), "Hey! What's up?")

    def test_completion_no_checkmark_prefix(self):
        self.assertEqual(completion_prefix("frontoffice", "completed"), "")
        self.assertEqual(completion_prefix("executor", "completed"), "")
        self.assertEqual(completion_prefix("frontoffice", "failed"), "❌ ")

    def test_quiet_strip_completion_chat_role(self):
        raw = "✅ I can help with the VPS and Shnuk."
        out = quiet_strip_completion(raw, agent_role="frontoffice", status="completed")
        self.assertNotIn("✅", out)
        self.assertNotIn("Shnuk", out)
        self.assertNotIn("VPS", out)


class OpsGateTests(unittest.TestCase):
    def setUp(self):
        clear_memory_for_tests()
        self.replies: list[str] = []

        def send(cid, msg, tid=""):
            self.replies.append(msg)
            return True

        self.send = send
        self.chat = "test-chat-phase-c"

    def _handle(self, text: str):
        return try_handle_ops(
            text=text,
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )

    def test_ops_asks_confirm_not_brochure(self):
        # Reads are auto — still no brochure
        with mock.patch("app.ops_handlers._dispatch_ops_agent") as disp:
            reason = self._handle("check docker on the vps")
            self.assertEqual(reason, "ops_auto_dispatched")
            disp.assert_called_once()
        self.assertEqual(len(self.replies), 1)
        reply = self.replies[0]
        self.assertIn("Checking", reply)
        self.assertNotIn("✅", reply)
        self.assertNotIn("Shnuk", reply)
        self.assertNotIn("I can", reply)

    def test_hey_not_ops(self):
        self.assertIsNone(self._handle("hey"))
        self.assertEqual(self.replies, [])

    def test_ops_yes_dispatches(self):
        # Mutating path still confirms then dispatches ops agent
        reason = self._handle("restart aop-worker on the vps")
        self.assertEqual(reason, "ops_pending_confirm")
        self.replies.clear()
        with mock.patch("app.ops_handlers._dispatch_ops_agent") as disp:
            reason = self._handle("yes")
            self.assertEqual(reason, "ops_confirmed_dispatched")
            disp.assert_called_once()
            args = disp.call_args[0]
            kwargs = disp.call_args.kwargs
            self.assertIn("restart", args[0].lower())
            self.assertTrue(kwargs.get("mutate_confirmed") if "mutate_confirmed" in kwargs else (args[4] if len(args) > 4 else False))
        self.assertTrue(any("On it" in r or "Checking" in r for r in self.replies))
        self.assertNotIn("✅", self.replies[0])

    def test_ops_no_cancels(self):
        self._handle("restart aop-scheduler")
        self.replies.clear()
        reason = self._handle("no")
        self.assertEqual(reason, "ops_cancelled")
        self.assertIn("skipped", self.replies[0].lower())

    def test_refuse_other_apps(self):
        reason = self._handle("restart budget-tracker on the vps")
        self.assertEqual(reason, "ops_refused")
        self.assertIn("policy", self.replies[0].lower())

    def test_topic_and_map(self):
        self.assertEqual(extract_ops_topic("check docker please"), "docker")
        self.assertIn("docker ps", map_ops_command("check docker") or "")
        # Named aop-*/celia-* containers beat the generic "Celia" label
        self.assertEqual(extract_ops_topic("restart aop-worker"), "aop-worker")
        self.assertEqual(
            extract_ops_topic("restart aop-ingress and aop-scheduler"),
            "aop-ingress, aop-scheduler",
        )
        self.assertEqual(extract_ops_topic("how's Celia looking"), "Celia")


class PhaseABRegressionTests(unittest.TestCase):
    """Keep Phase A/B success bars green under Phase C."""

    def setUp(self):
        list_store.clear_memory_for_tests()
        clear_memory_for_tests()
        self.replies: list[str] = []

        def send(cid, msg, tid=""):
            self.replies.append(msg)
            return True

        self.send = send
        self.chat = "test-chat-phase-c-ab"

    def test_list_still_auto(self):
        reason = try_handle_list(
            text="make a grocery list\nCondensed milk x4",
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "list_created")
        self.assertNotIn("✅", self.replies[0])
        action, pol = classify_action("list", "make a grocery list")
        self.assertEqual(pol, "auto")

    def test_finance_intent_still_wins(self):
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")
        action, pol = classify_action("finance", "spent 50 at Agora")
        self.assertEqual(action, "finance.write")
        self.assertEqual(pol, "confirm")

    def test_hey_is_chat_not_ops(self):
        self.assertEqual(classify_intent("hey"), "chat")
        self.assertIsNone(
            try_handle_ops(
                text="hey",
                chat_id=self.chat,
                telegram_user_id=1,
                thread_id="",
                chat_type="private",
                send=self.send,
            )
        )


if __name__ == "__main__":
    unittest.main()
