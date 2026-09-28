"""Phase D2: life agent list/cal/notes tools + compound routing."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))
sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.intent_router import (  # noqa: E402
    classify_intent,
    is_compound_life_request,
    life_domain_flags,
    looks_like_life_action,
)
from app.side_effect_policy import policy_for  # noqa: E402
from app.quiet_mode import quiet_strip_completion  # noqa: E402


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_phase_d2"
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


def _load_life_tools():
    path = ROOT / "services" / "worker-runtime" / "app" / "life_tools.py"
    name = "life_tools_phase_d2"
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
    if "psycopg" not in sys.modules:
        sys.modules["psycopg"] = types.ModuleType("psycopg")
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


D2_LIST = {
    "create_list",
    "show_list",
    "add_list_items",
    "remove_list_item",
    "clear_list",
    "mark_list_item_bought",
}
D2_CAL = {"create_calendar_event", "list_calendar_events"}
D2_NOTES = {"add_note", "list_notes"}
D1 = {
    "create_reminder",
    "list_reminders",
    "cancel_reminder",
    "create_task",
    "list_tasks",
}


class CompoundRoutingTests(unittest.TestCase):
    def test_list_plus_remind(self):
        t = "add milk to the list and remind me at 5"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(life_domain_flags(t), {"list", "reminder_task"})
        self.assertEqual(classify_intent(t), "life")

    def test_simple_list_not_compound(self):
        self.assertFalse(is_compound_life_request("make a list"))
        self.assertEqual(classify_intent("make a list"), "list")

    def test_simple_remind_not_compound(self):
        self.assertFalse(is_compound_life_request("remind me in 2 hours to stretch"))
        self.assertEqual(classify_intent("remind me in 2 hours to stretch"), "reminder")

    def test_note_plus_remind_compound(self):
        t = "note: wifi is abc and remind me tomorrow to pay rent"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "life")


class PolicyD2Tests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(policy_for("list.create"), "auto")
        self.assertEqual(policy_for("list.append"), "auto")
        self.assertEqual(policy_for("list.clear"), "auto")
        self.assertEqual(policy_for("list.bought"), "auto")
        self.assertEqual(policy_for("cal.create"), "confirm")
        self.assertEqual(policy_for("cal.list"), "auto")
        self.assertEqual(policy_for("note.create"), "auto")
        self.assertEqual(policy_for("note.read"), "auto")


class LifeRegistryD2Tests(unittest.TestCase):
    def test_tools_registered(self):
        llm = _load_llm_client()
        names = set(llm.registered_tool_names("life"))
        self.assertTrue(D1.issubset(names))
        self.assertTrue(D2_LIST.issubset(names))
        self.assertTrue(D2_CAL.issubset(names))
        self.assertTrue(D2_NOTES.issubset(names))
        self.assertNotIn("run_shell_command", names)
        for n in names:
            self.assertIn(n, llm.TOOL_POLICY_KEYS, n)
        self.assertEqual(llm.TOOL_POLICY_KEYS["create_calendar_event"], "cal.create")
        self.assertEqual(llm.TOOL_POLICY_KEYS["add_list_items"], "list.append")
        self.assertEqual(llm.TOOL_POLICY_KEYS["add_note"], "note.create")
        prompt = llm.build_system_prompt("life")
        self.assertIn("calendar", prompt.lower())
        self.assertIn("list", prompt.lower())

    def test_life_tool_names_set(self):
        lt = _load_life_tools()
        self.assertTrue(D1.issubset(lt.LIFE_TOOL_NAMES))
        self.assertTrue(D2_LIST.issubset(lt.LIFE_TOOL_NAMES))
        self.assertTrue(D2_CAL.issubset(lt.LIFE_TOOL_NAMES))
        self.assertTrue(D2_NOTES.issubset(lt.LIFE_TOOL_NAMES))

    def test_cal_create_stages_pending(self):
        lt = _load_life_tools()
        with mock.patch.object(lt, "_set_cal_pending") as sp:
            out = lt.create_calendar_event_tool(
                db_user_id=5,
                chat_id="929388047",
                title="Dentist",
                starts_at_iso="2026-09-29T10:00:00+00:00",
                confirmed=False,
            )
        self.assertIn("PENDING_CONFIRM", out)
        self.assertTrue(sp.called)

    def test_list_ops_in_memory_fallback(self):
        """Without Redis, list ops should soft-fail gracefully."""
        lt = _load_life_tools()
        with mock.patch.object(lt, "_redis", return_value=None):
            out = lt.create_shopping_list(chat_id="c-d2", title="Groceries", items=[{"name": "milk", "qty": 1}])
            # save is no-op without redis but still returns created message
            self.assertIn("Groceries", out)
            shown = lt.show_shopping_list(chat_id="c-d2")
            self.assertIn("No list", shown)

    def test_quiet_strip(self):
        raw = "✅ Added milk. I can also check the VPS and Shnuk."
        out = quiet_strip_completion(raw, agent_role="life", status="completed")
        self.assertNotIn("✅", out)
        self.assertNotIn("VPS", out)


class OrchD2Tests(unittest.TestCase):
    def test_orch_mentions_agenda(self):
        src = (ROOT / "services" / "orchestrator" / "app" / "main.py").read_text()
        self.assertIn('"agenda"', src)
        self.assertIn("shopping list", src)
        self.assertNotIn("ops_loop", src)


class PhaseABCSmoke(unittest.TestCase):
    def test_finance_still(self):
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")

    def test_start_help(self):
        self.assertEqual(classify_intent("/start", has_active_list=True, is_collecting=True), "help")


if __name__ == "__main__":
    unittest.main()
