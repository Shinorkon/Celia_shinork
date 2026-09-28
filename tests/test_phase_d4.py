"""Phase D4: life finance writes (confirm) + compound spend+remind + bare know."""
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


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_phase_d4"
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
    name = "life_tools_phase_d4"
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


D4_WRITE = {"log_spend", "record_expense", "set_budget"}
D4_READ = {"spent_summary"}


class CompoundFinanceRoutingTests(unittest.TestCase):
    def test_spend_plus_remind(self):
        t = "spent 50 on lunch and remind me tomorrow"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(life_domain_flags(t), {"finance", "reminder_task"})
        self.assertEqual(classify_intent(t), "life")

    def test_clear_spend_stays_finance(self):
        t = "spent 50 at Agora"
        self.assertFalse(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "finance")

    def test_budget_plus_remind(self):
        t = "set food budget 3000 and remind me Friday"
        self.assertTrue(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "life")

    def test_session_pref_alone_finance(self):
        t = "prefer the lower text amount"
        self.assertFalse(is_compound_life_request(t))
        self.assertEqual(classify_intent(t), "finance")


class BareKnowTests(unittest.TestCase):
    def test_bare_what_do_you_know(self):
        self.assertEqual(classify_intent("what do you know"), "memory")
        self.assertIn("memory", life_domain_flags("what do you know"))

    def test_about_me_still(self):
        self.assertEqual(classify_intent("what do you know about me"), "memory")


class PolicyD4Tests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(policy_for("finance.write"), "confirm")
        self.assertEqual(policy_for("finance.read"), "auto")


class LifeRegistryD4Tests(unittest.TestCase):
    def test_tools_registered(self):
        llm = _load_llm_client()
        names = set(llm.registered_tool_names("life"))
        self.assertTrue(D4_WRITE.issubset(names))
        self.assertTrue(D4_READ.issubset(names))
        self.assertEqual(llm.TOOL_POLICY_KEYS["log_spend"], "finance.write")
        self.assertEqual(llm.TOOL_POLICY_KEYS["record_expense"], "finance.write")
        self.assertEqual(llm.TOOL_POLICY_KEYS["set_budget"], "finance.write")
        self.assertEqual(llm.TOOL_POLICY_KEYS["spent_summary"], "finance.read")
        prompt = llm.build_system_prompt("life")
        self.assertIn("log_spend", prompt)

    def test_life_tool_names(self):
        lt = _load_life_tools()
        self.assertTrue(D4_WRITE.issubset(lt.LIFE_TOOL_NAMES))
        self.assertTrue(D4_READ.issubset(lt.LIFE_TOOL_NAMES))

    def test_log_spend_stages_pending(self):
        lt = _load_life_tools()
        with mock.patch.object(lt, "_seed_finance_categories"):
            with mock.patch.object(lt, "_resolve_finance_category", return_value=(1, "Food")):
                with mock.patch.object(lt, "_finance_create_pending", return_value=99) as cp:
                    out = lt.log_spend_tool(
                        db_user_id=5,
                        chat_id="929388047",
                        telegram_user_id="929388047",
                        amount_mvr=50,
                        merchant="Agora",
                        category="Food",
                        confirmed=False,
                    )
        self.assertIn("PENDING_CONFIRM", out)
        self.assertTrue(cp.called)
        payload = cp.call_args.kwargs["payload"]
        self.assertEqual(payload["amount_mvr"], 50.0)
        self.assertEqual(payload["merchant"], "Agora")
        self.assertEqual(payload["tx_type"], "expense")

    def test_set_budget_stages_pending(self):
        lt = _load_life_tools()
        with mock.patch.object(lt, "_seed_finance_categories"):
            with mock.patch.object(lt, "_resolve_finance_category", return_value=(2, "Food")):
                with mock.patch.object(lt, "_finance_create_pending", return_value=7) as cp:
                    out = lt.set_budget_tool(
                        db_user_id=5,
                        chat_id="929388047",
                        telegram_user_id="929388047",
                        category="Food",
                        amount_mvr=3000,
                        confirmed=False,
                    )
        self.assertIn("PENDING_CONFIRM", out)
        self.assertEqual(cp.call_args.kwargs["payload"]["kind"], "set_budget")

    def test_spent_summary_empty(self):
        lt = _load_life_tools()

        class FakeCur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return (0, 0)

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
            out = lt.spent_summary_tool(db_user_id=5, period="month")
        self.assertIn("Nothing logged", out)


class FinanceHandlerSkipTests(unittest.TestCase):
    def test_handler_skips_compound(self):
        src = (ROOT / "services" / "telegram-ingress" / "app" / "finance_handlers.py").read_text()
        self.assertIn("is_compound_life_request(text)", src)
        self.assertIn("return None", src)


class DocsD4Tests(unittest.TestCase):
    def test_registry(self):
        docs = (ROOT / "docs" / "tool_policy_registry.md").read_text()
        self.assertIn("Phase D4", docs)
        self.assertIn("log_spend", docs)
        self.assertIn("Phase D life-agent build order complete", docs)


class PhaseRegression(unittest.TestCase):
    def test_list_plus_remind(self):
        t = "add milk to the list and remind me at 5"
        self.assertEqual(classify_intent(t), "life")

    def test_remember_plus_remind(self):
        t = "remember I hate cilantro and remind me tomorrow to buy milk"
        self.assertEqual(classify_intent(t), "life")


if __name__ == "__main__":
    unittest.main()
