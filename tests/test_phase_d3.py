"""Phase D3: life agent memory + finance session tools + compound routing."""
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
)
from app.side_effect_policy import policy_for  # noqa: E402
from app.quiet_mode import quiet_strip_completion  # noqa: E402


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_phase_d3"
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
    name = "life_tools_phase_d3"
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


D3_MEMORY = {
    "memory_remember",
    "memory_recall",
    "memory_forget",
    "memory_correct",
}
D3_FINANCE = {
    "set_lower_text_amount_pref",
    "recalculate_receipts",
}


class CompoundMemoryRoutingTests(unittest.TestCase):
    def test_remember_plus_remind(self):
        t = "remember I hate cilantro and remind me tomorrow to buy milk"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(life_domain_flags(t), {"memory", "reminder_task"})
        self.assertEqual(classify_intent(t), "life")

    def test_simple_remember_not_compound(self):
        t = "remember my wifi password is abc"
        self.assertFalse(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "memory")

    def test_what_do_you_know(self):
        self.assertEqual(classify_intent("what do you know about me"), "memory")
        self.assertIn("memory", life_domain_flags("what do you know about me"))

    def test_finance_session_alone_stays_finance(self):
        t = "prefer the lower text amount"
        self.assertFalse(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "finance")
        self.assertEqual(life_domain_flags(t), {"finance_session"})


class PolicyD3Tests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(policy_for("memory.write"), "auto")
        self.assertEqual(policy_for("memory.recall"), "auto")
        self.assertEqual(policy_for("memory.forget"), "confirm")
        self.assertEqual(policy_for("memory.correct"), "confirm")
        self.assertEqual(policy_for("memory.forget_all"), "confirm")
        self.assertEqual(policy_for("finance.amount_pref"), "auto")
        self.assertEqual(policy_for("finance.recalculate"), "auto")


