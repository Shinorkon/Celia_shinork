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
    has_remind_verb,
    is_done_ack,
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
        self.assertIn("Got it", sent[0])
        self.assertIn("stretch", sent[0].lower())

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



class GreetingTypoReminderTests(unittest.TestCase):
    """Bug 2026-09-28: greeting + Remaind typo must not fall to AOP chat."""

    def setUp(self):
        # Mon 28 Sep 2026 07:10 MVT — matches the reported miss window
        self.now = datetime(2026, 9, 28, 7, 10, tzinfo=USER_TZ)

    def test_greeting_remaind_typo_intent(self):
        msg = "Hey man Remaind me to buy eggs and baked beans today around 3pm"
        self.assertEqual(classify_intent(msg), "reminder")
        self.assertTrue(looks_like_reminder(msg))
        self.assertTrue(has_remind_verb(msg))

    def test_greeting_remaind_typo_parse(self):
        msg = "Hey man Remaind me to buy eggs and baked beans today around 3pm"
        spec = parse_reminder(msg, now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.kind, "once")
        self.assertIn("eggs", spec.title.lower())
        self.assertIn("baked beans", spec.title.lower())
        self.assertNotIn("hey", spec.title.lower())
        local = spec.run_at.astimezone(USER_TZ)
        self.assertEqual(local.hour, 15)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.date(), self.now.date())
        self.assertEqual(spec.local_when, "around 3pm")

    def test_plain_remind_me(self):
        msg = "remind me to buy eggs and baked beans today around 3pm"
        self.assertEqual(classify_intent(msg), "reminder")
        spec = parse_reminder(msg, now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.run_at.astimezone(USER_TZ).hour, 15)
        self.assertIn("eggs", spec.title.lower())

    def test_remindr_typo(self):
        self.assertEqual(classify_intent("remindr me at 4pm to stretch"), "reminder")
        self.assertTrue(looks_like_reminder("remindr me at 4pm to stretch"))

    def test_do_not_steal_unrelated_hey(self):
        self.assertEqual(classify_intent("hey"), "chat")
        self.assertFalse(looks_like_reminder("hey"))
        self.assertFalse(looks_like_reminder("hey man"))
        self.assertFalse(looks_like_reminder("hey whats up"))
        self.assertNotEqual(classify_intent("hey whats up"), "reminder")
        # remember ≠ remind
        self.assertFalse(has_remind_verb("remember that I like coffee"))
        self.assertEqual(classify_intent("remember that I like coffee"), "memory")

    def test_handler_carlia_confirm(self):
        sent = []

        def send(cid, msg, tid=""):
            sent.append(msg)
            return True

        with mock.patch("app.task_handlers.store") as st:
            st.ensure_user.return_value = 5
            st.create_reminder.return_value = {
                "id": 42,
                "title": "buy eggs and baked beans",
                "kind": "once",
                "bundled": False,
            }
            reason = try_handle_tasks(
                "Hey man Remaind me to buy eggs and baked beans today around 3pm",
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
        self.assertNotIn("I can also", sent[0])
        self.assertIn("Got it", sent[0])
        self.assertIn("around 3pm", sent[0])
        self.assertIn("eggs", sent[0].lower())
        self.assertIn("baked beans", sent[0].lower())


class DecimalClockAndDoneAckTests(unittest.TestCase):
    """Bug 2026-09-28 pm: 3.30pm → 15:30; title strip; already-done ack."""

    def setUp(self):
        self.now = datetime(2026, 9, 28, 14, 40, tzinfo=USER_TZ)

    def test_decimal_dot_pm_at(self):
        msg = "Yo Make sure to remaind me to buy eggs and baked beans at 3.30pm today"
        self.assertEqual(classify_intent(msg), "reminder")
        spec = parse_reminder(msg, now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        local = spec.run_at.astimezone(USER_TZ)
        self.assertEqual(local.hour, 15)
        self.assertEqual(local.minute, 30)
        self.assertEqual(local.date(), self.now.date())
        title = spec.title.lower()
        self.assertIn("eggs", title)
        self.assertIn("baked beans", title)
        self.assertNotIn("remaind", title)
        self.assertNotIn("make sure", title)
        self.assertNotIn("today", title)

    def test_decimal_dot_bare_pm(self):
        spec = parse_reminder(
            "remind me to stretch 3.30 pm today", now=self.now
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        local = spec.run_at.astimezone(USER_TZ)
        self.assertEqual((local.hour, local.minute), (15, 30))

    def test_colon_still_works(self):
        spec = parse_reminder(
            "remind me to stretch at 3:30pm today", now=self.now
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        local = spec.run_at.astimezone(USER_TZ)
        self.assertEqual((local.hour, local.minute), (15, 30))

    def test_already_done_intent(self):
        for phrase in (
            "Already done",
            "already done",
            "done",
            "finished",
            "all done",
            "got it done",
        ):
            self.assertTrue(is_done_ack(phrase), phrase)
            self.assertTrue(looks_like_reminder(phrase), phrase)
            self.assertEqual(classify_intent(phrase), "reminder", phrase)

    def test_already_done_handler_cancels_latest(self):
        sent = []

        def send(cid, msg, tid=""):
            sent.append(msg)
            return True

        with mock.patch("app.task_handlers.store") as st:
            st.ensure_user.return_value = 5
            st.latest_clearable_reminder.return_value = {
                "id": 28,
                "title": "buy eggs and baked beans",
                "kind": "once",
                "clear_mode": "active",
            }
            st.mark_reminder_done.return_value = True
            reason = try_handle_tasks(
                "Already done",
                "111",
                929388047,
                "",
                "private",
                send,
            )
        self.assertEqual(reason, "reminder_done_ack")
        self.assertTrue(sent)
        self.assertEqual(sent[0], "Got it, marked done.")
        st.mark_reminder_done.assert_called_once_with(5, 28)

    def test_done_after_fired_reminder_acks(self):
        """Bug 2026-09-30: Done after scheduled fire must not say Nothing open."""
        sent = []

        def send(cid, msg, tid=""):
            sent.append(msg)
            return True

        with mock.patch("app.task_handlers.store") as st:
            st.ensure_user.return_value = 5
            st.latest_clearable_reminder.return_value = {
                "id": 29,
                "title": "buy chocolate powder",
                "kind": "once",
                "status": "fired",
                "clear_mode": "fired",
            }
            st.mark_reminder_done.return_value = True
            reason = try_handle_tasks(
                "Done",
                "111",
                929388047,
                "",
                "private",
                send,
            )
        self.assertEqual(reason, "reminder_done_ack")
        self.assertEqual(sent, ["Got it, marked done."])
        st.mark_reminder_done.assert_called_once_with(5, 29)


class RegressionSmoke(unittest.TestCase):
    """Keep A/B/C/memory policy surfaces intact."""

    def test_list_auto(self):
        self.assertEqual(policy_for("list.create"), "auto")

    def test_finance_write_confirm(self):
        self.assertEqual(policy_for("finance.write"), "confirm")

    def test_ops_confirm(self):
        self.assertEqual(policy_for("ops.shell_read"), "auto")

    def test_memory_forget_confirm(self):
        self.assertEqual(policy_for("memory.forget"), "confirm")

    def test_other_apps_refuse(self):
        a, p = classify_action("ops", "restart budget-tracker")
        self.assertEqual(a, "policy.other_apps")
        self.assertEqual(p, "refuse")


if __name__ == "__main__":
    unittest.main()
