"""Life OS slice 5: tool/policy registry + life-reflect + regression smoke."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# Only ingress on sys.path as `app` — worker/scheduler also have app/ and would
# shadow (worker ships app/__init__.py). Load those via importlib instead.
sys.path.insert(0, str(ROOT / "services" / "telegram-ingress"))
sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

try:
    import redis  # noqa: F401
except ImportError:
    fake = types.ModuleType("redis")

    class _R:
        pass

    fake.Redis = _R
    sys.modules["redis"] = fake

from app.side_effect_policy import (  # noqa: E402
    POLICY_TABLE,
    policy_for,
)
from app.quiet_mode import quiet_strip_completion, quiet_strip_chat  # noqa: E402


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_slice5"
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


def _load_life_reflect_jobs():
    path = ROOT / "services" / "scheduler" / "app" / "life_reflect_jobs.py"
    name = "life_reflect_jobs_slice5"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class RegistryPatternTests(unittest.TestCase):
    def test_unknown_action_defaults_confirm(self):
        self.assertEqual(policy_for("totally.unknown.action.xyz"), "confirm")
        self.assertNotIn("totally.unknown.action.xyz", POLICY_TABLE)

    def test_life_reflect_policy_keys(self):
        self.assertEqual(policy_for("memory.recall"), "auto")
        self.assertEqual(policy_for("life.reflect"), "auto")
        self.assertEqual(policy_for("life.reflect.notify"), "auto")

    def test_prior_domains_still_green(self):
        expected = {
            "list.create": "auto",
            "finance.write": "confirm",
            "finance.read": "auto",
            "ops.shell_read": "confirm",
            "memory.forget": "confirm",
            "memory.write": "auto",
            "task.create": "auto",
            "reminder.create": "auto",
            "cal.create": "confirm",
            "cal.list": "auto",
            "note.create": "auto",
            "note.read": "auto",
            "policy.other_apps": "refuse",
        }
        for key, want in expected.items():
            self.assertEqual(policy_for(key), want, key)

    def test_llm_registry_life_reflect_tools_only(self):
        llm = _load_llm_client()
        names = llm.registered_tool_names("life-reflect")
        self.assertEqual(sorted(names), ["notify_user", "recall_memory"])
        self.assertNotIn("run_shell_command", names)
        self.assertEqual(llm.registered_tool_names("coder"), ["run_shell_command"])
        self.assertEqual(llm.registered_tool_names("memory-writer"), ["save_memory_items"])
        ops = set(llm.registered_tool_names("ops-reflect"))
        self.assertEqual(ops, {"run_shell_command", "notify_user"})

    def test_every_registered_tool_has_policy_key_doc(self):
        llm = _load_llm_client()
        for name in llm.registered_tool_names():
            self.assertIn(name, llm.TOOL_POLICY_KEYS, name)

    def test_register_tool_idempotent(self):
        llm = _load_llm_client()
        before = len(llm.tools_for_role("life-reflect"))
        llm.register_tool("life-reflect", llm.NOTIFY_USER_SCHEMA)
        self.assertEqual(len(llm.tools_for_role("life-reflect")), before)


class QuietLifeReflectTests(unittest.TestCase):
    def test_life_reflect_gets_quiet_strip(self):
        raw = "✅ Reminder: dentist tomorrow. I can also check the VPS and Shnuk."
        out = quiet_strip_completion(raw, agent_role="life-reflect", status="completed")
        self.assertNotIn("✅", out)
        self.assertNotIn("Shnuk", out)
        self.assertNotIn("VPS", out)
        self.assertIn("dentist", out.lower())

    def test_hey_still_quiet(self):
        self.assertEqual(quiet_strip_chat("Hey! What's up?"), "Hey! What's up?")


class LifeReflectJobTests(unittest.TestCase):
    def test_job_constants_and_disabled_short_circuit(self):
        mod = _load_life_reflect_jobs()
        self.assertEqual(mod.LIFE_REFLECT_JOB_ID, "life-reflect-periodic")
        with mock.patch.object(mod, "LIFE_REFLECT_ENABLED", False):
            stats = mod.run_life_reflect(force=False)
        self.assertEqual(stats["skipped_disabled"], 1)
        self.assertEqual(stats["dispatched"], 0)

    def test_snapshot_mentions_no_finance_digest(self):
        mod = _load_life_reflect_jobs()
        src = Path(mod.__file__).read_text()
        self.assertIn("Do NOT restate weekly/monthly money digests", src)
        self.assertIn("recall_memory", src)
        self.assertIn("life-reflect", src)
        self.assertNotIn("publish_finance_digests", src)

    def test_register_idempotent_signature(self):
        mod = _load_life_reflect_jobs()
        sched = mock.Mock()
        sched.get_job.return_value = None
        fake_aps = types.ModuleType("apscheduler")
        fake_triggers = types.ModuleType("apscheduler.triggers")
        fake_cron = types.ModuleType("apscheduler.triggers.cron")

        class CronTrigger:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        fake_cron.CronTrigger = CronTrigger
        sys.modules["apscheduler"] = fake_aps
        sys.modules["apscheduler.triggers"] = fake_triggers
        sys.modules["apscheduler.triggers.cron"] = fake_cron
        with mock.patch.object(mod, "LIFE_REFLECT_ENABLED", True):
            mod.register_life_reflect_jobs(sched)
        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        self.assertEqual(kwargs["id"], mod.LIFE_REFLECT_JOB_ID)
        self.assertTrue(kwargs["replace_existing"])


class FinanceDigestUntouchedTests(unittest.TestCase):
    def test_digest_job_ids_still_sunday_month(self):
        path = ROOT / "services" / "scheduler" / "app" / "finance_digest_jobs.py"
        src = path.read_text()
        self.assertIn('WEEKLY_JOB_ID = "finance-digest-weekly"', src)
        self.assertIn('MONTHLY_JOB_ID = "finance-digest-monthly"', src)
        self.assertIn('day_of_week="sun"', src)
        self.assertIn("hour=13", src)
        self.assertIn("day=1", src)
        self.assertIn("hour=4", src)


class DocsPresentTests(unittest.TestCase):
    def test_registry_doc_exists(self):
        doc = ROOT / "docs" / "tool_policy_registry.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        self.assertIn("POLICY_TABLE", text)
        self.assertIn("Unknown", text)
        self.assertIn("life-reflect", text)
        self.assertIn("PARKED", text)


if __name__ == "__main__":
    unittest.main()
