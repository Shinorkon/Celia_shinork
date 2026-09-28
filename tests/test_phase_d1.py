"""Phase D1: /start+bulk clear intents, life agent tools, A/B/C regression smoke."""
from __future__ import annotations

import importlib.util
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

from app.intent_router import (  # noqa: E402
    classify_intent,
    looks_like_list_intent,
    looks_like_list_clear,
    looks_like_memory_clear,
    looks_like_bulk_clear_both,
    is_start_or_help,
    parse_remove_query,
)
from app.side_effect_policy import POLICY_TABLE, classify_action, policy_for  # noqa: E402
from app.list_handlers import try_handle_list  # noqa: E402
from app import list_store  # noqa: E402
from app.memory_handlers import try_handle_memory, clear_memory_for_tests  # noqa: E402
from app.quiet_mode import quiet_strip_completion  # noqa: E402


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_phase_d1"
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
    sys.modules[name] = mod  # required before exec for @dataclass
    spec.loader.exec_module(mod)
    return mod


def _load_orch():
    path = ROOT / "services" / "orchestrator" / "app" / "main.py"
    # Avoid importing FastAPI app side effects — just exec route helper via source scan
    src = path.read_text()
    return src


class IntentStartBulkTests(unittest.TestCase):
    def test_start_is_help_not_list(self):
        self.assertTrue(is_start_or_help("/start"))
        self.assertEqual(classify_intent("/start", has_active_list=True, is_collecting=True), "help")
        self.assertFalse(looks_like_list_intent("/start", has_active_list=True, is_collecting=True))

    def test_slash_commands_not_list_items(self):
        self.assertFalse(looks_like_list_intent("/help", has_active_list=True, is_collecting=True))
        self.assertFalse(looks_like_list_intent("/digest", has_active_list=True, is_collecting=True))

    def test_bulk_clear_memory_and_list(self):
        t = "Remove everything from memory and list first"
        self.assertTrue(looks_like_bulk_clear_both(t))
        self.assertTrue(looks_like_memory_clear(t))
        self.assertEqual(classify_intent(t, has_active_list=True, is_collecting=True), "memory")
        self.assertFalse(looks_like_list_intent(t, has_active_list=True, is_collecting=True))
        self.assertIsNone(parse_remove_query(t))

    def test_clear_list_intent(self):
        self.assertTrue(looks_like_list_clear("clear the list"))
        self.assertEqual(classify_intent("clear the list", has_active_list=True), "list")
        self.assertEqual(classify_action("list", "clear the list")[0], "list.clear")

    def test_forget_all_memory_intent(self):
        self.assertTrue(looks_like_memory_clear("forget everything from memory"))
        self.assertEqual(classify_intent("forget everything from memory"), "memory")
        a, p = classify_action("memory", "forget everything from memory")
        self.assertEqual(a, "memory.forget_all")
        self.assertEqual(p, "confirm")

    def test_item_remove_still_works(self):
        self.assertEqual(parse_remove_query("remove milk"), "milk")
        self.assertEqual(
            classify_intent("remove milk", has_active_list=True, is_collecting=True),
            "list",
        )


class PolicyD1Tests(unittest.TestCase):
    def test_new_keys(self):
        self.assertEqual(policy_for("list.clear"), "auto")
        self.assertEqual(policy_for("memory.forget_all"), "confirm")
        for k in (
            "reminder.create",
            "reminder.list",
            "reminder.cancel",
            "task.create",
            "task.list",
        ):
            self.assertIn(k, POLICY_TABLE)
            self.assertEqual(policy_for(k), "auto", k)


