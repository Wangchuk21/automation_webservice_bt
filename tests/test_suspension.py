"""
Tests for the suspension decision logic.

These are the tests that matter most in this repository. The rules here decide
whether a live customer's website gets switched off, so each one is written to
fail loudly if the policy is weakened:

  * an account already suspended for a non-billing reason is never touched
  * an existing reason is never overwritten
  * absence of a billing match never becomes a suspension
  * an incomplete billing list aborts the whole run
  * a panel with no handler aborts rather than being skipped

decide() is pure, so none of this needs a server. Run with:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import suspension
from suspension import (
    SKIP_ACTIVE, SKIP_ALREADY_BILLING, SKIP_NO_MATCH, SKIP_OTHER_REASON,
    SUSPEND, RunReport, SuspensionError, decide, execute, extract_domains,
    is_billing_reason, write_audit,
)


def _report():
    """The smallest thing write_audit will accept."""
    return RunReport(total_accounts=0, bscs_complete=True)


def acct(panel="cpanel", username="u", domain="d.bt", suspended=False, reason=""):
    return {"panel": panel, "username": username, "domain": domain,
            "suspended": suspended, "reason": reason}


def lapsed(domains, complete=True, note=""):
    return {
        "domains": set(domains),
        "complete": complete,
        "note": note,
        "rows": [{"contract": "CONTR1", "customer_code": "c", "public_key": "k",
                  "name_field": ",".join(domains), "domains": list(domains)}],
    }


class TestDomainExtraction(unittest.TestCase):
    def test_repeated_www_tokens_deduplicate(self):
        self.assertEqual(extract_domains("www.test.bt, www.test.bt, www.test.bt"), ["test.bt"])

    def test_mixed_with_person_name(self):
        self.assertEqual(extract_domains("Dorji Bhutan, --, www.test.bt"), ["test.bt"])

    def test_person_name_yields_nothing(self):
        self.assertEqual(extract_domains("Jamtsho, Karma"), [])
        self.assertEqual(extract_domains(""), [])
        self.assertEqual(extract_domains("   "), [])

    def test_unknown_tld_rejected(self):
        # Not a .bt domain, so it must not become a suspension target.
        self.assertEqual(extract_domains("www.example.com"), [])

    def test_known_bt_suffixes_accepted(self):
        for d in ("wons.bt", "x.com.bt", "x.org.bt", "x.edu.bt", "x.gov.bt"):
            self.assertEqual(extract_domains(f"www.{d}"), [d], d)

    def test_placeholders_rejected(self):
        for junk in ("--", "null", "N/A", "-"):
            self.assertEqual(extract_domains(junk), [])


class TestBillingReasonClassification(unittest.TestCase):
    def test_known_billing_reasons(self):
        for panel, reason in [("cpanel", "pending bills"), ("cpanel", "1 year pending bill"),
                              ("cpanel", "billing issue"), ("cpanel", "Payment due"),
                              ("directadmin", "billing")]:
            self.assertTrue(is_billing_reason(panel, reason), f"{panel}/{reason}")

    def test_non_billing_reasons(self):
        for panel, reason in [("directadmin", "abuse"), ("directadmin", "spam"),
                              ("directadmin", "user_bandwidth"), ("cpanel", "compromised"),
                              ("cpanel", "forwarding"), ("cpanel", "Surrendered"),
                              ("cpanel", "forwarded")]:
            self.assertFalse(is_billing_reason(panel, reason), f"{panel}/{reason}")

    def test_unknown_reason_is_not_billing(self):
        """Fail safe: an unrecognised reason must never be treated as billing."""
        for reason in ("banana", "mystery", "2026", "x" * 200):
            self.assertFalse(is_billing_reason("cpanel", reason), reason)
            self.assertFalse(is_billing_reason("directadmin", reason), reason)

    def test_active_sentinels_are_not_reasons(self):
        for s in ("", "not suspended", "none", "null", "NOT SUSPENDED"):
            self.assertFalse(is_billing_reason("cpanel", s), s)

    def test_case_insensitive(self):
        self.assertTrue(is_billing_reason("cpanel", "PENDING BILLS"))
        self.assertTrue(is_billing_reason("directadmin", "Billing"))


class TestDecisionMatrix(unittest.TestCase):
    def test_active_and_lapsed_is_suspended(self):
        r = decide([acct(domain="test.bt")], lapsed(["test.bt"]))
        self.assertEqual(r.decisions[0].action, SUSPEND)
        self.assertTrue(r.decisions[0].will_suspend)

    def test_active_and_billing_ok_is_skipped(self):
        r = decide([acct(domain="ok.bt")], lapsed(["other.bt"]))
        self.assertEqual(r.decisions[0].action, SKIP_NO_MATCH)
        self.assertFalse(r.decisions[0].will_suspend)

    def test_www_prefix_matches_on_either_side(self):
        """BSCS stores "www.test.bt" while panels report "test.bt". Both
        orderings must match, and the account side must be normalised too."""
        r1 = decide([acct(domain="test.bt")], lapsed(["www.test.bt"]))
        self.assertEqual(r1.decisions[0].action, SUSPEND)
        r2 = decide([acct(domain="www.test.bt")], lapsed(["test.bt"]))
        self.assertEqual(r2.decisions[0].action, SUSPEND)

    def test_normalise_domain(self):
        from suspension import normalise_domain
        self.assertEqual(normalise_domain("  WWW.Test.BT "), "test.bt")
        self.assertEqual(normalise_domain('"test.bt"'), "test.bt")
        self.assertEqual(normalise_domain(""), "")
        self.assertEqual(normalise_domain(None), "")

    def test_already_suspended_for_billing_is_skipped(self):
        a = acct(domain="test.bt", suspended=True, reason="pending bills")
        r = decide([a], lapsed(["test.bt"]))
        self.assertEqual(r.decisions[0].action, SKIP_ALREADY_BILLING)
        self.assertFalse(r.decisions[0].will_suspend)

    def test_abuse_case_is_never_suspended_even_if_billing_lapsed(self):
        """The headline rule: an abuse suspension is left completely alone."""
        a = acct(domain="test.bt", suspended=True, reason="abuse")
        r = decide([a], lapsed(["test.bt"]))
        self.assertEqual(r.decisions[0].action, SKIP_OTHER_REASON)
        self.assertFalse(r.decisions[0].will_suspend)

    def test_every_non_billing_reason_is_untouched(self):
        for reason in ("abuse", "spam", "user_bandwidth", "compromised",
                       "forwarding", "forwarded", "Surrendered", "", "Unknown"):
            a = acct(domain="test.bt", suspended=True, reason=reason)
            r = decide([a], lapsed(["test.bt"]))
            self.assertFalse(r.decisions[0].will_suspend,
                             f"reason {reason!r} must not be re-suspended")

    def test_account_with_no_domain_is_never_suspended(self):
        r = decide([acct(domain="")], lapsed(["test.bt"]))
        self.assertFalse(r.decisions[0].will_suspend)

    def test_suspended_account_is_not_even_matched_against_billing(self):
        """Billing is only consulted for accounts that are not suspended."""
        a = acct(domain="test.bt", suspended=True, reason="abuse")
        r = decide([a], lapsed([]))
        self.assertEqual(r.decisions[0].action, SKIP_OTHER_REASON)

    def test_multiple_domains_from_one_contract(self):
        r = decide([acct(domain="a.bt"), acct(domain="b.bt"), acct(domain="c.bt")],
                   lapsed(["a.bt", "b.bt"]))
        actions = [d.action for d in r.decisions]
        self.assertEqual(actions, [SUSPEND, SUSPEND, SKIP_NO_MATCH])

    def test_incomplete_list_is_flagged_but_decisions_still_made(self):
        """decide() reports; execute() is what refuses. Separation matters."""
        r = decide([acct(domain="test.bt")], lapsed(["test.bt"], complete=False, note="capped"))
        self.assertFalse(r.bscs_complete)
        self.assertEqual(r.bscs_note, "capped")

    def test_contract_matched_twice_is_not_also_listed_as_unmatched(self):
        """One account can match two contracts; the joined display string must
        not make each contract look unmatched as well."""
        lapsed_result = {
            "domains": {"test.bt"}, "complete": True, "note": "",
            "rows": [
                {"contract": "C1", "name_field": "www.test.bt", "domains": ["test.bt"]},
                {"contract": "C2", "name_field": "www.test.bt", "domains": ["test.bt"]},
            ],
        }
        r = decide([acct(username="t", domain="test.bt")], lapsed_result)
        self.assertEqual(r.decisions[0].action, SUSPEND)
        self.assertEqual(r.unmatched_contracts, [],
                         "both contracts matched the account, so neither is unmatched")

    def test_genuinely_unmatched_contract_is_recorded(self):
        lapsed_result = {
            "domains": {"a.bt"}, "complete": True, "note": "",
            "rows": [
                {"contract": "C1", "name_field": "www.a.bt", "domains": ["a.bt"]},
                {"contract": "C2", "name_field": "Jamtsho, Karma", "domains": []},
            ],
        }
        r = decide([acct(username="a", domain="a.bt")], lapsed_result)
        self.assertEqual(len(r.unmatched_contracts), 1)
        self.assertEqual(r.unmatched_contracts[0]["contract"], "C2")
        self.assertEqual(r.unmatched_contracts[0]["name_field"], "Jamtsho, Karma")

    def test_counts(self):
        accounts = [
            acct(username="a", domain="a.bt"),
            acct(username="b", domain="b.bt", suspended=True, reason="abuse"),
            acct(username="c", domain="c.bt", suspended=True, reason="billing"),
            acct(username="d", domain="d.bt"),
        ]
        r = decide(accounts, lapsed(["a.bt", "b.bt", "c.bt"]))
        counts = r.counts()
        self.assertEqual(counts[SUSPEND], 1)
        self.assertEqual(counts[SKIP_OTHER_REASON], 1)
        self.assertEqual(counts[SKIP_ALREADY_BILLING], 1)
        self.assertEqual(counts[SKIP_NO_MATCH], 1)


class TestExecutionGuards(unittest.TestCase):
    def test_incomplete_billing_list_refuses_to_run(self):
        """The single most important guard in this module."""
        r = decide([acct(domain="test.bt")], lapsed(["test.bt"], complete=False, note="25 of 63"))
        handler = lambda u, reason: {"success": True}  # noqa: E731
        with self.assertRaises(SuspensionError) as ctx:
            execute(r, {"cpanel": handler}, dry_run=False)
        self.assertIn("incomplete", str(ctx.exception).lower())

    def test_missing_handler_aborts_rather_than_skipping(self):
        """A panel we cannot act on must stop the run, not be quietly missed."""
        r = decide([acct(panel="directadmin", domain="test.bt")], lapsed(["test.bt"]))
        with self.assertRaises(SuspensionError) as ctx:
            execute(r, {"cpanel": lambda u, reason: {"success": True}}, dry_run=False)
        self.assertIn("no suspension handler", str(ctx.exception).lower())

    def test_dry_run_calls_no_handler(self):
        called = []

        def handler(u, reason):
            called.append(u)
            return {"success": True}

        r = decide([acct(username="x", domain="test.bt")], lapsed(["test.bt"]))
        out = execute(r, {"cpanel": handler}, dry_run=True)
        self.assertEqual(called, [], "dry run must not touch anything")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["results"][0]["action"], "would_suspend")

    def test_live_run_suspends_only_decided_accounts(self):
        called = []
        r = decide([acct(username="x", domain="a.bt"), acct(username="y", domain="b.bt")],
                   lapsed(["a.bt"]))
        out = execute(r, {"cpanel": lambda u, reason: called.append(u) or {"success": True}},
                      dry_run=False)
        self.assertEqual(called, ["x"])
        self.assertEqual(len(out["results"]), 1)

    def test_handler_failure_is_reported_not_swallowed(self):
        r = decide([acct(username="x", domain="a.bt")], lapsed(["a.bt"]))
        out = execute(r, {"cpanel": lambda u, reason: {"success": False, "message": "boom"}},
                      dry_run=False)
        self.assertFalse(out["results"][0]["success"])
        self.assertEqual(out["results"][0]["action"], "suspend_failed")
        self.assertIn("boom", out["results"][0]["message"])

    def test_already_suspended_from_handler_is_flagged(self):
        """Defence in depth: if the account got suspended between decide() and
        execute(), the handler's refusal must be visible, not counted as success."""
        r = decide([acct(username="x", domain="a.bt")], lapsed(["a.bt"]))
        out = execute(r, {"cpanel": lambda u, reason: {
            "success": False, "already_suspended": True, "message": "already suspended"}},
            dry_run=False)
        self.assertFalse(out["results"][0]["success"])
        self.assertTrue(out["results"][0]["already_suspended"])


