"""Receipt batch quiet replies + amount preference + multi-image debounce."""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# When run from /workspace/celia-fix, parents[1] is /workspace — mirror VPS layout.
if (ROOT / "services" / "telegram-ingress").is_dir():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))
else:
    # Local scratch: modules sit beside this test file.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

# Stub finance_store.fmt_mvr if full store unavailable
from app.finance_parse import (  # noqa: E402
    looks_like_amount_preference_rule,
    wants_breakdown,
    wants_total_only,
    looks_like_receipt_flow_text,
)
from app.debounce import (  # noqa: E402
    BufferedUpdate,
    ChatDebouncer,
    collect_buffered_images,
    merge_buffered_images,
    MEDIA_GROUP_DEBOUNCE_MS,
)
from app.finance_handlers import (  # noqa: E402
    _append_session_receipt,
    _batch_totals_copy,
    _mark_total_only,
    _session_totals_reply,
    _short_batch_summary,
    clear_finance_session_for_tests,
    try_handle_finance,
)


class ParseHelpersTests(unittest.TestCase):
    def test_wants_breakdown(self):
        self.assertTrue(wants_breakdown("show me the list"))
        self.assertTrue(wants_breakdown("break it down"))
        self.assertFalse(wants_breakdown("just the total"))

    def test_amount_pref_rule(self):
        sample = (
            "If a receipt has an amount but I have a separate text with a. "
            "Lower amount Count the lower amount"
        )
        self.assertTrue(looks_like_amount_preference_rule(sample))
        self.assertTrue(looks_like_receipt_flow_text(sample))
        self.assertFalse(looks_like_amount_preference_rule("spent 50 at Agora"))

    def test_total_only_bare(self):
        self.assertTrue(wants_total_only("total"))
        self.assertTrue(wants_total_only("just the total"))
        self.assertTrue(wants_total_only("what's the sum"))


class ShortSummaryTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = mock.patch(
            "app.finance_handlers._redis", return_value=None
        )
        self.redis_patch.start()
        clear_finance_session_for_tests()

    def tearDown(self):
        self.redis_patch.stop()
        clear_finance_session_for_tests()

    def test_short_vs_long(self):
        receipts = [
            {"amount": 113.5, "merchant": "Yelee Supermart", "category": "Food"},
            {"amount": 200.0, "merchant": "Bizaara", "category": "Food"},
        ]
        short = _short_batch_summary(receipts)
        self.assertIn("2 receipts", short)
        self.assertIn("313.50", short)
        self.assertNotIn("Yelee", short)
        long = _batch_totals_copy(receipts)
        self.assertIn("Yelee", long)
        self.assertIn("Sum (2)", long)
        self.assertEqual(_session_totals_reply(receipts, "total"), short)
        self.assertEqual(
            _session_totals_reply(receipts, "show me the list"), long
        )


class AmountPrefHandlerTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = mock.patch(
            "app.finance_handlers._redis", return_value=None
        )
        self.redis_patch.start()
        clear_finance_session_for_tests()
        self.sent: list[str] = []

        def send(cid, msg, tid=""):
            self.sent.append(msg)
            return True

        self.send = send

    def tearDown(self):
        self.redis_patch.stop()
        clear_finance_session_for_tests()

    def test_pref_short_confirm_no_list_dump(self):
        chat = "88001"
        _mark_total_only(chat, calc_mode=True)
        _append_session_receipt(chat, 113.5, "Yelee Supermart", "Food")
        _append_session_receipt(chat, 200.0, "Bizaara", "Food")
        with mock.patch(
            "app.finance_handlers._persist_amount_pref_memory"
        ):
            reason = try_handle_finance(
                text=(
                    "If a receipt has an amount but I have a separate text with a. "
                    "Lower amount Count the lower amount"
                ),
                chat_id=chat,
                telegram_user_id=1,
                thread_id="",
                chat_type="private",
                send=self.send,
            )
        self.assertEqual(reason, "finance_amount_pref_saved")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("lower text amount", self.sent[0].lower())
        self.assertNotIn("Yelee", self.sent[0])
        self.assertNotIn("Sum (", self.sent[0])

    def test_total_ask_is_short(self):
        chat = "88002"
        _mark_total_only(chat, calc_mode=True)
        _append_session_receipt(chat, 50, "A", "Food")
        _append_session_receipt(chat, 75.5, "B", "Food")
        reason = try_handle_finance(
            text="just the total",
            chat_id=chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )
        self.assertEqual(reason, "finance_receipt_totals")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 receipts", self.sent[0])
        self.assertNotIn("A —", self.sent[0])


