"""Life OS slice 4: calendar + notes unit tests + regression smoke."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
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
from app.calendar_parse import (  # noqa: E402
    is_agenda_query,
    looks_like_calendar,
    parse_agenda,
    parse_event,
)
from app.intent_router import classify_intent  # noqa: E402
from app.note_handlers import looks_like_note, try_handle_notes  # noqa: E402
from app import calendar_handlers as cal_h  # noqa: E402


USER_TZ = ZoneInfo("Indian/Maldives")


class PolicySlice4Tests(unittest.TestCase):
    def test_calendar_note_keys(self):
        self.assertEqual(policy_for("cal.create"), "confirm")
        self.assertEqual(policy_for("cal.update"), "confirm")
        self.assertEqual(policy_for("cal.list"), "auto")
        self.assertEqual(policy_for("note.create"), "auto")
        self.assertEqual(policy_for("note.read"), "auto")
        self.assertEqual(policy_for("policy.secrets_exfil"), "refuse")

    def test_classify_calendar_create(self):
        a, p = classify_action("calendar", "add event Friday 3pm dentist")
        self.assertEqual(a, "cal.create")
        self.assertEqual(p, "confirm")

    def test_classify_agenda(self):
        a, p = classify_action("calendar", "what's on this week")
        self.assertEqual(a, "cal.list")
        self.assertEqual(p, "auto")

    def test_classify_note(self):
        a, p = classify_action("note", "note: landlord wifi is x")
        self.assertEqual(a, "note.create")
        self.assertEqual(p, "auto")


class ParseTests(unittest.TestCase):
    def setUp(self):
        # Wed 23 Sep 2026 10:00 MVT
        self.now = datetime(2026, 9, 23, 10, 0, tzinfo=USER_TZ)

    def test_add_event_friday_3pm(self):
        spec = parse_event("add event Friday 3pm dentist", now=self.now)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.title.lower(), "dentist")
        local = spec.starts_at.astimezone(USER_TZ)
        self.assertEqual(local.weekday(), 4)  # Friday
        self.assertEqual(local.hour, 15)

    def test_agenda_week(self):
        self.assertTrue(is_agenda_query("what's on this week"))
        spec = parse_agenda("what's on this week", now=self.now)
        self.assertEqual(spec.label, "this week")

    def test_looks_like_calendar(self):
        self.assertTrue(looks_like_calendar("add event Friday 3pm dentist"))
        self.assertTrue(looks_like_calendar("/agenda"))
        self.assertFalse(looks_like_calendar("spent 50 on coffee"))


class IntentTests(unittest.TestCase):
    def test_calendar_intent(self):
        self.assertEqual(classify_intent("add event Friday 3pm dentist"), "calendar")
        self.assertEqual(classify_intent("what's on this week"), "calendar")

    def test_note_intent(self):
        self.assertEqual(classify_intent("note: landlord wifi is x"), "note")
        self.assertEqual(classify_intent("what notes about wifi"), "note")

    def test_regressions(self):
        self.assertEqual(classify_intent("spent 50 on coffee"), "finance")
        self.assertEqual(classify_intent("add milk to list"), "list")
        self.assertEqual(classify_intent("remind me in 2 hours stretch"), "reminder")
        self.assertEqual(classify_intent("remember that I prefer short replies"), "memory")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        cal_h.clear_calendar_for_tests()
        self.sent: list[str] = []

        def send(chat_id, msg, thread_id=""):
            self.sent.append(msg)
            return True

        self.send = send

    @mock.patch("app.calendar_handlers.store")
    def test_create_asks_confirm(self, mock_store):
        mock_store.ensure_user.return_value = 1
        mock_store.find_conflicts.return_value = []
        reason = cal_h.try_handle_calendar(
            text="add event Friday 3pm dentist",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "calendar_create_pending")
        self.assertTrue(self.sent)
        self.assertIn("?", self.sent[-1])
        self.assertNotIn("✅", self.sent[-1])

    @mock.patch("app.calendar_handlers.store")
    def test_create_confirm_yes(self, mock_store):
        mock_store.ensure_user.return_value = 1
        mock_store.find_conflicts.return_value = []
        mock_store.create_event.return_value = {
            "id": 7,
            "title": "dentist",
            "starts_at": datetime(2026, 9, 25, 10, 0),
            "location": None,
        }
        cal_h.try_handle_calendar(
            text="add event Friday 3pm dentist",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        reason = cal_h.try_handle_calendar(
            text="yes",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "calendar_created")
        self.assertTrue(mock_store.create_event.called)
        self.assertIn("Booked", self.sent[-1])

    @mock.patch("app.calendar_handlers.store")
    def test_agenda_auto(self, mock_store):
        mock_store.ensure_user.return_value = 1
        mock_store.list_events.return_value = [
            {
                "id": 1,
                "title": "dentist",
                "starts_at": datetime(2026, 9, 25, 10, 0),
                "location": None,
            }
        ]
        reason = cal_h.try_handle_calendar(
            text="what's on this week",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "calendar_agenda")
        self.assertIn("dentist", self.sent[-1].lower())

    @mock.patch("app.note_handlers.store")
    def test_note_auto_save_and_recall(self, mock_store):
        mock_store.ensure_user.return_value = 1
        mock_store.save_item.return_value = 42
        mock_store.find_candidates.return_value = [
            {"id": 42, "kind": "note", "title": "landlord wifi", "body": "landlord wifi is x"}
        ]
        mock_store.list_known.return_value = []
        reason = try_handle_notes(
            text="note: landlord wifi is x",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "note_saved")
        self.assertEqual(self.sent[-1], "Noted.")
        self.sent.clear()
        reason = try_handle_notes(
            text="what notes about wifi",
            chat_id="1",
            telegram_user_id=929388047,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "note_recalled")
        self.assertIn("wifi", self.sent[-1].lower())

    def test_looks_like_note(self):
        self.assertTrue(looks_like_note("note: hello"))
        self.assertTrue(looks_like_note("jot down buy eggs"))
        self.assertFalse(looks_like_note("spent 10 on tea"))


class RegressionPolicyTests(unittest.TestCase):
    def test_prior_slices_still_green(self):
        self.assertEqual(policy_for("list.create"), "auto")
        self.assertEqual(policy_for("finance.write"), "confirm")
        self.assertEqual(policy_for("reminder.create"), "auto")
        self.assertEqual(policy_for("memory.forget"), "confirm")
        self.assertEqual(policy_for("ops.shell_read"), "confirm")
        self.assertIn("auto", set(POLICY_TABLE.values()))


if __name__ == "__main__":
    unittest.main()
