"""Life OS slice 1: memory foundation unit tests."""
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
from app.memory_store import (  # noqa: E402
    is_junk_memory_item,
    segment_for_kind,
)
from app.memory_handlers import (  # noqa: E402
    clear_memory_for_tests,
    looks_like_memory,
    try_handle_memory,
)
from app.intent_router import classify_intent  # noqa: E402


class MemoryPolicyTests(unittest.TestCase):
    def test_memory_policy_keys(self):
        self.assertEqual(policy_for("memory.read"), "auto")
        self.assertEqual(policy_for("memory.write"), "auto")
        self.assertEqual(policy_for("memory.forget"), "confirm")
        self.assertEqual(policy_for("memory.correct"), "confirm")
        for k in ("memory.read", "memory.write", "memory.forget", "memory.correct"):
            self.assertIn(k, POLICY_TABLE)

    def test_classify_memory_intents(self):
        a, p = classify_action("memory", "remember that I prefer terse replies")
        self.assertEqual(a, "memory.write")
        self.assertEqual(p, "auto")
        a, p = classify_action("memory", "forget that condensed milk thing")
        self.assertEqual(a, "memory.forget")
        self.assertEqual(p, "confirm")
        a, p = classify_action("memory", "correct that budget for groceries is 3000")
        self.assertEqual(a, "memory.correct")
        self.assertEqual(p, "confirm")
        a, p = classify_action("memory", "what do you know about me?")
        self.assertEqual(a, "memory.read")
        self.assertEqual(p, "auto")


class JunkFilterTests(unittest.TestCase):
    def test_list_qty_is_junk(self):
        self.assertTrue(
            is_junk_memory_item(
                "preference",
                "Condensed Milk Quantity",
                "The user wants 4 units of condensed milk.",
            )
        )

    def test_receipt_total_is_junk(self):
        self.assertTrue(
            is_junk_memory_item(
                "project_state",
                "Updated spending total",
                "Total spending updated to 147.00 MVR.",
            )
        )
        self.assertTrue(
            is_junk_memory_item(
                "goal",
                "Calculate total spending from receipts",
                "The user wants me to calculate their total spending from receipts",
            )
        )

    def test_real_preference_ok(self):
        self.assertFalse(
            is_junk_memory_item(
                "preference",
                "Prefer terse replies",
                "Falulaan prefers short terse Telegram replies.",
            )
        )

    def test_segment_map(self):
        self.assertEqual(segment_for_kind("preference"), "semantic")
        self.assertEqual(segment_for_kind("decision"), "episodic")
        self.assertEqual(segment_for_kind("habit"), "procedural")
        self.assertEqual(segment_for_kind("goal", "working"), "working")


class MemoryHandlerTests(unittest.TestCase):
    def setUp(self):
        clear_memory_for_tests()
        self.sent: list[tuple[str, str, str]] = []

        def send(cid, msg, tid=""):
            self.sent.append((cid, msg, tid))
            return True

        self.send = send

    def test_looks_like_memory(self):
        self.assertTrue(looks_like_memory("remember that I prefer terse replies"))
        self.assertTrue(looks_like_memory("forget that condensed milk thing"))
        self.assertTrue(looks_like_memory("correct that budget for groceries is 3000"))
        self.assertTrue(looks_like_memory("what do you know about me?"))
        self.assertFalse(looks_like_memory("spent 50 at Agora"))
        self.assertFalse(looks_like_memory("make a grocery list"))

    def test_intent_router_memory(self):
        self.assertEqual(
            classify_intent("remember that I prefer terse replies"),
            "memory",
        )
        self.assertEqual(classify_intent("what do you know about me?"), "memory")
        # finance still wins
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")

    @mock.patch("app.memory_handlers.store")
    def test_remember_auto(self, mock_store):
        mock_store.ensure_user.return_value = 5
        mock_store.is_junk_memory_item.return_value = False
        mock_store.save_item.return_value = 99
        reason = try_handle_memory(
            "remember that I prefer terse replies",
            "1",
            929388047,
            "",
            "private",
            self.send,
        )
        self.assertEqual(reason, "memory_remembered")
        mock_store.save_item.assert_called_once()
        self.assertTrue(any("remember" in s[1].lower() for s in self.sent))

    @mock.patch("app.memory_handlers.store")
    def test_forget_confirm_then_yes(self, mock_store):
        mock_store.ensure_user.return_value = 5
        mock_store.find_candidates.return_value = [
            {
                "id": 12,
                "kind": "preference",
                "segment": "semantic",
                "title": "Condensed Milk Quantity",
                "body": "4 units",
            }
        ]
        mock_store.soft_forget.return_value = True
        r1 = try_handle_memory(
            "forget that condensed milk thing",
            "1",
            929388047,
            "",
            "private",
            self.send,
        )
        self.assertEqual(r1, "memory_forget_pending")
        self.assertTrue(any("Forget" in s[1] for s in self.sent))
        self.sent.clear()
        r2 = try_handle_memory("yes", "1", 929388047, "", "private", self.send)
        self.assertEqual(r2, "memory_forgotten")
        mock_store.soft_forget.assert_called_once()
        self.assertTrue(any("Forgotten" in s[1] for s in self.sent))

    @mock.patch("app.memory_handlers.store")
    def test_correct_confirm(self, mock_store):
        mock_store.ensure_user.return_value = 5
        mock_store.find_candidates.return_value = [
            {
                "id": 20,
                "kind": "fact",
                "segment": "semantic",
                "title": "grocery budget",
                "body": "budget for groceries is 2000",
            }
        ]
        mock_store.correct_item.return_value = 21
        r1 = try_handle_memory(
            "correct that budget for groceries is 3000",
            "1",
            929388047,
            "",
            "private",
            self.send,
        )
        self.assertEqual(r1, "memory_correct_pending")
        r2 = try_handle_memory("yes", "1", 929388047, "", "private", self.send)
        self.assertEqual(r2, "memory_corrected")
        mock_store.correct_item.assert_called_once()


if __name__ == "__main__":
    unittest.main()