class CollectImagesTests(unittest.TestCase):
    def test_collect_keeps_all(self):
        updates = [
            BufferedUpdate(1, "c", "", "private", "", image_data_url="data:a"),
            BufferedUpdate(1, "c", "", "private", "", image_data_url="data:b"),
            BufferedUpdate(1, "c", "", "private", "hi"),
        ]
        self.assertEqual(collect_buffered_images(updates), ["data:a", "data:b"])
        self.assertEqual(merge_buffered_images(updates), "data:b")

    def test_media_group_longer_debounce(self):
        flushed: list = []
        done = threading.Event()

        def on_flush(chat_id, items):
            flushed.append(items)
            done.set()

        d = ChatDebouncer(
            on_flush, delay_ms=150, media_group_delay_ms=400
        )
        d.add(
            BufferedUpdate(
                1, "42", "", "private", "", image_data_url="data:1", media_group_id="g1"
            )
        )
        time.sleep(0.2)  # past short delay, still inside media-group delay
        d.add(
            BufferedUpdate(
                1, "42", "", "private", "", image_data_url="data:2", media_group_id="g1"
            )
        )
        self.assertTrue(done.wait(2.0))
        self.assertEqual(len(flushed), 1)
        self.assertEqual(len(flushed[0]), 2)
        self.assertGreaterEqual(MEDIA_GROUP_DEBOUNCE_MS, 2000)
        d.cancel_all()


class MultiImageBatchTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = mock.patch(
            "app.finance_handlers._redis", return_value=None
        )
        self.redis_patch.start()
        clear_finance_session_for_tests()
        self.sent: list[str] = []

        def send(cid, msg, tid=""):
            self.sent.append(msg)
            return True

        self.send = send

    def tearDown(self):
        self.redis_patch.stop()
        clear_finance_session_for_tests()

    def test_multi_image_one_reply(self):
        from app.finance_vision import ReceiptVisionResult

        chat = "88003"
        _mark_total_only(chat, calc_mode=True)

        visions = [
            ReceiptVisionResult(
                amount=10.0, merchant="A", date=None, category_hint="Food",
                note="", confidence=0.9,
            ),
            ReceiptVisionResult(
                amount=20.0, merchant="B", date=None, category_hint="Food",
                note="", confidence=0.9,
            ),
            ReceiptVisionResult(
                amount=30.0, merchant="C", date=None, category_hint="Food",
                note="", confidence=0.9,
            ),
        ]
        call = {"i": 0}

        def fake_vision(_url):
            i = call["i"]
            call["i"] += 1
            return visions[i]

        with mock.patch(
            "app.finance_handlers.extract_receipt_from_image", side_effect=fake_vision
        ), mock.patch(
            "app.finance_handlers._save_receipt_image", return_value=None
        ):
            reason = try_handle_finance(
                text="",
                chat_id=chat,
                telegram_user_id=1,
                thread_id="",
                chat_type="private",
                send=self.send,
                image_data_urls=["data:1", "data:2", "data:3"],
            )
        self.assertEqual(reason, "finance_receipt_batched")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 receipts", self.sent[0])
        self.assertIn("60.00", self.sent[0])
        self.assertNotIn("A —", self.sent[0])


if __name__ == "__main__":
    unittest.main()
