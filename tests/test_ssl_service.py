"""
Certificates for newly hosted domains.

The behaviour these tests pin down is mostly about restraint: when a certificate
must NOT be attempted, and how that is reported.

A failed Let's Encrypt validation is not a no-op. It counts against a rate limit
-- five failed authorisations per hostname per account per week -- and that
limit is shared by every customer on the server. A tool that tries and fails
repeatedly can lock out real customers for days. So the gate is tested as
carefully as the issuance path.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssl_service
from config import settings


class TestTheDnsGate(unittest.TestCase):
    """Whether a certificate may be attempted at all."""

    def test_a_domain_pointing_here_may_be_attempted(self):
        with patch("ssl_service.check_domain",
                   return_value={"status": "mapped", "message": "resolves here"}):
            self.assertTrue(ssl_service.dns_gate("cpanel", "wank.bt")["allowed"])

    def test_a_domain_pointing_elsewhere_may_not(self):
        with patch("ssl_service.check_domain",
                   return_value={"status": "not_mapped",
                                 "message": "Resolves to 203.0.113.9"}):
            gate = ssl_service.dns_gate("cpanel", "wank.bt")
        self.assertFalse(gate["allowed"])

    def test_the_refusal_says_why(self):
        """"No" with no explanation is what makes an operator press the button
        again."""
        with patch("ssl_service.check_domain",
                   return_value={"status": "not_mapped",
                                 "message": "Resolves to 203.0.113.9"}):
            reason = ssl_service.dns_gate("cpanel", "wank.bt")["reason"]
        self.assertIn("does not point at this server", reason)
        self.assertIn("203.0.113.9", reason)

    def test_no_domain_is_refused(self):
        self.assertFalse(ssl_service.dns_gate("cpanel", "")["allowed"])

    def test_the_gate_can_be_switched_off_but_says_so(self):
        """An escape hatch is fine; one that pretends the domain is fine is not."""
        with patch.object(settings, "SSL_REQUIRE_DNS_MAPPING", False):
            with patch("ssl_service.check_domain") as chk:
                gate = ssl_service.dns_gate("cpanel", "wank.bt")
        chk.assert_not_called()
        self.assertTrue(gate["allowed"])
        self.assertIn("switched off", gate["reason"])


class TestTheWholePathIsGated(unittest.TestCase):
    """
    The gate has to sit in front of the panel call, not merely beside it. A test
    on dns_gate alone would pass even if enable_ssl ignored it.
    """

    def test_an_unmapped_domain_never_reaches_the_panel(self):
        with patch.object(settings, "SSL_REQUIRE_DNS_MAPPING", True), \
             patch("ssl_service.check_domain",
                   return_value={"status": "not_mapped", "message": "elsewhere"}), \
             patch.object(ssl_service, "enable_cpanel") as cp, \
             patch.object(ssl_service, "enable_directadmin") as da:
            out = ssl_service.enable_ssl("cpanel", "wank", "wank.bt")
        cp.assert_not_called()
        da.assert_not_called()
        self.assertEqual(out["status"], ssl_service.SKIPPED)
        self.assertTrue(out["gated"])

    def test_a_mapped_domain_does_reach_the_panel(self):
        with patch.object(settings, "SSL_REQUIRE_DNS_MAPPING", True), \
             patch("ssl_service.check_domain",
                   return_value={"status": "mapped", "message": "here"}), \
             patch.object(ssl_service, "enable_cpanel",
                          return_value={"step": "ssl", "success": True,
                                        "status": "issued", "message": "queued"}):
            out = ssl_service.enable_ssl("cpanel", "wank", "wank.bt")
        self.assertEqual(out["status"], "issued")

    def test_the_dns_evidence_travels_with_the_success(self):
        """The claim 'a certificate covers this domain' is only meaningful
        alongside the DNS that made it safe to issue."""
        with patch.object(settings, "SSL_REQUIRE_DNS_MAPPING", True), \
             patch("ssl_service.check_domain",
                   return_value={"status": "mapped", "resolved": ["202.144.128.216"]}), \
             patch.object(ssl_service, "enable_cpanel",
                          return_value={"step": "ssl", "success": True,
                                        "status": "issued", "message": "queued"}):
            out = ssl_service.enable_ssl("cpanel", "wank", "wank.bt")
        self.assertEqual(out["dns"]["resolved"], ["202.144.128.216"])

    def test_the_whole_thing_can_be_switched_off(self):
        with patch.object(settings, "SSL_ENABLED", False):
            out = ssl_service.enable_ssl("cpanel", "wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.SKIPPED)
        self.assertIn("switched off", out["message"])

    def test_an_unknown_panel_is_refused_rather_than_guessed(self):
        out = ssl_service.enable_ssl("plesk", "wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.SKIPPED)


class TestDirectAdminOutcomes(unittest.TestCase):
    """
    Read from the server's own /static/swagger.json rather than from
    documentation that may be a version behind. Each of these error codes is a
    different situation needing a different answer from a human.
    """

    def _run(self, status_code, body):
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.post") as post:
            cfg.host = "203.0.113.1"
            cfg.tls_hostname = ""
            cfg.api_user = "admin"
            cfg.api_password = "pw"
            resp = post.return_value
            resp.status_code = status_code
            resp.json.return_value = body
            return ssl_service.enable_directadmin("wank.bt", "wank")

    def test_a_fulfilled_certificate_is_a_success(self):
        out = self._run(200, {"acmeEnabled": True, "certsFulfilled": [{"domain": "wank.bt"}],
                              "dnsNamesFailedChallenge": []})
        self.assertEqual(out["status"], ssl_service.ISSUED)
        self.assertIn("renews it automatically", out["message"])

    def test_acme_being_off_is_reported_as_the_servers_own_word(self):
        """Found live: yongnay answers acmeEnabled:false. Believed over any
        assumption, and not dressed up as a failure."""
        out = self._run(200, {"acmeEnabled": False, "certsFulfilled": []})
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("licence", out["message"].lower())

    def test_a_failed_challenge_is_a_failure_naming_the_name(self):
        out = self._run(200, {"acmeEnabled": True, "certsFulfilled": [],
                              "dnsNamesFailedChallenge": ["mail.wank.bt"]})
        self.assertEqual(out["status"], ssl_service.FAILED)
        self.assertIn("mail.wank.bt", out["message"])

    def test_a_caa_failure_is_reported(self):
        out = self._run(200, {"acmeEnabled": True, "certsFulfilled": [],
                              "dnsNamesFailedCAA": ["wank.bt"]})
        self.assertIn("wank.bt", out["message"])

    def test_the_rate_limit_is_never_suggested_to_retry(self):
        """It is shared across the server. Retrying is what makes it worse."""
        out = self._run(490, {"type": "RATELIMIT_REACHED"})
        self.assertIn("do not retry", out["message"])

    def test_an_issue_already_running_is_distinguished(self):
        out = self._run(491, {"type": "DOMAIN_ACME_ALREADY_IN_PROGRESS"})
        self.assertIn("already running", out["message"])

    def test_a_licence_overused_is_not_called_a_failure(self):
        out = self._run(402, {"type": "LICENSE_OVERUSED"})
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("licence", out["message"].lower())

    def test_a_non_json_body_does_not_crash_provisioning(self):
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.post") as post:
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            post.return_value.status_code = 500
            post.return_value.json.side_effect = ValueError("not json")
            post.return_value.text = "<html>oops</html>"
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.FAILED)
        self.assertIn("not JSON", out["message"])

    def test_a_network_failure_does_not_fail_the_account(self):
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.post", side_effect=OSError("refused")):
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.FAILED)


class TestCpanelOutcomes(unittest.TestCase):
    def test_a_queued_domain_is_a_success(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("provisioners.cpanel.CPanelProvisioner") as prov, \
             patch.object(settings.CPANEL, "sudo_password", "pw"):
            prov.return_value.ssh.execute.return_value = (
                0, '{"result":1,"metadata":{"result":[{"status":"queued"}]}}', "")
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.ISSUED)

    def test_a_missing_plugin_is_reported_as_unavailable(self):
        """Verified live: thimpchu has no AutoSSL plugin and no Let's Encrypt
        provider RPM, so there is genuinely nothing to call."""
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("provisioners.cpanel.CPanelProvisioner") as prov, \
             patch.object(settings.CPANEL, "sudo_password", "pw"):
            prov.return_value.ssh.execute.return_value = (
                1, "", "autossl: command not found")
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("Install", out["message"])

    def test_a_refusal_is_a_failure(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("provisioners.cpanel.CPanelProvisioner") as prov, \
             patch.object(settings.CPANEL, "sudo_password", "pw"):
            prov.return_value.ssh.execute.return_value = (
                0, '{"result":0,"errorstatus":1,"errors":["domain not validated"]}', "")
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.FAILED)

    def test_no_username_is_refused(self):
        out = ssl_service.enable_cpanel("", "wank.bt")
        self.assertEqual(out["status"], ssl_service.FAILED)


class TestRenewalIsNotOursToMaintain(unittest.TestCase):
    """
    Both panels renew on their own once a certificate exists. A cron of ours would
    be a second mechanism that fails silently, and an expired certificate is
    discovered by customers rather than by us.
    """

    def test_no_cron_entry_exists_for_certificates(self):
        root = Path(__file__).resolve().parent.parent
        crontab = (root / "deploy" / "crontab").read_text()
        self.assertNotIn("ssl", crontab.lower())
        self.assertNotIn("cert", crontab.lower())

    def test_no_renewal_code_exists(self):
        root = Path(__file__).resolve().parent.parent
        src = (root / "ssl_service.py").read_text()
        self.assertNotIn("def renew", src)


class TestItIsWiredIntoProvisioning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.app = (root / "app.py").read_text()
        cls.js = (root / "static" / "js" / "app.js").read_text()

    def test_the_certificate_step_runs_after_account_creation(self):
        body = self.app.split("def run_post_create_steps(")[1].split("\n@app.")[0]
        self.assertIn("ssl_service.enable_ssl(", body)
        # After the panel-specific steps, so a failed certificate never prevents
        # the IPv6 or SFTP work.
        self.assertGreater(body.index("ssl_service.enable_ssl("),
                           min(body.index("enable_ipv6"), body.index("allow_sftp_user")))

    def test_a_skipped_step_is_not_logged_as_a_failure(self):
        """A red warning on a domain that has not been pointed at the server yet
        is how warnings stop being read."""
        body = self.app.split("def run_post_create_steps(")[1].split("\n@app.")[0]
        self.assertIn("ssl_service.SKIPPED", body)

    def test_the_panel_shows_why_it_was_skipped(self):
        from tests.test_dns_check import js_function
        body = js_function(self.js, "renderPostCreate")
        self.assertIn("skipped", body)
        self.assertIn("unsupported", body)
        self.assertIn("Nothing to fix above", body)


class TestTheDockerImageShipsTheModule(unittest.TestCase):
    """
    The Dockerfile copies modules by an explicit list, not COPY . . -- which has
    bitten this project before, silently leaving a module out of the image and
    every account created with no certificate and no error.
    """

    def test_ssl_service_is_copied(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text()
        self.assertIn("ssl_service.py", dockerfile)


class TestCpanelDoesNotGuess(unittest.TestCase):
    """
    AutoSSL is not installed on thimpchu, so the WHM function name could not be
    read off the server the way ipv6_enable_account was. Calling a guessed name
    would return a confident-looking "AutoSSL is not installed" and send whoever
    reads it chasing the wrong problem, when the real issue is the guess.
    """

    def test_it_refuses_rather_than_calling_a_guessed_function(self):
        # False is the point of this test: unverified is the default.
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", False), \
             patch("provisioners.cpanel.CPanelProvisioner") as prov:
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        prov.assert_not_called()
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("not been set up", out["message"])

    def test_the_message_does_not_blame_auto_ssl_being_absent(self):
        """It is absent, but that is not why nothing was attempted, and saying so
        would point at the wrong fix."""
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", False):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertNotIn("Install", out["message"])

    def test_the_gate_is_still_true_by_default(self):
        """Defaulting to a guess would ship it silently."""
        self.assertFalse(settings.CPANEL_AUTOSSL_VERIFIED)


class TestAnUnauthorisedRequestIsNotBlamedOnThePlatform(unittest.TestCase):
    """
    Found live: impersonating an account that does not exist on the server
    answers 401 UNAUTHORIZED. That is not DirectAdmin declining to issue a
    certificate -- it is the account or the credentials being wrong, and folding
    it into "not supported" sends whoever reads it to install a licence they
    already have.
    """

    def test_a_401_is_a_failure_not_an_unsupported_platform(self):
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.post") as post:
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            post.return_value.status_code = 401
            post.return_value.json.return_value = {"type": "UNAUTHORIZED"}
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.FAILED)
        self.assertIn("does not exist", out["message"])

    def test_the_rate_limit_stays_unsupported(self):
        """It is a platform condition, and the message must keep saying do not
        retry."""
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.post") as post:
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            post.return_value.status_code = 490
            post.return_value.json.return_value = {"type": "RATELIMIT_REACHED"}
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
