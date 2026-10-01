"""
HTTP-level tests for the suspension review endpoints.

The critical property is that the safety rules are enforced SERVER-SIDE: a
crafted request must not be able to suspend an account that is already
suspended, because that is what protects the abuse cases from being
relabelled as billing.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from config import settings
import suspension as suspension_mod


class FakeProvisioner:
    """Records calls and reports a scripted state."""

    state = None
    calls = []
    suspend_result = {"success": True, "message": "suspended ok"}

    def account_state(self, username):
        return self.state

    def suspend_account(self, username, reason="billing", confirm=False):
        FakeProvisioner.calls.append((username, reason, confirm))
        return dict(FakeProvisioner.suspend_result)


class SuspendAPITest(unittest.TestCase):
    def setUp(self):
        FakeProvisioner.state = {"panel": "cpanel", "username": "x", "domain": "x.bt",
                                 "suspended": False, "reason": "not suspended"}
        FakeProvisioner.calls = []
        FakeProvisioner.suspend_result = {"success": True, "message": "suspended ok"}

        # The suspend endpoint is destructive, so it sits behind
        # require_token_for_destructive, which refuses (503) when no token is
        # configured at all. These tests therefore set a token and present it
        # rather than clearing it.
        self._orig_token = settings.API_AUTH_TOKEN
        self.TOKEN = "test-token"
        settings.API_AUTH_TOKEN = self.TOKEN

        self._cp = app_module.get_cpanel_provisioner
        self._da = app_module.get_da_provisioner
        app_module.get_cpanel_provisioner = lambda: FakeProvisioner()
        app_module.get_da_provisioner = lambda: FakeProvisioner()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.get_cpanel_provisioner = self._cp
        app_module.get_da_provisioner = self._da
        settings.API_AUTH_TOKEN = self._orig_token

    def _suspend(self, **data):
        data.setdefault("confirm", "true")
        return self.client.post("/api/v1/suspension/suspend", data=data,
                                headers={"X-API-Token": self.TOKEN})

    # -- gating -----------------------------------------------------------
    def test_requires_explicit_confirm(self):
        r = self._suspend(panel="cpanel", username="x", confirm="false")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(FakeProvisioner.calls, [])

    def test_invalid_panel_rejected(self):
        r = self._suspend(panel="plesk", username="x", confirm="true")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(FakeProvisioner.calls, [])

    def test_unknown_account_is_404(self):
        FakeProvisioner.state = None
        r = self._suspend(panel="cpanel", username="x", confirm="true")
        self.assertEqual(r.status_code, 404)

    # -- the safety rule --------------------------------------------------
    def test_already_suspended_is_refused(self):
        """The headline rule, enforced server-side so the UI cannot bypass it."""
        FakeProvisioner.state = {"panel": "cpanel", "username": "x", "domain": "x.bt",
                                 "suspended": True, "reason": "abuse"}
        r = self._suspend(panel="cpanel", username="x", confirm="true")
        self.assertEqual(r.status_code, 409)
        self.assertIn("abuse", r.json()["detail"])
        self.assertEqual(FakeProvisioner.calls, [],
                         "must not even call the panel for an already-suspended account")

    def test_suspension_reason_cannot_be_overridden_to_billing_on_suspended_account(self):
        FakeProvisioner.state = {"panel": "directadmin", "username": "y", "domain": "y.bt",
                                 "suspended": True, "reason": "spam"}
        r = self._suspend(panel="directadmin", username="y", reason="billing", confirm="true")
        self.assertEqual(r.status_code, 409)
        self.assertIn("spam", r.json()["detail"])
        self.assertEqual(FakeProvisioner.calls, [])

    def test_active_account_is_suspended(self):
        r = self._suspend(panel="cpanel", username="x", confirm="true")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertEqual(FakeProvisioner.calls, [("x", "billing", True)])

    def test_panel_failure_surfaces(self):
        FakeProvisioner.suspend_result = {"success": False, "message": "whmapi1 exploded"}
        r = self._suspend(panel="cpanel", username="x", confirm="true")
        self.assertEqual(r.status_code, 500)
        self.assertIn("exploded", r.json()["detail"])

    def test_requires_token_when_configured(self):
        settings.API_AUTH_TOKEN = "tok"
        try:
            r = self._suspend(panel="cpanel", username="x", confirm="true")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(FakeProvisioner.calls, [])
        finally:
            settings.API_AUTH_TOKEN = None

    # -- report -----------------------------------------------------------
    def test_report_without_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = settings.SUSPENSION_AUDIT_LOG
            settings.SUSPENSION_AUDIT_LOG = str(Path(tmp) / "none.jsonl")
            try:
                r = self.client.get("/api/v1/suspension/report", headers={"X-API-Token": self.TOKEN})
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["available"])
            finally:
                settings.SUSPENSION_AUDIT_LOG = orig

    def test_report_summarises_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            record = {
                "generated_at": "2026-09-27T02:17:00+00:00",
                "total_accounts": 355,
                "bscs_complete": True,
                "bscs_note": "",
                "counts": {"suspend": 2, "skip_already_suspended_billing": 121,
                           "skip_suspended_other_reason": 15, "skip_no_bscs_match": 217},
                "unmatched_contracts": [{"contract": "CONTR1", "name_field": "A B",
                                         "domains_found": []}],
                "decisions": [
                    {"panel": "cpanel", "username": "a", "domain": "a.bt", "action": "suspend",
                     "contract": "CONTR2", "reason": "", "current_state": "not suspended",
                     "bscs_customer": "www.a.bt"},
                    {"panel": "cpanel", "username": "b", "domain": "b.bt",
                     "action": "skip_no_bscs_match", "reason": "", "current_state": "",
                     "contract": "", "bscs_customer": ""},
                ],
            }
            log.write_text(json.dumps(record) + "\n")
            orig = settings.SUSPENSION_AUDIT_LOG
            settings.SUSPENSION_AUDIT_LOG = str(log)
            try:
                r = self.client.get("/api/v1/suspension/report", headers={"X-API-Token": self.TOKEN})
                d = r.json()
                self.assertTrue(d["available"])
                self.assertEqual(len(d["candidates"]), 1)
                self.assertEqual(d["candidates"][0]["username"], "a")
                self.assertEqual(d["already_suspended_billing"], 121)
                self.assertEqual(d["suspended_other_reason"], 15)
                self.assertEqual(len(d["unmatched_contracts"]), 1)
            finally:
                settings.SUSPENSION_AUDIT_LOG = orig

    def test_report_reports_age_and_staleness(self):
        """A failed run writes no record, so without an age the dashboard would
        show a week-old list as if it were current."""
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        base = {"total_accounts": 1, "bscs_complete": True, "bscs_note": "",
                "counts": {}, "unmatched_contracts": [],
                "decisions": [{"panel": "cpanel", "username": "a", "domain": "a.bt",
                               "action": "suspend", "contract": "", "reason": "",
                               "current_state": "", "bscs_customer": ""}]}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            orig = settings.SUSPENSION_AUDIT_LOG
            try:
                log.write_text(json.dumps({**base, "generated_at": fresh}) + "\n")
                settings.SUSPENSION_AUDIT_LOG = str(log)
                d = self.client.get("/api/v1/suspension/report",
                                    headers={"X-API-Token": self.TOKEN}).json()
                self.assertFalse(d["stale"])
                self.assertIsNotNone(d["age_hours"])
                self.assertLess(d["age_hours"], 1)

                log.write_text(json.dumps({**base, "generated_at": old}) + "\n")
                settings.SUSPENSION_AUDIT_LOG = str(log)
                d = self.client.get("/api/v1/suspension/report",
                                    headers={"X-API-Token": self.TOKEN}).json()
                self.assertTrue(d["stale"], "a 40h-old report must be flagged stale")
                self.assertGreater(d["age_hours"], 26)
            finally:
                settings.SUSPENSION_AUDIT_LOG = orig

    def test_report_flags_incomplete_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "audit.jsonl"
            log.write_text(json.dumps({
                "generated_at": "x", "total_accounts": 1, "bscs_complete": False,
                "bscs_note": "25 of 63", "counts": {}, "unmatched_contracts": [],
                "decisions": [{"panel": "cpanel", "username": "a", "domain": "a.bt",
                               "action": "suspend", "contract": "", "reason": "",
                               "current_state": "", "bscs_customer": ""}],
            }) + "\n")
            orig = settings.SUSPENSION_AUDIT_LOG
            settings.SUSPENSION_AUDIT_LOG = str(log)
            try:
                d = self.client.get("/api/v1/suspension/report", headers={"X-API-Token": self.TOKEN}).json()
                self.assertFalse(d["bscs_complete"])
                self.assertIn("25 of 63", d["bscs_note"])
            finally:
                settings.SUSPENSION_AUDIT_LOG = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheReportCarriesTheHeartbeat(unittest.TestCase):
    """
    The report is what the dashboard reads, so a heartbeat the endpoint does not
    surface is the same as no heartbeat at all.

    This class exists because it caught a real bug: the endpoint raised
    AttributeError on every request, so the whole suspension card 500'd, and the
    full suite still passed -- nothing exercised this path.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        import app as app_module
        self.client = TestClient(app_module.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "heartbeat.json"
        p = patch.object(suspension_mod, "heartbeat_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)

    def _get(self):
        # The endpoint is token-gated and the environment has a real token set,
        # so send the configured one rather than a made-up string.
        return self.client.get(
            "/api/v1/suspension/report",
            headers={"X-API-Token": settings.API_AUTH_TOKEN or "t"})

    def test_the_endpoint_works_with_no_heartbeat_at_all(self):
        """The first run on a fresh deployment. Must be 200, not 500."""
        r = self._get()
        self.assertEqual(r.status_code, 200, r.text[:200])
        d = r.json()
        self.assertIsNone(d.get("last_attempt_at"))
        self.assertFalse(d.get("last_attempt_ok"))

    def test_a_recent_good_attempt_is_reported_as_good(self):
        from datetime import datetime, timezone
        self.path.write_text(json.dumps({
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "ok": True, "detail": "completed"}))
        d = self._get().json()
        self.assertTrue(d["last_attempt_ok"])
        self.assertFalse(d["no_attempt_since"])
        self.assertEqual(d["last_attempt_detail"], "completed")

    def test_a_recent_failed_attempt_is_reported_as_failed(self):
        from datetime import datetime, timezone
        self.path.write_text(json.dumps({
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "ok": False, "detail": "crashed: RuntimeError: boom",
            "traceback": "Traceback...\nRuntimeError: boom"}))
        d = self._get().json()
        self.assertFalse(d["last_attempt_ok"])
        self.assertIn("RuntimeError", d["last_attempt_detail"])
        self.assertIn("RuntimeError", d["last_attempt_traceback"])

    def test_no_attempt_in_a_long_time_is_called_out_separately(self):
        """The case in the screenshot: the list is stale and nothing has tried to
        refresh it, so the container or host is not up at 02:17. That is a
        different problem from a job that ran and failed."""
        from datetime import datetime, timedelta, timezone
        self.path.write_text(json.dumps({
            "attempted_at": (datetime.now(timezone.utc)
                             - timedelta(hours=66)).isoformat(),
            "ok": True, "detail": "completed"}))
        d = self._get().json()
        self.assertTrue(d["no_attempt_since"])
        self.assertGreater(d["last_attempt_age_hours"], 26)

    def test_the_age_is_computed_from_the_heartbeat_not_the_audit(self):
        """They are different moments: the audit record is the last run that got
        as far as deciding something."""
        from datetime import datetime, timedelta, timezone
        self.path.write_text(json.dumps({
            "attempted_at": (datetime.now(timezone.utc)
                             - timedelta(hours=5)).isoformat(),
            "ok": False, "detail": "failed"}))
        d = self._get().json()
        self.assertGreater(d["last_attempt_age_hours"], 4.5)
        self.assertLess(d["last_attempt_age_hours"], 6)
