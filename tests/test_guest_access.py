"""Guest chat-only access: ingress allow + mutating tools refused."""
from __future__ import annotations

import os
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

# Seed env before importing guest_access / main helpers
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "929388047"
os.environ["GUEST_TELEGRAM_USER_IDS"] = "1210484792"

# Re-import fresh modules under test
import importlib

import app.guest_access as guest_access  # noqa: E402
importlib.reload(guest_access)

from app.side_effect_policy import classify_action  # noqa: E402


OWNER = 929388047
GUEST = 1210484792
STRANGER = 999888777


class GuestAccessTests(unittest.TestCase):
    def test_owner_is_owner_not_guest(self):
        self.assertTrue(guest_access.is_owner(OWNER))
        self.assertFalse(guest_access.is_guest(OWNER))

    def test_guest_env_authorized_for_chat(self):
        self.assertTrue(guest_access.is_guest_env(GUEST))
        self.assertTrue(guest_access.is_guest(GUEST))
        self.assertTrue(guest_access.is_chat_authorized(GUEST))

    def test_stranger_not_authorized(self):
        with mock.patch.object(guest_access, "is_guest_db", return_value=False):
            with mock.patch.object(guest_access, "is_authorized_full", return_value=False):
                self.assertFalse(guest_access.is_chat_authorized(STRANGER))

    def test_guest_chat_action_auto(self):
        action, base = classify_action("chat", "Yo buddy")
        self.assertEqual(action, "chat.reply")
        pol = guest_access.enforce_user_policy(GUEST, action, base)
        self.assertEqual(pol, "auto")

    def test_guest_ops_refused(self):
        action, base = classify_action("ops", "check docker on the vps")
        self.assertTrue(action.startswith("ops.") or action.startswith("policy."))
        pol = guest_access.enforce_user_policy(GUEST, action, base)
        self.assertEqual(pol, "refuse")

    def test_guest_finance_write_allowed_confirm(self):
        """Guests may log their OWN expenses (confirm); never ops/server."""
        action, base = classify_action("finance", "I spent 50 on lunch")
        self.assertEqual(action, "finance.write")
        pol = guest_access.enforce_user_policy(GUEST, action, base)
        self.assertEqual(pol, "confirm")

    def test_guest_ssh_shell_refused(self):
        for text in (
            "ssh into the server",
            "restart aop-worker",
            "deploy celia",
            "cat /root/Celia/.env.prod",
        ):
            action, base = classify_action("ops", text)
            pol = guest_access.enforce_user_policy(GUEST, action, base)
            self.assertEqual(pol, "refuse", msg=text)

    def test_guest_memory_wipe_refused(self):
        action, base = classify_action("memory", "forget everything")
        self.assertEqual(action, "memory.forget_all")
        pol = guest_access.enforce_user_policy(GUEST, action, base)
        self.assertEqual(pol, "refuse")

    def test_owner_ops_unchanged(self):
        action, base = classify_action("ops", "check docker on the vps")
        pol = guest_access.enforce_user_policy(OWNER, action, base)
        self.assertEqual(pol, base)
        self.assertEqual(pol, "auto")

    def test_guest_not_on_owner_allowlist(self):
        self.assertNotIn(GUEST, guest_access.OWNER_TELEGRAM_USER_IDS)
        self.assertIn(GUEST, guest_access.GUEST_TELEGRAM_USER_IDS)

    def test_guest_relay_refused(self):
        action, base = classify_action("relay", "Will you pass on a message to Raaish?")
        self.assertEqual(action, "comms.third_party")
        self.assertEqual(base, "confirm")
        pol = guest_access.enforce_user_policy(GUEST, action, base)
        self.assertEqual(pol, "refuse")

    def test_owner_relay_stays_confirm(self):
        action, base = classify_action("relay", "Will you pass on a message to Raaish?")
        pol = guest_access.enforce_user_policy(OWNER, action, base)
        self.assertEqual(pol, "confirm")

    def test_stranger_relay_not_authorized(self):
        with mock.patch.object(guest_access, "is_guest_db", return_value=False):
            with mock.patch.object(guest_access, "is_authorized_full", return_value=False):
                self.assertFalse(guest_access.is_chat_authorized(STRANGER))
                pol = guest_access.enforce_user_policy(
                    STRANGER, "comms.third_party", "confirm"
                )
                self.assertEqual(pol, "refuse")


if __name__ == "__main__":
    unittest.main()
