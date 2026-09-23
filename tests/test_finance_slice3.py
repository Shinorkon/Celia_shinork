"""Life OS slice 3: Redis finance receipt sessions + merchant entity link."""
from __future__ import annotations

import json
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

from app.side_effect_policy import POLICY_TABLE, classify_action, policy_for  # noqa: E402
from app.finance_handlers import (  # noqa: E402
    _SESSION_TTL_SEC,
    _append_session_receipt,
    _get_finance_session,
    _mark_total_only,
    _session_key,
    clear_finance_session_for_tests,
    should_try_receipt,
)
from app.memory_store import upsert_entity  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
            self.ttls.pop(k, None)
        return n

    def scan_iter(self, match=None, count=100):
        prefix = (match or "*").rstrip("*")
        for k in list(self.store):
            if k.startswith(prefix):
                yield k


class FinanceSessionRedisTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedis()
        self.redis_patch = mock.patch(
            "app.finance_handlers._redis", return_value=self.fake
        )
        self.redis_patch.start()
        clear_finance_session_for_tests()

    def tearDown(self):
        self.redis_patch.stop()
        clear_finance_session_for_tests()

    def test_session_key_shape(self):
        self.assertEqual(_session_key("123"), "celia:finance:session:123")
        self.assertGreaterEqual(_SESSION_TTL_SEC, 40 * 60)

    def test_mark_and_append_persist_redis(self):
        chat = "9001"
        _mark_total_only(chat, calc_mode=True)
        key = _session_key(chat)
        self.assertIn(key, self.fake.store)
        self.assertEqual(self.fake.ttls.get(key), _SESSION_TTL_SEC)
        raw = json.loads(self.fake.store[key])
        self.assertTrue(raw["total_only"])
        self.assertTrue(raw["calc_mode"])
        self.assertEqual(raw["receipts"], [])

        _append_session_receipt(chat, 85.5, "Agora", "Food")
        raw2 = json.loads(self.fake.store[key])
        self.assertEqual(len(raw2["receipts"]), 1)
        self.assertEqual(raw2["receipts"][0]["merchant"], "Agora")
        self.assertAlmostEqual(raw2["receipts"][0]["amount"], 85.5)

    def test_survives_mem_clear_via_redis(self):
        chat = "9002"
        _mark_total_only(chat, calc_mode=True)
        _append_session_receipt(chat, 40, "STO", "Food")
        # Simulate ingress process restart: wipe in-process fallback only.
        from app import finance_handlers as fh

        with fh._SESSION_LOCK:
            fh._MEM_SESSIONS.clear()
        s = _get_finance_session(chat)
        self.assertTrue(s["calc_mode"])
        self.assertEqual(len(s["receipts"]), 1)
        self.assertEqual(s["receipts"][0]["merchant"], "STO")

    def test_should_try_receipt_uses_session(self):
        chat = "9003"
        self.assertFalse(should_try_receipt("hello", False, chat))
        _mark_total_only(chat, calc_mode=True)
        self.assertTrue(should_try_receipt("", True, chat))


class FinanceSessionMemFallbackTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = mock.patch(
            "app.finance_handlers._redis", return_value=None
        )
        self.redis_patch.start()
        clear_finance_session_for_tests()

    def tearDown(self):
        self.redis_patch.stop()
        clear_finance_session_for_tests()

    def test_mem_fallback_works(self):
        chat = "9100"
        _append_session_receipt(chat, 12, "X", "Other")
        s = _get_finance_session(chat)
        self.assertEqual(len(s["receipts"]), 1)


class MerchantUpsertTests(unittest.TestCase):
    def test_blank_name_returns_none(self):
        self.assertIsNone(
            upsert_entity(db_user_id=1, entity_type="merchant", canonical_name="  ")
        )

    def test_upsert_sql_shape(self):
        fake_cur = mock.MagicMock()
        fake_cur.fetchone.return_value = (42,)
        fake_conn = mock.MagicMock()
        fake_conn.__enter__ = mock.MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = mock.MagicMock(return_value=False)
        fake_conn.cursor.return_value.__enter__ = mock.MagicMock(return_value=fake_cur)
        fake_conn.cursor.return_value.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("app.memory_store._conn", return_value=fake_conn):
            eid = upsert_entity(
                db_user_id=7,
                entity_type="merchant",
                canonical_name="  Agora  Mart ",
                attrs={"source": "finance"},
            )
        self.assertEqual(eid, 42)
        sql = fake_cur.execute.call_args[0][0]
        self.assertIn("INSERT INTO memory_entities", sql)
        self.assertIn("ON CONFLICT", sql)
        params = fake_cur.execute.call_args[0][1]
        self.assertEqual(params[0], 7)
        self.assertEqual(params[1], "merchant")
        self.assertEqual(params[2], "Agora Mart")


class RegressionSmoke(unittest.TestCase):
    def test_finance_policy_unchanged(self):
        self.assertEqual(POLICY_TABLE["finance.write"], "confirm")
        self.assertEqual(POLICY_TABLE["finance.read"], "auto")
        a, p = classify_action("finance", "spent 10 at Agora")
        self.assertEqual(a, "finance.write")
        self.assertEqual(p, "confirm")

    def test_list_memory_task_still_present(self):
        self.assertEqual(policy_for("list.create"), "auto")
        self.assertEqual(policy_for("memory.forget"), "confirm")
        self.assertIn("task.create", POLICY_TABLE)
        self.assertEqual(policy_for("task.create"), "auto")

    def test_other_apps_refuse(self):
        a, p = classify_action("ops", "restart budget-tracker")
        self.assertEqual(p, "refuse")


if __name__ == "__main__":
    unittest.main()
