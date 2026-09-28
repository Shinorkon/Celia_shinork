"""Phase D ops multi-step: policy, schemas, allowlist, ingress routing."""
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
sys.path.insert(0, str(ROOT / "services" / "worker-runtime" / "app"))

try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.side_effect_policy import (  # noqa: E402
    POLICY_TABLE,
    classify_action,
    policy_for,
)
from app.ops_handlers import (  # noqa: E402
    clear_memory_for_tests,
    try_handle_ops,
)
from app.quiet_mode import quiet_strip_completion  # noqa: E402


def _load_llm_client():
    path = ROOT / "services" / "worker-runtime" / "app" / "llm_client.py"
    name = "llm_client_phase_d_ops"
    if name in sys.modules:
        return sys.modules[name]
    if "httpx" not in sys.modules:
        sys.modules["httpx"] = types.ModuleType("httpx")
        sys.modules["httpx"].Client = object  # type: ignore
        sys.modules["httpx"].Timeout = object  # type: ignore
        sys.modules["httpx"].HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_ops_tools():
    path = ROOT / "services" / "worker-runtime" / "app" / "ops_tools.py"
    name = "ops_tools_phase_d_ops"
    if name in sys.modules:
        return sys.modules[name]
    if "httpx" not in sys.modules:
        hx = types.ModuleType("httpx")
        hx.Client = object  # type: ignore
        sys.modules["httpx"] = hx
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class OpsPolicyTests(unittest.TestCase):
    def test_shell_read_auto(self):
        self.assertEqual(policy_for("ops.shell_read"), "auto")
        action, pol = classify_action("ops", "how's Celia looking — docker status?")
        self.assertEqual(action, "ops.shell_read")
        self.assertEqual(pol, "auto")

    def test_shell_write_confirm(self):
        action, pol = classify_action("ops", "restart aop-worker")
        self.assertEqual(action, "ops.shell_write")
        self.assertEqual(pol, "confirm")

    def test_deploy_confirm(self):
        action, pol = classify_action("ops", "deploy the celia stack")
        self.assertEqual(action, "ops.deploy")
        self.assertEqual(pol, "confirm")

    def test_refuse_other_apps(self):
        for text in (
            "restart shnuk api",
            "check oreuda containers",
            "docker ps budget-tracker",
            "look at directors eye",
            "check shino-chan",
        ):
            action, pol = classify_action("ops", text)
            self.assertEqual(pol, "refuse", msg=text)
            self.assertEqual(action, "policy.other_apps", msg=text)

    def test_unknown_defaults_confirm(self):
        self.assertEqual(policy_for("ops.brand_new_thing"), "confirm")
        self.assertIn("ops.shell_read", POLICY_TABLE)


class OpsSchemaTests(unittest.TestCase):
    def test_ops_role_tools_registered(self):
        llm = _load_llm_client()
        names = set(llm.registered_tool_names("ops"))
        for expected in (
            "ops_stack_status",
            "ops_service_health",
            "ops_host_resources",
            "ops_container_logs",
            "ops_edge_status",
            "ops_restart_container",
        ):
            self.assertIn(expected, names)
        # No raw shell on ops role — scoped tools only
        self.assertNotIn("run_shell_command", names)

    def test_policy_keys_for_ops_tools(self):
        llm = _load_llm_client()
        for tool, key in (
            ("ops_stack_status", "ops.shell_read"),
            ("ops_service_health", "ops.shell_read"),
            ("ops_restart_container", "ops.shell_write"),
        ):
            self.assertEqual(llm.TOOL_POLICY_KEYS.get(tool), key)


class OpsAllowlistTests(unittest.TestCase):
    def test_allowed_containers(self):
        ot = _load_ops_tools()
        self.assertTrue(ot.is_allowed_container("aop-worker"))
        self.assertTrue(ot.is_allowed_container("celia-redis"))
        self.assertFalse(ot.is_allowed_container("shnuk_api"))
        self.assertFalse(ot.is_allowed_container("oreuda_db"))

    def test_restart_requires_confirm(self):
        ot = _load_ops_tools()
        calls: list[str] = []

        def runner(cmd: str) -> str:
            calls.append(cmd)
            return "ok"

        out = ot.ops_restart_container(runner, "aop-worker", mutate_confirmed=False)
        self.assertIn("CONFIRM_REQUIRED", out)
        self.assertEqual(calls, [])

        out2 = ot.ops_restart_container(runner, "aop-worker", mutate_confirmed=True)
        self.assertTrue(calls)
        self.assertIn("docker restart aop-worker", calls[0])

    def test_logs_refuse_other(self):
        ot = _load_ops_tools()
        out = ot.ops_container_logs(lambda c: c, "shnuk_api")
        self.assertIn("Refused", out)


class OpsIngressTests(unittest.TestCase):
    def setUp(self):
        clear_memory_for_tests()
        self.replies: list[str] = []

        def send(chat_id, text, thread_id=""):
            self.replies.append(text)
            return True

        self.send = send
        self.chat = "test-chat-d-ops"

    def _handle(self, text: str):
        return try_handle_ops(
            text=text,
            chat_id=self.chat,
            telegram_user_id=1,
            thread_id="",
            chat_type="private",
            send=self.send,
        )

    def test_read_auto_dispatches_ops_agent(self):
        with mock.patch("app.ops_handlers._dispatch_ops_agent") as disp:
            reason = self._handle("how's Celia looking — check docker")
            self.assertEqual(reason, "ops_auto_dispatched")
            disp.assert_called_once()
            kwargs = disp.call_args.kwargs if disp.call_args.kwargs else {}
            # positional: text, chat_id, thread_id, user_id, mutate_confirmed
            args = disp.call_args[0]
            self.assertIn("docker", args[0].lower() or "how's")
            self.assertFalse(args[4] if len(args) > 4 else kwargs.get("mutate_confirmed", False))
        self.assertTrue(any("Checking" in r or "On it" in r for r in self.replies))

    def test_restart_asks_confirm(self):
        reason = self._handle("restart aop-worker")
        self.assertEqual(reason, "ops_pending_confirm")
        self.assertTrue(any("proceed" in r.lower() or "want me" in r.lower() for r in self.replies))

    def test_restart_yes_dispatches_mutate(self):
        self._handle("restart aop-worker")
        self.replies.clear()
        with mock.patch("app.ops_handlers._dispatch_ops_agent") as disp:
            reason = self._handle("yes")
            self.assertEqual(reason, "ops_confirmed_dispatched")
            args = disp.call_args[0]
            kwargs = disp.call_args.kwargs
            self.assertTrue(kwargs.get("mutate_confirmed", args[4] if len(args) > 4 else False))

    def test_refuse_shnuk(self):
        reason = self._handle("restart shnuk on the vps")
        self.assertEqual(reason, "ops_refused")

    def test_quiet_keeps_ops_status_language(self):
        text = "aop-worker is up. Disk is fine."
        out = quiet_strip_completion(text, agent_role="ops", status="completed")
        self.assertIn("aop-worker", out)


class OrchOpsRouteTests(unittest.TestCase):
    def test_preferred_ops_in_source(self):
        src = (ROOT / "services" / "orchestrator" / "app" / "main.py").read_text()
        self.assertIn('"ops"', src)
        self.assertIn('preferred == "ops"', src)


if __name__ == "__main__":
    unittest.main()
