"""Life OS slice 2: tasks/reminders unit tests + A/B/C/memory regression smoke."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.side_effect_policy import POLICY_TABLE, classify_action, policy_for  # noqa: E402
from app.reminder_parse import (  # noqa: E402
    USER_TZ,
    bundle_window_key,
    looks_like_reminder,
    looks_like_task,
    parse_reminder,
    parse_task,
    to_utc,
)
from app.intent_router import classify_intent, looks_like_list_intent  # noqa: E402
from app.task_handlers import try_handle_tasks  # noqa: E402


class PolicySlice2Tests(unittest.TestCase):
    def test_reminder_task_keys(self):
        for k in (
            "task.create",
            "task.complete",
            "task.delete",
            "task.list",
            "reminder.create",
            "reminder.list",
            "reminder.cancel",
            "reminder.edit",
            "reminder.snooze",
        ):
            self.assertIn(k, POLICY_TABLE)
            self.assertEqual(policy_for(k), "auto", k)
        self.assertEqual(policy_for("comms.third_party"), "confirm")

    def test_classify_reminder_create(self):
        a, p = classify_action("reminder", "remind me in 3 hours to stretch")
        self.assertEqual(a, "reminder.create")
        self.assertEqual(p, "auto")

    def test_classify_reminder_cancel(self):
        a, p = classify_action("reminder", "cancel reminder stretch")
        self.assertEqual(a, "reminder.cancel")
        self.assertEqual(p, "auto")


class ParseTests(unittest.TestCase):
    def setUp(self):
        # Fixed: Wed 23 Sep 2026 09:00 MVT
        self.now = datetime(2026, 9, 23, 9, 0, tzinfo=USER_TZ)

    def test_relative_hours(self):
        spec = parse_reminder("remind me in 3 hours to stretch", now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.kind, "once")
        self.assertEqual(spec.title.lower(), "stretch")
        delta = spec.run_at - to_utc(self.now)
        self.assertAlmostEqual(delta.total_seconds(), 3 * 3600, delta=2)

    def test_every_monday(self):
        spec = parse_reminder("remind me every Monday to pay rent", now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.kind, "cron")
        self.assertEqual(spec.cron_expr, "0 9 * * mon")
        self.assertIn("pay rent", spec.title.lower())

    def test_named_day_clock(self):
        spec = parse_reminder("remind me Thursday 9am pay rent", now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.kind, "once")
        local = spec.run_at.astimezone(USER_TZ)
        self.assertEqual(local.weekday(), 3)  # Thursday
        self.assertEqual(local.hour, 9)

    def test_task_with_due(self):
        spec = parse_task("todo: pay rent due Friday", now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.title.lower(), "pay rent")
        self.assertIsNotNone(spec.due_at)

    def test_bundle_key_stable(self):
        a = to_utc(self.now)
        b = a + timedelta(seconds=30)
        self.assertEqual(bundle_window_key(a), bundle_window_key(b))


class IntentTests(unittest.TestCase):
    def test_reminder_intent(self):
        self.assertEqual(classify_intent("remind me in 3 hours"), "reminder")

    def test_task_intent(self):
        self.assertEqual(classify_intent("todo: buy stamps due tomorrow"), "task")

    def test_shopping_list_still_list(self):
        self.assertEqual(classify_intent("make a shopping list"), "list")
        self.assertTrue(looks_like_list_intent("make a shopping list"))

    def test_finance_still_wins(self):
        self.assertEqual(classify_intent("spent 50 on coffee"), "finance")

    def test_memory_still(self):
        self.assertEqual(classify_intent("what do you know about me?"), "memory")

    def test_looks_like_helpers(self):
        self.assertTrue(looks_like_reminder("remind me in 3 hours"))
        self.assertTrue(looks_like_task("todo: stretch"))
        self.assertFalse(looks_like_reminder("add milk to list"))


class HandlerMockTests(unittest.TestCase):
    def test_create_reminder_quiet(self):
        sent = []

        def send(cid, msg, tid=""):
            sent.append(msg)
            return True

        with mock.patch("app.task_handlers.store") as st:
            st.ensure_user.return_value = 5
            st.create_reminder.return_value = {
                "id": 1,
                "title": "stretch",
                "kind": "once",
                "bundled": False,
            }
            reason = try_handle_tasks(
                "remind me in 3 hours to stretch",
                "111",
                929388047,
                "",
                "private",
                send,
            )
        self.assertEqual(reason, "reminder_created")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("✅", sent[0])
        self.assertNotIn("Shnuk", sent[0])
        self.assertIn("#1", sent[0])

    def test_third_party_blocked(self):
        sent = []

        def send(cid, msg, tid=""):
            sent.append(msg)
            return True

        reason = try_handle_tasks(
            "remind them about the meeting",
            "111",
            929388047,
            "",
            "private",
            send,
        )
        self.assertEqual(reason, "reminder_third_party_blocked")
        self.assertTrue(sent)


class RegressionSmoke(unittest.TestCase):
    """Keep A/B/C/memory policy surfaces intact."""

    def test_list_auto(self):
        self.assertEqual(policy_for("list.create"), "auto")

    def test_finance_write_confirm(self):
        self.assertEqual(policy_for("finance.write"), "confirm")

    def test_ops_confirm(self):
        self.assertEqual(policy_for("ops.shell_read"), "confirm")

    def test_memory_forget_confirm(self):
        self.assertEqual(policy_for("memory.forget"), "confirm")

    def test_other_apps_refuse(self):
        a, p = classify_action("ops", "restart budget-tracker")
        self.assertEqual(a, "policy.other_apps")
        self.assertEqual(p, "refuse")


if __name__ == "__main__":
    unittest.main()