class LifeRegistryD3Tests(unittest.TestCase):
    def test_tools_registered(self):
        llm = _load_llm_client()
        names = set(llm.registered_tool_names("life"))
        self.assertTrue(D3_MEMORY.issubset(names))
        self.assertTrue(D3_FINANCE.issubset(names))
        self.assertEqual(llm.TOOL_POLICY_KEYS["memory_remember"], "memory.write")
        self.assertEqual(llm.TOOL_POLICY_KEYS["memory_recall"], "memory.recall")
        self.assertEqual(llm.TOOL_POLICY_KEYS["memory_forget"], "memory.forget")
        self.assertEqual(llm.TOOL_POLICY_KEYS["memory_correct"], "memory.correct")
        self.assertEqual(llm.TOOL_POLICY_KEYS["set_lower_text_amount_pref"], "finance.amount_pref")
        self.assertEqual(llm.TOOL_POLICY_KEYS["recalculate_receipts"], "finance.recalculate")
        for n in D3_MEMORY | D3_FINANCE:
            self.assertIn(n, llm.TOOL_POLICY_KEYS, n)
        prompt = llm.build_system_prompt("life")
        self.assertIn("memory", prompt.lower())

    def test_life_tool_names_set(self):
        lt = _load_life_tools()
        self.assertTrue(D3_MEMORY.issubset(lt.LIFE_TOOL_NAMES))
        self.assertTrue(D3_FINANCE.issubset(lt.LIFE_TOOL_NAMES))

    def test_forget_stages_pending(self):
        lt = _load_life_tools()

        class FakeCur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return (42, "oat milk", "I prefer oat milk")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCur()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(lt, "_conn", return_value=FakeConn()):
            with mock.patch.object(lt, "_set_mem_pending") as sp:
                out = lt.memory_forget_tool(
                    db_user_id=5,
                    chat_id="929388047",
                    query="oat milk",
                    confirmed=False,
                )
        self.assertIn("PENDING_CONFIRM", out)
        self.assertTrue(sp.called)
        payload = sp.call_args[0][1]
        self.assertEqual(payload["action"], "forget")
        self.assertEqual(payload["target_id"], 42)

    def test_correct_stages_pending(self):
        lt = _load_life_tools()

        class FakeCur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return (7, "budget", "budget is 2000")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCur()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(lt, "_conn", return_value=FakeConn()):
            with mock.patch.object(lt, "_set_mem_pending") as sp:
                out = lt.memory_correct_tool(
                    db_user_id=5,
                    chat_id="929388047",
                    query="budget",
                    new_body="budget is 3000",
                    confirmed=False,
                )
        self.assertIn("PENDING_CONFIRM", out)
        self.assertTrue(sp.called)
        payload = sp.call_args[0][1]
        self.assertEqual(payload["action"], "correct")
        self.assertEqual(payload["body"], "budget is 3000")

    def test_lower_text_pref_session(self):
        lt = _load_life_tools()
        sess = {
            "total_only": False,
            "calc_mode": False,
            "prefer_lower_text_amount": False,
            "receipts": [],
            "updated_at": 0,
        }
        with mock.patch.object(lt, "_get_finance_session", return_value=dict(sess)):
            with mock.patch.object(lt, "_save_finance_session") as save:
                out = lt.set_lower_text_amount_pref_tool(chat_id="929388047", enabled=True)
        self.assertIn("lower text", out.lower())
        self.assertTrue(save.called)
        saved = save.call_args[0][1]
        self.assertTrue(saved["prefer_lower_text_amount"])

    def test_recalculate_empty(self):
        lt = _load_life_tools()
        with mock.patch.object(
            lt,
            "_get_finance_session",
            return_value={
                "prefer_lower_text_amount": True,
                "receipts": [],
                "total_only": False,
                "calc_mode": False,
                "updated_at": 0,
            },
        ):
            out = lt.recalculate_receipts_tool(chat_id="929388047")
        self.assertIn("Nothing parked", out)

    def test_recalculate_with_dual_amounts(self):
        lt = _load_life_tools()
        receipts = [
            {"amount": 100.0, "vision_amount": 100.0, "text_amount": 80.0},
            {"amount": 50.0, "vision_amount": 50.0, "text_amount": None},
        ]
        with mock.patch.object(
            lt,
            "_get_finance_session",
            return_value={
                "prefer_lower_text_amount": True,
                "receipts": receipts,
                "total_only": False,
                "calc_mode": False,
                "updated_at": 0,
            },
        ):
            with mock.patch.object(lt, "_save_finance_session") as save:
                out = lt.recalculate_receipts_tool(chat_id="929388047")
        self.assertIn("Updated", out)
        self.assertIn("MVR 130.00", out)  # min(100,80)+50
        self.assertTrue(save.called)

    def test_quiet_strip(self):
        raw = "✅ Remembered. I can also check the VPS."
        out = quiet_strip_completion(raw, agent_role="life", status="completed")
        self.assertNotIn("✅", out)
        self.assertNotIn("VPS", out)


class OrchD3Tests(unittest.TestCase):
    def test_orch_mentions_memory(self):
        src = (ROOT / "services" / "orchestrator" / "app" / "main.py").read_text()
        self.assertIn('"remember"', src)
        self.assertIn("what do you know", src)
        self.assertIn("recalculate", src)
        self.assertNotIn("ops_loop", src)


class IngressSkipCompoundTests(unittest.TestCase):
    def test_main_skips_memory_on_compound(self):
        src = (ROOT / "services" / "telegram-ingress" / "app" / "main.py").read_text()
        self.assertIn("not is_compound_life_request(text):\n        memory_reason = try_handle_memory", src)
        self.assertIn('"memory"', src)  # preferred_role includes memory


class DocsD3Tests(unittest.TestCase):
    def test_registry_mentions_d3(self):
        docs = (ROOT / "docs" / "tool_policy_registry.md").read_text()
        self.assertIn("Phase D3", docs)
        self.assertIn("memory_remember", docs)
        self.assertIn("set_lower_text_amount_pref", docs)


class PhaseABCSmoke(unittest.TestCase):
    def test_finance_spend_still(self):
        self.assertEqual(classify_intent("spent 50 at Agora"), "finance")

    def test_list_plus_remind_still(self):
        t = "add milk to the list and remind me at 5"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "life")


if __name__ == "__main__":
    unittest.main()
