"""Phase A unit tests: debounce merge, intent router, list path, finance intact."""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# tests live at /root/Celia/tests; app at services/telegram-ingress (symlinked as /root/Celia/app)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.debounce import (  # noqa: E402
    BufferedUpdate,
    ChatDebouncer,
    merge_buffered_texts,
)
from app.intent_router import (  # noqa: E402
    classify_intent,
    looks_like_list_intent,
    parse_item_line,
)
from app.list_handlers import try_handle_list, _strip_list_opener  # noqa: E402
from app import list_store  # noqa: E402
from app.finance_parse import looks_like_finance, parse_finance  # noqa: E402


class DebounceTests(unittest.TestCase):
    def test_merge_split_messages(self):
        updates = [
            BufferedUpdate(1, "c", "", "private", "make a list"),
            BufferedUpdate(1, "c", "", "private", "Condensed milk x4"),
        ]
        self.assertEqual(
            merge_buffered_texts(updates),
            "make a list\nCondensed milk x4",
        )

    def test_debounce_collapses_rapid(self):
        flushed: list = []
        done = threading.Event()

        def on_flush(chat_id, items):
            flushed.append((chat_id, merge_buffered_texts(items)))
            done.set()

        d = ChatDebouncer(on_flush, delay_ms=200)
        d.add(BufferedUpdate(1, "42", "", "private", "make a list"))
        time.sleep(0.05)
        d.add(BufferedUpdate(1, "42", "", "private", "Condensed milk x4"))
        self.assertTrue(done.wait(2.0), "debounce did not flush")
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0][1], "make a list\nCondensed milk x4")
        d.cancel_all()


class IntentRouterTests(unittest.TestCase):
    def test_make_list(self):
        self.assertEqual(classify_intent("make a list"), "list")
        self.assertTrue(looks_like_list_intent("make a list"))

    def test_combined_bubble(self):
        t = "make a list\nCondensed milk x4"
        self.assertEqual(classify_intent(t), "list")

    def test_hey_not_list(self):
        self.assertEqual(classify_intent("hey"), "chat")
        self.assertFalse(looks_like_list_intent("hey"))

    def test_finance_still_looks_like(self):
        self.assertTrue(looks_like_finance("spent 50 at Agora"))
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")
        self.assertFalse(looks_like_list_intent("spent 50 at Agora"))
        p = parse_finance("spent 85 food")
        self.assertIsNotNone(p)
        self.assertEqual(p.amount_mvr, 85.0)

    def test_ops(self):
        self.assertEqual(classify_intent("check docker on the vps"), "ops")

    def test_parse_item(self):
        self.assertEqual(parse_item_line("Condensed milk x4"), ("Condensed milk", 4))
        self.assertEqual(parse_item_line("4x eggs"), ("eggs", 4))


class ListHandlerTests(unittest.TestCase):
    def setUp(self):
        list_store.clear_memory_for_tests()
        self.replies: list[str] = []

        def send(cid, msg, tid=""):
            self.replies.append(msg)
            return True

        self.send = send
        self.chat = "test-chat-phase-a"

    def test_split_msgs_one_list(self):
        """Success bar: make a list + Condensed milk x4 → one list, short reply."""
        text = "make a list\nCondensed milk x4"
        reason = try_handle_list(
            text=text,
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "list_created")
        self.assertEqual(len(self.replies), 1)
        reply = self.replies[0]
        self.assertNotIn("✅", reply)
        self.assertNotIn("Shnuk", reply)
        self.assertNotIn("Budgy", reply)
        self.assertNotIn("VPS", reply)
        # ≤2 sentences
        sentences = [s for s in reply.replace("!", ".").split(".") if s.strip()]
        self.assertLessEqual(len(sentences), 2)
        self.assertIn("Condensed milk", reply)
        doc = list_store.get_active_list(self.chat)
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(len(doc["items"]), 1)
        self.assertEqual(doc["items"][0]["name"], "Condensed milk")
        self.assertEqual(doc["items"][0]["qty"], 4)

    def test_combined_bubble(self):
        reason = try_handle_list(
            text="make a list\nCondensed milk x4",
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "list_created")
        doc = list_store.get_active_list(self.chat)
        assert doc is not None
        self.assertEqual(doc["items"][0]["qty"], 4)

    def test_hey_not_handled_as_list(self):
        reason = try_handle_list(
            text="hey",
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertIsNone(reason)
        self.assertEqual(self.replies, [])

    def test_strip_checkmark(self):
        self.assertEqual(_strip_list_opener("✅ Done with list"), "Done with list")


if __name__ == "__main__":
    unittest.main()
