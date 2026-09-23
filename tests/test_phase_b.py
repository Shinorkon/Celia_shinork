"""Phase B unit tests: titles, collecting session, check/remove, richer confirms.

Phase A regression lives in test_phase_a.py — this file covers B-only behaviors.
"""
from __future__ import annotations

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

from app.intent_router import (  # noqa: E402
    classify_intent,
    extract_list_title,
    looks_like_list_intent,
    parse_check_query,
    parse_remove_query,
    parse_rename_title,
)
from app.list_handlers import try_handle_list  # noqa: E402
from app import list_store  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_groceries_alias(self):
        self.assertEqual(extract_list_title("make a grocery list"), "Groceries")
        self.assertEqual(extract_list_title("shopping list"), "Shopping")

    def test_called_title(self):
        self.assertEqual(
            extract_list_title("make a list called Groceries"),
            "Groceries",
        )


class PhaseBHandlerTests(unittest.TestCase):
    def setUp(self):
        list_store.clear_memory_for_tests()
        self.replies: list[str] = []

        def send(cid, msg, tid=""):
            self.replies.append(msg)
            return True

        self.send = send
        self.chat = "test-chat-phase-b"

    def _handle(self, text: str):
        return try_handle_list(
            text=text,
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )

    def test_titled_create_and_confirm(self):
        reason = self._handle("make a grocery list\nCondensed milk x4")
        self.assertEqual(reason, "list_created")
        self.assertEqual(len(self.replies), 1)
        reply = self.replies[0]
        self.assertNotIn("✅", reply)
        self.assertNotIn("Shnuk", reply)
        self.assertIn("Condensed milk", reply)
        self.assertIn("×4", reply)
        self.assertIn("Groceries", reply)
        self.assertIn("1 item", reply)
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        self.assertEqual(doc["title"], "Groceries")
        self.assertTrue(doc["items"][0].get("id"))
        self.assertFalse(doc["items"][0].get("done"))
        self.assertTrue(list_store.is_collecting(self.chat))

    def test_collecting_session_appends(self):
        self._handle("make a list called Groceries")
        self.replies.clear()
        reason = self._handle("eggs x6")
        self.assertEqual(reason, "list_appended")
        self.assertIn("Added eggs ×6 to Groceries", self.replies[0])
        self.assertIn("1 item", self.replies[0])
        self.replies.clear()
        reason = self._handle("bread")
        self.assertEqual(reason, "list_appended")
        self.assertIn("2 items", self.replies[0])
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        self.assertEqual(len(doc["items"]), 2)

    def test_done_ends_collecting(self):
        self._handle("make a grocery list\nmilk")
        self.assertTrue(list_store.is_collecting(self.chat))
        self.replies.clear()
        reason = self._handle("done")
        self.assertEqual(reason, "list_done")
        self.assertFalse(list_store.is_collecting(self.chat))
        self.assertIn("Groceries", self.replies[0])
        self.assertNotIn("✅", self.replies[0])

    def test_show_list_richer(self):
        self._handle("make a grocery list\nCondensed milk x4\neggs")
        self.replies.clear()
        reason = self._handle("show list")
        self.assertEqual(reason, "list_shown")
        reply = self.replies[0]
        self.assertIn("Groceries", reply)
        self.assertIn("Condensed milk", reply)
        self.assertIn("[ ]", reply)
        self.assertNotIn("✅", reply)

    def test_check_off_bought(self):
        self._handle("make a grocery list\nCondensed milk x4\neggs")
        self.replies.clear()
        reason = self._handle("bought condensed milk")
        self.assertEqual(reason, "list_checked")
        self.assertIn("bought", self.replies[0].lower())
        self.assertIn("Groceries", self.replies[0])
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        milk = next(i for i in doc["items"] if "milk" in i["name"].lower())
        self.assertTrue(milk["done"])

    def test_remove_item(self):
        self._handle("make a list\neggs\nbread")
        self.replies.clear()
        reason = self._handle("remove eggs")
        self.assertEqual(reason, "list_removed")
        self.assertIn("Removed eggs", self.replies[0])
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        self.assertEqual(len(doc["items"]), 1)
        self.assertEqual(doc["items"][0]["name"], "bread")

    def test_rename(self):
        self._handle("make a list\nmilk")
        self.replies.clear()
        reason = self._handle("call it Groceries")
        self.assertEqual(reason, "list_renamed")
        self.assertIn("Groceries", self.replies[0])
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        self.assertEqual(doc["title"], "Groceries")

    def test_finance_still_not_list(self):
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")
        self.assertFalse(looks_like_list_intent("spent 50 at Agora"))
        reason = self._handle("spent 50 at Agora")
        self.assertIsNone(reason)

    def test_hey_not_list(self):
        reason = self._handle("hey")
        self.assertIsNone(reason)

    def test_parse_helpers(self):
        self.assertEqual(parse_check_query("bought milk"), "milk")
        self.assertEqual(parse_remove_query("remove eggs from list"), "eggs")
        self.assertEqual(parse_rename_title("call it Groceries"), "Groceries")


if __name__ == "__main__":
    unittest.main()
