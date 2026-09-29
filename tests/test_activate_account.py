"""
Tests for re-activating an account suspended for billing.

The point of this feature is putting a paying customer's website back up. The
risk in it is putting the *wrong* customer's website back up: an account taken
down for abuse, spam or compromise is suspended for a reason a payment does not
address, and reactivating it would quietly overturn a decision somebody made on
purpose.

So the rule under test throughout is that only a billing suspension is lifted,
and it is re-read from the panel rather than taken from the nightly report.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner


def prov(cls, state, after=None, exec_result=(0, '{"metadata":{"result":1}}', "")):
    """A provisioner with a scripted account_state: first call, then the after."""
    p = cls.__new__(cls)
    p.ssh = MagicMock()
    p.ssh.execute.return_value = exec_result
    # The sudo branch reads these, as it does when built from config.
    p.ssh_user = "wangchuk"
    p.ssh_password = "pw"
    p.host = "202.144.128.216"
    p.api_user, p.api_password = "admin", "pw"
    p.tls_hostname = ""
    p.account_state = MagicMock(side_effect=[state, after if after is not None else state])
    return p


def state(suspended, reason="", domain="wank.bt"):
    return {"suspended": suspended, "reason": reason, "domain": domain,
            "username": "wank", "suspend_time": ""}


class TestOnlyBillingSuspensionsAreLifted(unittest.TestCase):
    """The safety rule, on both panels."""

    REASONS = ["abuse", "spam", "compromised", "user_bandwidth", "Surrendered",
               "bandwidth exceeded", "", "unknown"]

    def test_cpanel_refuses_every_other_reason(self):
        for reason in self.REASONS:
            p = prov(CPanelProvisioner, state(True, reason))
            got = p.activate_account("wank", confirm=True)
            self.assertFalse(got["success"], f"{reason!r} was allowed")
            self.assertTrue(got["wrong_reason"], f"{reason!r} was not flagged as a refusal")
            self.assertIn("not billing", got["message"])

    def test_directadmin_refuses_every_other_reason(self):
        for reason in self.REASONS:
            p = prov(DirectAdminProvisioner, state(True, reason))
            got = p.activate_account("wank", confirm=True)
            self.assertFalse(got["success"], f"{reason!r} was allowed")
            self.assertTrue(got["wrong_reason"])

    def test_a_refused_account_is_never_actually_touched(self):
        """The refusal must happen before the write, not after it."""
        p = prov(CPanelProvisioner, state(True, "abuse"))
        p.activate_account("wank", confirm=True)
        p.ssh.execute.assert_not_called()

    def test_billing_is_lifted_on_cpanel(self):
        p = prov(CPanelProvisioner, state(True, "billing"), after=state(False, ""))
        self.assertTrue(p.activate_account("wank", confirm=True)["success"])

    def test_billing_on_directadmin_is_recorded_not_claimed(self):
        """DirectAdmin has no API for this, so the result must say the account
        is still suspended rather than report it done."""
        p = prov(DirectAdminProvisioner, state(True, "Billing"))
        got = p.activate_account("wank", confirm=True)
        self.assertTrue(got["success"])
        self.assertTrue(got["manual"], "must be flagged as needing a manual step")
        self.assertIn("still suspended", got["message"])

    def test_the_reason_match_ignores_case_and_padding(self):
        p = prov(CPanelProvisioner, state(True, "  BILLING "), after=state(False, ""))
        self.assertTrue(p.activate_account("wank", confirm=True)["success"])


class TestAlreadyActiveIsNotAnError(unittest.TestCase):
    """A double-click must not raise an alarm; the account is already up."""

    def test_cpanel(self):
        got = prov(CPanelProvisioner, state(False)).activate_account("wank", confirm=True)
        self.assertTrue(got["success"])
        self.assertTrue(got["already_active"])

    def test_directadmin(self):
        got = prov(DirectAdminProvisioner, state(False)).activate_account("wank", confirm=True)
        self.assertTrue(got["success"])
        self.assertTrue(got["already_active"])

    def test_nothing_is_written(self):
        p = prov(CPanelProvisioner, state(False))
        p.activate_account("wank", confirm=True)
        p.ssh.execute.assert_not_called()


class TestConfirmationAndUnknownAccounts(unittest.TestCase):
    def test_confirm_is_required_on_both(self):
        for cls in (CPanelProvisioner, DirectAdminProvisioner):
            p = prov(cls, state(True, "billing"), after=state(False, ""))
            self.assertFalse(p.activate_account("wank", confirm=False)["success"])

    def test_an_unknown_account_fails_rather_than_reporting_success(self):
        p = prov(CPanelProvisioner, None)
        self.assertFalse(p.activate_account("nope", confirm=True)["success"])

    def test_a_nonsense_username_is_refused_before_any_call(self):
        p = prov(CPanelProvisioner, state(True, "billing"))
        self.assertFalse(p.activate_account("bad name; rm -rf /", confirm=True)["success"])
        p.ssh.execute.assert_not_called()


class TestSuccessIsVerifiedNotAssumed(unittest.TestCase):
    """
    whmapi1 and the DirectAdmin API both report failure in the body while
    exiting zero, so a reply is not proof. Both re-read the account afterwards.
    """

    def test_cpanel_confirms_the_account_really_came_up(self):
        p = prov(CPanelProvisioner, state(True, "billing"), after=state(True, "billing"))
        got = p.activate_account("wank", confirm=True)
        self.assertFalse(got["success"])
        self.assertIn("still suspended", got["message"])

    def test_directadmin_never_claims_an_account_it_cannot_reach(self):
        p = prov(DirectAdminProvisioner, state(True, "billing"))
        got = p.activate_account("wank", confirm=True)
        self.assertTrue(got["manual"])
        self.assertNotIn("back up", got["message"])

    def test_the_cpanel_call_is_the_verified_one(self):
        """unsuspendacct, read from Accounts.pm on the server. cPanel has no
        reason parameter on the way back up."""
        p = prov(CPanelProvisioner, state(True, "billing"), after=state(False, ""))
        p.activate_account("wank", confirm=True)
        command = p.ssh.execute.call_args[0][0]
        self.assertIn("unsuspendacct", command)
        self.assertIn("user=wank", command)
        self.assertNotIn("suspendacct user", command.replace("unsuspendacct user", ""))
        self.assertNotIn("reason=", command)

    def test_directadmin_makes_no_api_call_at_all(self):
        """The old CMD_API_MODIFY_USER call was inferred from the binary and
        never worked. Its own swagger spec has no suspend endpoint."""
        p = prov(DirectAdminProvisioner, state(True, "billing"))
        with patch("provisioners.directadmin.requests.get") as get:
            p.activate_account("wank", confirm=True)
        get.assert_not_called()

    def test_the_suspension_path_also_makes_no_api_call(self):
        p = prov(DirectAdminProvisioner, state(False))
        with patch("provisioners.directadmin.requests.get") as get:
            got = p.suspend_account("wank", reason="billing", confirm=True)
        get.assert_not_called()
        self.assertTrue(got["manual"])
        self.assertIn("NOT suspended", got["message"])


class TestActivateEndpoint(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        app_module.app.dependency_overrides[app_module.require_token_for_destructive] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def _prov(self, st):
        p = MagicMock()
        p.account_state.return_value = st
        p.activate_account.return_value = {"success": True, "message": "Activated."}
        return p

    def test_a_billing_suspension_is_lifted(self):
        with patch.object(app_module, "get_da_provisioner", return_value=self._prov(state(True, "billing"))):
            r = self.client.post("/api/v1/suspension/activate",
                                 data={"panel": "directadmin", "username": "wank",
                                       "confirm": "true"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_abuse_suspension_is_refused_with_409(self):
        """The whole point. A payment must not clear an abuse suspension."""
        with patch.object(app_module, "get_da_provisioner", return_value=self._prov(state(True, "abuse"))):
            r = self.client.post("/api/v1/suspension/activate",
                                 data={"panel": "directadmin", "username": "wank",
                                       "confirm": "true"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("not billing", r.json()["detail"])

    def test_a_refusal_never_calls_activate(self):
        p = self._prov(state(True, "abuse"))
        with patch.object(app_module, "get_da_provisioner", return_value=p):
            self.client.post("/api/v1/suspension/activate",
                             data={"panel": "directadmin", "username": "wank",
                                   "confirm": "true"})
        p.activate_account.assert_not_called()

    def test_already_active_is_a_success_not_an_error(self):
        with patch.object(app_module, "get_da_provisioner", return_value=self._prov(state(False))):
            r = self.client.post("/api/v1/suspension/activate",
                                 data={"panel": "directadmin", "username": "wank",
                                       "confirm": "true"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["already_active"])

    def test_confirm_is_required(self):
        r = self.client.post("/api/v1/suspension/activate",
                             data={"panel": "directadmin", "username": "wank"})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_account_is_404(self):
        with patch.object(app_module, "get_da_provisioner", return_value=self._prov(None)):
            r = self.client.post("/api/v1/suspension/activate",
                                 data={"panel": "directadmin", "username": "wank",
                                       "confirm": "true"})
        self.assertEqual(r.status_code, 404)

    def test_it_re_reads_state_rather_than_trusting_the_report(self):
        """The report is a snapshot; the reason is checked live at action time."""
        p = self._prov(state(True, "abuse"))
        with patch.object(app_module, "get_da_provisioner", return_value=p):
            self.client.post("/api/v1/suspension/activate",
                             data={"panel": "directadmin", "username": "wank",
                                   "confirm": "true"})
        p.account_state.assert_called_once_with("wank")


class TestReportOffersSomethingToActivate(unittest.TestCase):
    def test_the_report_lists_accounts_suspended_for_billing(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        try:
            client = TestClient(app_module.app)
            with patch.object(app_module, "latest_report", return_value={
                    "generated_at": "2026-09-28T02:17:00+06:00",
                    "total_accounts": 356, "bscs_complete": True,
                    "counts": {app_module.SKIP_ALREADY_BILLING: 2,
                               app_module.SKIP_OTHER_REASON: 1,
                               app_module.SKIP_NO_MATCH: 3,
                               app_module.SUSPEND: 1},
                    "decisions": [
                        {"action": app_module.SKIP_ALREADY_BILLING, "panel": "directadmin",
                         "username": "wank", "domain": "wank.bt", "reason": "billing"},
                        {"action": app_module.SKIP_OTHER_REASON, "panel": "cpanel",
                         "username": "abuser", "domain": "bad.bt", "reason": "abuse"},
                    ],
                    "unmatched_contracts": [],
            }):
                r = client.get("/api/v1/suspension/report")
            body = r.json()
            self.assertEqual(len(body["activatable"]), 1)
            self.assertEqual(body["activatable"][0]["username"], "wank")
            # Only billing ones. An abuse suspension is not something to offer.
            self.assertNotIn("abuser", [a["username"] for a in body["activatable"]])
        finally:
            app_module.app.dependency_overrides = {}

    def test_the_dashboard_renders_the_section(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text()
        html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text()
        self.assertIn("renderActivatable", js)
        self.assertIn("activateNow", js)
        self.assertIn("suspension-activatable", html)
        self.assertIn("/api/v1/suspension/activate", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