class TestRealWorldScenarios(unittest.TestCase):
    """Scenarios taken from the actual dry run against the live systems."""

    def test_nissan_bhutan_scenario(self):
        """Active on cPanel, lapsed in BSCS, and a genuine suspension target."""
        accounts = [acct(panel="cpanel", username="nissanbhutan", domain="nissanbhutan.bt")]
        result = decide(accounts, lapsed(["nissanbhutan.bt"]))
        self.assertTrue(result.decisions[0].will_suspend)

    def test_jamtsho_karma_cannot_be_matched_and_is_not_suspended(self):
        """A lapsed contract with only a person's name yields no domain, so
        nothing is suspended. This is the known blind spot."""
        lapsed_result = {
            "domains": set(), "complete": True, "note": "",
            "rows": [{"contract": "CONTR0000181388", "customer_code": "1.124921",
                      "public_key": "CUST0000172549",
                      "name_field": "Jamtsho, Karma", "domains": []}],
        }
        accounts = [acct(panel="cpanel", username="someone", domain="unknown.bt")]
        r = decide(accounts, lapsed_result)
        self.assertFalse(r.decisions[0].will_suspend)

    def test_abuse_account_with_matching_lapsed_contract_is_untouched(self):
        accounts = [acct(panel="directadmin", username="dcclbt", domain="dcclbt.bt",
                         suspended=True, reason="abuse")]
        r = decide(accounts, lapsed(["dcclbt.bt"]))
        self.assertEqual(r.decisions[0].action, SKIP_OTHER_REASON)
        self.assertIn("not billing", r.decisions[0].reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAuditWriteIsReportedHonestly(unittest.TestCase):
    """
    Found in the first real run on the production host.

    The job's audit record could not be written -- the volume was root-owned
    while the job runs as provisioner -- and the run printed an ERROR followed
    by "audit written to ...". The dashboard then showed "no run recorded", with
    nothing on the console explaining why, because the one line an operator
    would have looked at said it had worked.

    Two faults: the Dockerfile created /app/data with the right ownership but
    not /app/suspension, and write_audit returned only a path, so the caller
    logged success unconditionally.
    """

    def test_a_successful_write_reports_true(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path, written = write_audit(_report(), {"results": []}, path=Path(d) / "a.jsonl")
            self.assertTrue(written)
            self.assertTrue(Path(path).exists())

    def test_a_failed_write_reports_false(self):
        unwritable = Path("/proc/nonexistent-dir/a.jsonl")
        path, written = write_audit(_report(), {"results": []}, path=unwritable)
        self.assertFalse(written, "a failed write must not be reported as written")

    def test_the_message_says_what_it_means_when_the_write_failed(self):
        """The line an operator reads must not claim success."""
        src = (Path(__file__).resolve().parent.parent
               / "scripts" / "suspend_expired.py").read_text()
        self.assertIn("if audit_written:", src)
        self.assertIn("NO AUDIT RECORD WAS WRITTEN", src)

    def test_the_image_creates_and_owns_the_suspension_directory(self):
        """A fresh named volume inherits the image directory's ownership. The
        Dockerfile handled /app/data and missed /app/suspension, so a newly
        commissioned host could not write its own audit trail."""
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
        self.assertIn("/app/suspension", dockerfile)
        mkdir = [ln for ln in dockerfile.splitlines() if "mkdir -p" in ln]
        self.assertTrue(any("/app/suspension" in ln for ln in mkdir),
                        "the image never creates /app/suspension")
        chown = [ln for ln in dockerfile.splitlines() if "chown -R" in ln]
        self.assertTrue(any("/app/suspension" in ln for ln in chown),
                        "and never gives it to the user the job runs as")


class TestTheHeartbeat(unittest.TestCase):
    """
    A run that fails writes no audit record, which is correct -- the audit log
    holds decisions and inventing an entry with none would mislead. But it left
    one situation indistinguishable from another: a job that did not run, and a
    job that ran and failed. Both read as silence.

    That hides the common case. A container down at 02:17, a host rebooting, a
    crash on an import, an expired credential -- none leave a trace, so all of
    them present as "no news", and the dashboard could only warn that the list
    might be out of date: true of all of them, and actionable for none.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "heartbeat.json"
        p = patch.object(suspension, "heartbeat_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)

    def test_a_good_run_is_recorded(self):
        suspension.write_heartbeat(True, "completed", duration_seconds=61.5)
        beat = suspension.read_heartbeat()
        self.assertTrue(beat["ok"])
        self.assertEqual(beat["detail"], "completed")
        self.assertEqual(beat["duration_seconds"], 61.5)
        self.assertTrue(beat["attempted_at"])

    def test_a_failed_run_is_recorded_too(self):
        suspension.write_heartbeat(False, "crashed: ValueError: no credentials")
        beat = suspension.read_heartbeat()
        self.assertFalse(beat["ok"])
        self.assertIn("ValueError", beat["detail"])

    def test_a_traceback_is_kept_but_trimmed(self):
        """An unhandled exception deep in a library can be enormous, and this
        file is read by the dashboard."""
        suspension.write_heartbeat(False, "crashed", traceback_text="x" * 9000)
        self.assertLessEqual(len(suspension.read_heartbeat()["traceback"]), 2000)

    def test_no_heartbeat_reads_as_none_rather_than_raising(self):
        self.assertIsNone(suspension.read_heartbeat())

    def test_a_corrupt_heartbeat_reads_as_none(self):
        """A truncated file must not take the dashboard down with it."""
        self.path.write_text("{ this is not json")
        self.assertIsNone(suspension.read_heartbeat())

    def test_writing_it_never_raises(self):
        with patch.object(suspension, "heartbeat_path",
                          return_value=Path("/proc/nope/heartbeat.json")):
            with self.assertLogs("suspension", level="ERROR"):
                beat = suspension.write_heartbeat(True, "completed")
        self.assertTrue(beat["ok"], "the value is still returned for logging")


class TestTheJobRecordsEveryInvocation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.script = (root / "scripts" / "suspend_expired.py").read_text()

    def test_a_crash_is_caught_and_recorded(self):
        """The case where leaving no trace is most costly: the job has died
        silently and the only sign is a list that quietly stopped updating."""
        self.assertIn("except Exception", self.script)
        self.assertIn("write_heartbeat(False, detail", self.script)

    def test_the_traceback_is_kept(self):
        self.assertIn("traceback.format_exc()", self.script)

    def test_a_clean_run_is_recorded_too(self):
        self.assertIn("write_heartbeat(code == 0, detail", self.script)

    def test_the_heartbeat_is_written_after_the_decisions(self):
        """So a run that decided something is not reported as a failure just
        because the heartbeat could not follow it."""
        self.assertLess(self.script.index("code = run(args)"),
                        self.script.index("write_heartbeat(code == 0"))

    def test_the_argument_parsing_still_works_from_the_cli(self):
        """Moved out of the job body, so --live and --only-panel must still land."""
        self.assertIn('ap.add_argument("--live"', self.script)
        self.assertIn("--only-panel", self.script)


class TestTheDashboardSaysWhichSituationThisIs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()

    def _body(self):
        from tests.test_dns_check import js_function
        return js_function(self.js, "loadSuspensionReport")

    def test_a_failed_run_is_named_as_such(self):
        self.assertIn("THE JOB FAILED", self._body())

    def test_a_job_that_never_ran_is_named_as_such(self):
        self.assertIn("NOT RUNNING", self._body())

    def test_the_old_wording_is_gone(self):
        """'A failed run writes no record' is no longer true, and repeating it
        would send someone looking for the wrong thing."""
        self.assertNotIn("A failed run writes no record", self.js)

    def test_the_traceback_is_shown(self):
        self.assertIn("last_attempt_traceback", self._body())

    def test_a_merely_old_list_is_still_called_stale(self):
        """Not everything old is broken, and treating all of it as a failure is
        how people learn to ignore warnings."""
        self.assertIn("last successful run", self._body())
