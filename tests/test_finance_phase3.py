"""Unit tests for Celia Phase 3 finance helpers (no DB / no network)."""
from __future__ import annotations

import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.finance_parse import (  # noqa: E402
    parse_savings_contribute,
    parse_new_goal,
    parse_fixed_set,
    is_goals_list,
    is_fixed_list,
    is_flex_query,
    parse_digest_period,
    looks_like_finance,
    looks_like_phase3_finance,
    parse_finance,
    parse_set_budget,
)
from app.finance_digest import (  # noqa: E402
    build_digest_text,
    build_goals_list_text,
    build_fixed_list_text,
    build_flex_text,
)


def _fmt(x):
    return f"{Decimal(str(x)).quantize(Decimal('0.01')):.2f}"


class SavingsParseTests(unittest.TestCase):
    def test_save_toward(self):
        p = parse_savings_contribute("save 500 toward emergency")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.amount_mvr, 500.0)
        self.assertEqual(p.goal_hint.lower(), "emergency")

    def test_contribute_to(self):
        p = parse_savings_contribute("contribute 200 to vacation")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.amount_mvr, 200.0)
        self.assertEqual(p.goal_hint.lower(), "vacation")

    def test_does_not_steal_spent(self):
        self.assertIsNone(parse_savings_contribute("spent 85 on groceries at Agora"))


class GoalParseTests(unittest.TestCase):
    def test_new_goal(self):
        p = parse_new_goal("new goal emergency 10000")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name.lower(), "emergency")
        self.assertEqual(p.target_mvr, 10000.0)
        self.assertEqual(p.monthly_target_mvr, 0.0)

    def test_goal_monthly(self):
        p = parse_new_goal("goal vacation 5000 monthly 500")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name.lower(), "vacation")
        self.assertEqual(p.target_mvr, 5000.0)
        self.assertEqual(p.monthly_target_mvr, 500.0)


class FixedParseTests(unittest.TestCase):
    def test_fixed_set(self):
        p = parse_fixed_set("fixed rent 12000")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name.lower(), "rent")
        self.assertEqual(p.amount_mvr, 12000.0)

    def test_fixed_list_not_set(self):
        self.assertIsNone(parse_fixed_set("/fixed"))
        self.assertTrue(is_fixed_list("/fixed"))


class ListIntentTests(unittest.TestCase):
    def test_goals(self):
        self.assertTrue(is_goals_list("/goals"))
        self.assertTrue(is_goals_list("savings"))

    def test_flex(self):
        self.assertTrue(is_flex_query("/flex"))
        self.assertTrue(is_flex_query("variable left"))

    def test_digest(self):
        self.assertEqual(parse_digest_period("/digest"), "month")
        self.assertEqual(parse_digest_period("/digest week"), "week")
        self.assertEqual(parse_digest_period("digest monthly"), "month")

    def test_looks_like(self):
        self.assertTrue(looks_like_phase3_finance("save 500 toward emergency"))
        self.assertTrue(looks_like_finance("save 500 toward emergency"))
        self.assertTrue(looks_like_finance("/flex"))