class ListClearHandlerTests(unittest.TestCase):
    def setUp(self):
        list_store.clear_memory_for_tests()
        self.replies: list[str] = []

    def _send(self, chat_id, text, thread_id=""):
        self.replies.append(text)
        return True

    def test_clear_list(self):
        list_store.create_list("c1", title="Groceries", items=[{"name": "milk", "qty": 1}], collecting=True)
        reason = try_handle_list(
            text="clear the list",
            chat_id="c1",
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self._send,
        )
        self.assertEqual(reason, "list_cleared")
        self.assertTrue(any("Cleared" in r for r in self.replies))
        doc = list_store.get_active_list("c1")
        self.assertIsNotNone(doc)
        self.assertEqual(list_store.total_item_count(doc), 0)

    def test_start_not_appended(self):
        list_store.create_list("c2", title="List", items=[{"name": "eggs", "qty": 1}], collecting=True)
        reason = try_handle_list(
            text="/start",
            chat_id="c2",
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self._send,
        )
        self.assertIsNone(reason)
        doc = list_store.get_active_list("c2")
        names = [i["name"] for i in (doc or {}).get("items") or []]
        self.assertNotIn("/start", names)


class MemoryBulkPendingTests(unittest.TestCase):
    def setUp(self):
        clear_memory_for_tests()
        list_store.clear_memory_for_tests()
        self.replies: list[str] = []

    def _send(self, chat_id, text, thread_id=""):
        self.replies.append(text)
        return True

    def test_bulk_asks_confirm(self):
        with mock.patch("app.memory_handlers.store") as ms:
            ms.ensure_user.return_value = 5
            ms.list_known.return_value = [
                {"id": 1, "title": "pref", "body": "x"},
                {"id": 2, "title": "fact", "body": "y"},
            ]
            reason = try_handle_memory(
                text="Remove everything from memory and list first",
                chat_id="929388047",
                telegram_user_id=929388047,
                thread_id="",
                chat_type="private",
                send=self._send,
            )
        self.assertEqual(reason, "memory_clear_pending")
        self.assertTrue(any("Forget all" in r and "yes" in r.lower() for r in self.replies))


class LifeAgentRegistryTests(unittest.TestCase):
    def test_life_tools_registered(self):
        llm = _load_llm_client()
        names = sorted(llm.registered_tool_names("life"))
        self.assertEqual(
            names,
            [
                "cancel_reminder",
                "create_reminder",
                "create_task",
                "list_reminders",
                "list_tasks",
            ],
        )
        self.assertNotIn("run_shell_command", names)
        for n in names:
            self.assertIn(n, llm.TOOL_POLICY_KEYS)
        self.assertEqual(llm.ROLE_MODEL_MAP.get("life"), "gemini-2.5-flash")
        prompt = llm.build_system_prompt("life")
        self.assertIn("reminder", prompt.lower())
        self.assertNotIn("run_shell_command", prompt)

    def test_life_reflect_untouched(self):
        llm = _load_llm_client()
        self.assertEqual(sorted(llm.registered_tool_names("life-reflect")), ["notify_user", "recall_memory"])

    def test_quiet_strip_life(self):
        raw = "✅ Reminder set. I can also check the VPS and Shnuk."
        out = quiet_strip_completion(raw, agent_role="life", status="completed")
        self.assertNotIn("✅", out)
        self.assertNotIn("Shnuk", out)
        self.assertNotIn("VPS", out)


class OrchLifeRouteTests(unittest.TestCase):
    def test_route_source_prefers_life(self):
        src = _load_orch()
        self.assertIn('return "life", "preferred_life"', src)
        self.assertIn('return "life", "life_action"', src)
        self.assertIn('"life"', src)
        # SSH multi-step ops still parked — no ops_loop import
        self.assertNotIn("ops_loop", src)
        self.assertNotIn("plan_ops_steps", src)


class PhaseABCSmoke(unittest.TestCase):
    def test_list_make_still_list(self):
        self.assertEqual(classify_intent("make a list"), "list")

    def test_finance_wins(self):
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")

    def test_ops_confirm_policy(self):
        self.assertEqual(policy_for("ops.shell_read"), "confirm")
        self.assertEqual(policy_for("ops.deploy"), "confirm")

    def test_greeting_chat(self):
        self.assertEqual(classify_intent("hey"), "chat")


if __name__ == "__main__":
    unittest.main()
