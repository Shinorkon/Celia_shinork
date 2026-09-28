"""Scheduler DELETE /jobs/{id} must be idempotent (no HTTP 500 on missing job)."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "services" / "scheduler" / "app" / "main.py"


class CancelJobSourceContractTests(unittest.TestCase):
    def test_cancel_job_catches_job_lookup_error(self):
        src = MAIN.read_text()
        self.assertIn("from apscheduler.jobstores.base import JobLookupError", src)
        tree = ast.parse(src)
        cancel = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "cancel_job":
                cancel = node
                break
        self.assertIsNotNone(cancel, "cancel_job not found")
        assert cancel is not None
        handlers = [
            h
            for h in ast.walk(cancel)
            if isinstance(h, ast.ExceptHandler)
        ]
        self.assertTrue(handlers, "cancel_job has no except handler")
        names = []
        for h in handlers:
            if h.type is None:
                names.append("bare")
            elif isinstance(h.type, ast.Name):
                names.append(h.type.id)
            elif isinstance(h.type, ast.Attribute):
                names.append(h.type.attr)
        self.assertIn("JobLookupError", names)

    def test_cancel_job_still_sets_cancelled_status(self):
        src = MAIN.read_text()
        # Narrow to cancel_job body after decorator
        start = src.index("def cancel_job")
        end = src.find("\n\n", start)
        body = src[start:end if end > start else start + 400]
        self.assertIn("_set_job_status(job_id, \"cancelled\")", body)
        self.assertIn("remove_job", body)


class CancelJobLogicTests(unittest.TestCase):
    """Pure logic mirror of cancel_job — no FastAPI/DB import needed."""

    @staticmethod
    def _cancel(scheduler, set_status, job_id: str, JobLookupError) -> dict:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        set_status(job_id, "cancelled")
        return {"job_id": job_id, "status": "cancelled"}

    def test_missing_job_returns_cancelled_not_raise(self):
        class JobLookupError(Exception):
            pass

        sched = mock.Mock()
        sched.remove_job.side_effect = JobLookupError("gone")
        set_status = mock.Mock()
        out = self._cancel(sched, set_status, "abc", JobLookupError)
        self.assertEqual(out, {"job_id": "abc", "status": "cancelled"})
        set_status.assert_called_once_with("abc", "cancelled")

    def test_present_job_removed_then_cancelled(self):
        class JobLookupError(Exception):
            pass

        sched = mock.Mock()
        set_status = mock.Mock()
        out = self._cancel(sched, set_status, "xyz", JobLookupError)
        self.assertEqual(out["status"], "cancelled")
        sched.remove_job.assert_called_once_with("xyz")
        set_status.assert_called_once_with("xyz", "cancelled")


if __name__ == "__main__":
    unittest.main()