class DigestTextTests(unittest.TestCase):
    def test_digest_with_spend(self):
        msg = build_digest_text(
            period="month",
            total_expense=1500,
            expense_count=3,
            by_kind={"fixed": Decimal("200"), "variable": Decimal("1300"), "other": Decimal("0")},
            top_categories=[
                {"name": "Food", "spent": Decimal("800")},
                {"name": "Transport", "spent": Decimal("500")},
            ],
            fixed_obligations=12000,
            variable_spend=1300,
            goals=[
                {
                    "name": "emergency",
                    "saved_mvr": Decimal("500"),
                    "target_mvr": Decimal("10000"),
                    "is_active": True,
                }
            ],
            fmt_mvr=_fmt,
        )
        self.assertIn("1500.00", msg)
        self.assertIn("Food", msg)
        self.assertIn("emergency", msg)
        self.assertIn("/digest", msg)  # soft footer

    def test_goals_list_empty(self):
        msg = build_goals_list_text([], _fmt)
        self.assertIn("No savings goals", msg)

    def test_goals_list(self):
        msg = build_goals_list_text(
            [
                {
                    "name": "vacation",
                    "saved_mvr": Decimal("1000"),
                    "target_mvr": Decimal("5000"),
                    "monthly_target_mvr": Decimal("500"),
                    "is_active": True,
                }
            ],
            _fmt,
        )
        self.assertIn("vacation", msg)
        self.assertIn("20%", msg)

    def test_fixed_list(self):
        msg = build_fixed_list_text(
            [{"name": "rent", "amount_mvr": Decimal("12000"), "category_name": "Rent"}],
            Decimal("12000"),
            _fmt,
        )
        self.assertIn("rent", msg)
        self.assertIn("12000.00", msg)

    def test_flex_with_income(self):
        msg = build_flex_text(
            fixed_total=12000,
            variable_spend=3000,
            income_month=25000,
            fmt_mvr=_fmt,
        )
        self.assertIn("Fixed so far", msg)
        self.assertIn("Income logged this month", msg)
        self.assertIn("That leaves about", msg)
        self.assertIn("10000.00", msg)  # 25000 - 12000 - 3000

    def test_flex_no_income(self):
        msg = build_flex_text(
            fixed_total=12000,
            variable_spend=500,
            income_month=0,
            fmt_mvr=_fmt,
        )
        self.assertIn("No income logged this month yet", msg)
        self.assertIn("log it when it lands", msg)
        self.assertNotIn("That leaves about", msg)
        self.assertNotIn("fixed rent", msg)

    def test_flex_empty(self):
        msg = build_flex_text(
            fixed_total=0,
            variable_spend=0,
            income_month=0,
            fmt_mvr=_fmt,
        )
        self.assertIn("Nothing on the books yet for flex", msg)
        self.assertNotIn("fixed rent", msg)
        self.assertNotIn("12000", msg)

    def test_flex_over_income(self):
        msg = build_flex_text(
            fixed_total=12000,
            variable_spend=8000,
            income_month=15000,
            fmt_mvr=_fmt,
        )
        self.assertIn("over vs income", msg)
        self.assertIn("5000.00", msg)
        self.assertNotIn("That leaves about -", msg)

    def test_set_budget_parse(self):
        p = parse_set_budget("set food budget 3000")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.category_hint, "Food")
        self.assertEqual(p.amount_mvr, 3000.0)
        p2 = parse_set_budget("food limit 3000")
        self.assertIsNotNone(p2)
        p3 = parse_set_budget("/budget transport 1500")
        self.assertIsNotNone(p3)
        assert p3 is not None
        self.assertEqual(p3.category_hint, "Transport")
        self.assertTrue(looks_like_phase3_finance("set food budget 3000"))

    def test_maldives_aliases(self):
        cases = [
            ("spent 40 on stealgas", "Utilities"),
            ("spent 200 fenaka", "Utilities"),
            ("spent 150 mwsc", "Utilities"),
            ("spent 50 grab", "Transport"),
            ("spent 80 bolt", "Transport"),
            ("spent 120 ferry", "Transport"),
            ("spent 200 speedboat", "Transport"),
            ("spent 300 island hopper", "Transport"),
            ("spent 90 at Agora", "Food"),
            ("spent 200 fantasia", "Shopping"),
            ("spent 40 pharmacy", "Health"),
            ("spent 25 netflix", "Entertainment"),
            ("income 8000 salary", "Salary"),
            ("income 500 bonus", "Salary"),
            ("spent 120 dhiraagu", "Utilities"),
            ("spent 90 ooredoo", "Utilities"),
            ("spent 60 scooter", "Transport"),
            ("spent 450 seaplane", "Transport"),
            ("spent 80 dentist", "Health"),
            ("spent 150 shoes", "Shopping"),
            ("spent 35 disney", "Entertainment"),
            ("spent 12000 housing", "Rent"),
        ]
        for raw, expect in cases:
            p = parse_finance(raw)
            self.assertIsNotNone(p, raw)
            assert p is not None
            self.assertEqual(p.category_hint, expect, raw)


class Phase1StillWorks(unittest.TestCase):
    def test_spent_nl(self):
        p = parse_finance("spent 85 on groceries at Agora")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.amount_mvr, 85.0)


if __name__ == "__main__":
    if not (ROOT / "app" / "__init__.py").exists():
        (ROOT / "app").mkdir(exist_ok=True)
        (ROOT / "app" / "__init__.py").write_text("")
    unittest.main()
