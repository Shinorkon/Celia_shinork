"""Unit tests for Celia Phase 2 finance helpers (no DB / no network)."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

# Allow importing app.* from telegram-ingress
ROOT = Path(__file__).resolve().parents[1]
APP_PARENT = ROOT / "app"
# tests live beside a copy of app modules under celia_phase2/app
sys.path.insert(0, str(ROOT))

import types
try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.finance_vision import parse_vision_json  # noqa: E402
from app.finance_alerts import alert_crossed, format_budget_alert  # noqa: E402
from app.finance_handlers import should_try_receipt, _confirm_copy  # noqa: E402
from app.finance_parse import ParsedFinance, parse_finance  # noqa: E402


class VisionJsonTests(unittest.TestCase):
    def test_plain_json(self):
        r = parse_vision_json(
            '{"amount": 185.5, "merchant": "STO", "date": "2026-09-17", '
            '"category_hint": "Food", "note": "groceries", "confidence": 0.92}'
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.amount, 185.5)
        self.assertEqual(r.merchant, "STO")
        self.assertEqual(r.date, "2026-09-17")
        self.assertEqual(r.category_hint, "Food")
        self.assertAlmostEqual(r.confidence, 0.92)

    def test_fenced_json(self):
        r = parse_vision_json(
            'Sure!\n```json\n{"amount": 40, "merchant": "Agora", "date": null, '
            '"category_hint": "Food", "note": "", "confidence": 0.8}\n```'
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.amount, 40.0)
        self.assertIsNone(r.date)
        self.assertAlmostEqual(r.confidence, 0.8)

    def test_confidence_percent(self):
        r = parse_vision_json(
            '{"amount": 10, "merchant": "X", "date": null, '
            '"category_hint": "Other", "note": "", "confidence": 85}'
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertAlmostEqual(r.confidence, 0.85)

    def test_missing_amount(self):
        r = parse_vision_json(
            '{"amount": null, "merchant": "", "date": null, '
            '"category_hint": "Other", "note": "", "confidence": 0.2}'
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertIsNone(r.amount)
        self.assertLess(r.confidence, 0.5)

    def test_garbage(self):
        self.assertIsNone(parse_vision_json("not json at all"))
        self.assertIsNone(parse_vision_json(""))


class AlertThresholdTests(unittest.TestCase):
    def test_cross_80(self):
        self.assertEqual(alert_crossed(70, 15, 100), "80")

    def test_cross_100(self):
        self.assertEqual(alert_crossed(90, 15, 100), "100")

    def test_cross_both_prefers_100(self):
        # 70 + 40 = 110 crosses both 80 and 100 → 100
        self.assertEqual(alert_crossed(70, 40, 100), "100")

    def test_already_over_80_no_repeat(self):
        self.assertIsNone(alert_crossed(85, 5, 100))

    def test_already_at_100_no_repeat(self):
        self.assertIsNone(alert_crossed(100, 10, 100))
        self.assertIsNone(alert_crossed(110, 5, 100))

    def test_zero_limit(self):
        self.assertIsNone(alert_crossed(0, 50, 0))

    def test_exact_80(self):
        self.assertEqual(alert_crossed(70, 10, 100), "80")

    def test_format_80(self):
        msg = format_budget_alert("Food", 82, 100, "80", lambda x: f"{Decimal(str(x)):.2f}")
        self.assertIn("Heads up", msg)
        self.assertIn("Food", msg)
        self.assertIn("82%", msg)

    def test_format_100(self):
        msg = format_budget_alert("Food", 105, 100, "100", lambda x: f"{Decimal(str(x)):.2f}")
        self.assertIn("tapped out", msg)


class ReceiptHeuristicTests(unittest.TestCase):
    def test_image_only(self):
        self.assertTrue(should_try_receipt("", True))

    def test_receipt_cue(self):
        self.assertTrue(should_try_receipt("here's the receipt", True))

    def test_finance_caption(self):
        self.assertTrue(should_try_receipt("spent 85 at Agora", True))

    def test_meme_caption(self):
        self.assertFalse(should_try_receipt("lol this meme is wild", True))

    def test_no_image(self):
        self.assertFalse(should_try_receipt("", False))


class ParseStillWorks(unittest.TestCase):
    def test_spent_nl(self):
        p = parse_finance("spent 85 on groceries at Agora")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.amount_mvr, 85.0)
        self.assertEqual(p.tx_type, "expense")

    def test_confirm_copy_receipt(self):
        p = ParsedFinance(
            amount_mvr=185.0,
            tx_type="expense",
            category_hint="Food",
            merchant="STO",
            note="groceries",
            raw="[receipt]",
        )
        msg = _confirm_copy(p, "Food", from_receipt=True)
        self.assertIn("From the receipt", msg)
        self.assertIn("185.00", msg)
        self.assertIn("STO", msg)


if __name__ == "__main__":
    # finance_handlers imports finance_store (psycopg) — provide stub if missing
    try:
        import psycopg  # noqa: F401
    except ImportError:
        import types

        stub = types.ModuleType("psycopg")
        sys.modules["psycopg"] = stub

    # Point imports at local app package layout:
    # celia_phase2/app/*.py but package name is `app`
    # Ensure ROOT is on path and app is a package
    if not (ROOT / "app" / "__init__.py").exists():
        (ROOT / "app" / "__init__.py").write_text("")

    unittest.main()
