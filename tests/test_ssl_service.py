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
from unittest.mock import Mock, patch

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
        # enable_directadmin reads the per-domain acme config before issuing, so
        # the GET is mocked as "already enabled" and the PUT is never reached.
        # This keeps the test about the provision response it was written for.
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": True}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("dns_check.resolve_ips", return_value=["203.0.113.5"]), \
             patch("ssl_service.requests.get", return_value=acme), \
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
        """Found live: yongnay answers acmeEnabled:false for a domain with the
        per-domain switch off. Believed over any assumption, and not dressed up
        as a failure -- but not blamed on a licence, because that was wrong."""
        out = self._run(200, {"acmeEnabled": False, "certsFulfilled": []})
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("per-domain", out["message"])
        self.assertNotIn("licence", out["message"].lower())

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
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": True}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("dns_check.resolve_ips", return_value=["203.0.113.5"]), \
             patch("ssl_service.requests.get", return_value=acme), \
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
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": True}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("dns_check.resolve_ips", return_value=["203.0.113.5"]), \
             patch("ssl_service.requests.get", return_value=acme), \
             patch("ssl_service.requests.post", side_effect=OSError("refused")):
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.FAILED)


class TestCpanelAutoSsl(unittest.TestCase):
    """
    The calls are no longer guessed. They were read out of thimpchu's own
    /usr/local/cpanel/Whostmgr/API/1/SSL.pm -- the same file ipv6_enable_account
    came from -- which replaced a guess that would have reported a confident and
    wrong reason when it failed.

    The operator's own description of the workflow ("log in with the credentials
    and set Let's Encrypt") matches start_autossl_check_for_one_user, which runs
    `autossl --user <name>`. Running it as root over WHM avoids needing the
    customer's password, but not the preconditions, and each is checked and named.
    """

    def test_the_function_names_come_from_the_servers_own_api(self):
        for name in ("get_autossl_providers", "set_autossl_provider",
                     "start_autossl_check_for_one_user"):
            self.assertIn(name, ssl_service.__doc__ + open(ssl_service.__file__).read())

    def test_the_guard_is_on_by_default_now(self):
        """They were read off the server, so the default is to use them. The
        preconditions are checked at run time instead."""
        self.assertTrue(settings.CPANEL_AUTOSSL_VERIFIED)

    def test_the_guard_can_still_be_turned_off(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", False):
            with patch("ssl_service.autossl_providers") as probe:
                out = ssl_service.enable_cpanel("wank", "wank.bt")
        probe.assert_not_called()
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("not been confirmed", out["message"])

    def test_missing_autossl_plugin_is_named(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": False, "providers": [], "enabled": []}):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("not installed", out["message"])

    def test_installed_but_not_switched_on_is_a_different_fix(self):
        """"Install AutoSSL" would be wrong advice here: it is installed."""
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["LetsEncrypt"],
                                 "enabled": []}):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("not switched on", out["message"])
        self.assertNotIn("not installed", out["message"])

    def test_an_unreadable_feature_list_is_unknown_not_a_refusal(self):
        """Unknown and no are different answers, and guessing either is wrong:
        stopping skips a certificate that could have been issued, proceeding hits
        a per-account error that does not name the cause."""
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["LetsEncrypt"],
                                 "enabled": ["LetsEncrypt"]}), \
             patch("ssl_service.package_has_autossl", return_value=None):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.FAILED)
        self.assertIn("unknown", out["message"].lower())

    def test_the_feature_check_reads_the_list_the_package_names(self):
        """cPanel keeps this two hops deep: the package names a feature list and
        AutoSSL has to be ticked there. Checking the package itself finds nothing
        and would have concluded wrongly."""
        src = Path(ssl_service.__file__).read_text()
        i = src.index("def package_has_autossl(")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("FEATURELIST", body)
        self.assertIn("read_featurelist", body)

    def test_the_provider_name_key_comes_from_the_servers_answer(self):
        """I looked for 'name' and 'id', got an empty string, and concluded no
        provider existed -- which was wrong. It is module_name."""
        src = Path(ssl_service.__file__).read_text()
        i = src.index("def autossl_providers(")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("module_name", body)
        self.assertNotIn('p.get("name")', body)

    def test_no_letsencrypt_provider_is_named_separately(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["CPANELSignup"],
                                 "enabled": ["CPANELSignup"]}), \
             patch("ssl_service.package_has_autossl", return_value=True):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("CPANELSignup", out["message"])

    def test_a_package_without_the_feature_is_named(self):
        """start_autossl_check_for_one_user dies on this by name. Checking here
        turns a per-account surprise into one thing to look at -- and on this
        server none of the eight packages carry it, so installing the plugin
        alone would still not be enough."""
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["LetsEncrypt"],
                                 "enabled": ["LetsEncrypt"]}), \
             patch("ssl_service.package_has_autossl", return_value=False):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("package", out["message"].lower())
        self.assertIn("AutoSSL feature", out["message"])

    def _run_whm(self, responses):
        """responses maps the function name to (rc, stdout)."""
        calls = []

        class FakeSSH:
            def execute(self, cmd, stdin_data=None):
                for fn, (rc, body) in responses.items():
                    if f" {fn}" in cmd or cmd.endswith(fn):
                        calls.append(fn)
                        return rc, body, ""
                return 1, "", ""

        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["LetsEncrypt"]}), \
             patch("ssl_service.package_has_autossl", return_value=True), \
             patch("ssl_service._cpanel_ssh", return_value=FakeSSH()):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        return out, calls

    def test_a_successful_run_enables_auto_ssl_then_starts_it(self):
        ok = '{"metadata":{"result":1}}'
        out, calls = self._run_whm({
            "set_autossl_provider": (0, ok),
            "start_autossl_check_for_one_user": (0, ok),
        })
        self.assertEqual(out["status"], ssl_service.ISSUED)
        self.assertEqual(calls, ["set_autossl_provider",
                                 "start_autossl_check_for_one_user"],
                         "the server-wide switch must be set before the per-user run")
        self.assertIn("renews it automatically", out["message"])

    def test_it_passes_the_account_not_the_domain(self):
        """The API takes an account name. Passing the domain would silently
        match nothing, since no account is named after its own domain."""
        seen = []

        class FakeSSH:
            def execute(self, cmd, stdin_data=None):
                seen.append(cmd)
                return 0, '{"metadata":{"result":1}}', ""

        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers",
                   return_value={"available": True, "providers": ["LetsEncrypt"]}), \
             patch("ssl_service.package_has_autossl", return_value=True), \
             patch("ssl_service._cpanel_ssh", return_value=FakeSSH()):
            ssl_service.enable_cpanel("wank", "wank.bt")
        run_call = [c for c in seen if "start_autossl_check" in c][0]
        self.assertIn("user=wank", run_call)
        self.assertNotIn("wank.bt", run_call)

    def test_a_package_refusal_from_cpanel_is_reported_as_a_package_problem(self):
        out, _ = self._run_whm({
            "set_autossl_provider": (0, '{"metadata":{"result":1}}'),
            "start_autossl_check_for_one_user": (
                0, '{"metadata":{"result":0,"errors":["The user [wank] does not '
                   'have the [AutoSSL] feature enabled"]}}'),
        })
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)
        self.assertIn("package must include", out["message"])

    def test_any_other_refusal_is_a_failure(self):
        out, _ = self._run_whm({
            "set_autossl_provider": (0, '{"metadata":{"result":1}}'),
            "start_autossl_check_for_one_user": (
                0, '{"metadata":{"result":0,"errors":["unknown error"]}}'),
        })
        self.assertEqual(out["status"], ssl_service.FAILED)

    def test_a_ssh_executor_defaults_to_port_22_which_is_not_listening(self):
        """CPanelProvisioner(host) defaults ssh_port to 22 and thimpchu does not
        listen there, so the connection hangs to timeout rather than failing at
        once. Every panel call here passes the configured port."""
        src = Path(ssl_service.__file__).read_text()
        i = src.index("def _cpanel_ssh(")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("c.ssh_port", body)
        self.assertNotIn("CPanelProvisioner(settings.CPANEL.host)", src)

    def test_it_cannot_reach_cpanel_without_failing_the_account(self):
        with patch.object(settings, "CPANEL_AUTOSSL_VERIFIED", True), \
             patch("ssl_service.autossl_providers", side_effect=OSError("refused")):
            out = ssl_service.enable_cpanel("wank", "wank.bt")
        self.assertEqual(out["status"], ssl_service.FAILED)

    def test_no_username_is_refused_before_anything_is_called(self):
        with patch("provisioners.cpanel.CPanelProvisioner") as prov:
            out = ssl_service.enable_cpanel("", "wank.bt")
        prov.assert_not_called()
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


class TestAnUnauthorisedRequestIsNotBlamedOnThePlatform(unittest.TestCase):
    """
    Found live: impersonating an account that does not exist on the server
    answers 401 UNAUTHORIZED. That is not DirectAdmin declining to issue a
    certificate -- it is the account or the credentials being wrong, and folding
    it into "not supported" sends whoever reads it to install a licence they
    already have.
    """

    def test_a_401_is_a_failure_not_an_unsupported_platform(self):
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": True}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("dns_check.resolve_ips", return_value=["203.0.113.5"]), \
             patch("ssl_service.requests.get", return_value=acme), \
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
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": True}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("dns_check.resolve_ips", return_value=["203.0.113.5"]), \
             patch("ssl_service.requests.get", return_value=acme), \
             patch("ssl_service.requests.post") as post:
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            post.return_value.status_code = 490
            post.return_value.json.return_value = {"type": "RATELIMIT_REACHED"}
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertEqual(out["status"], ssl_service.UNSUPPORTED)


class TestLetsEncryptIsEnabledPerDomain(unittest.TestCase):
    """
    I first read DirectAdmin's acmeEnabled:false as a licence gate and was wrong.
    It is a per-domain setting, in the domain's own .conf, settable through
    PUT /api/domain-tls/{domain}/acme-config -- and domains with it on hold live
    Let's Encrypt certificates on this server with no licence at all.

    A domain created by the provisioning flow will not have it, and the panel
    answers DOMAIN_ACME_IS_DISABLED rather than issuing anything.
    """

    def _da(self, get_status=200, get_body=None, put_status=204):
        get_resp = Mock(status_code=get_status)
        get_resp.json.return_value = get_body if get_body is not None else {"enabled": False}
        put_resp = Mock(status_code=put_status, text="")
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.get", return_value=get_resp), \
             patch("ssl_service.requests.put", return_value=put_resp) as put, \
             patch("ssl_service.requests.post") as post:
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "acmeEnabled": True, "certsFulfilled": [{"domain": "wank.bt"}]}
            out = ssl_service.enable_directadmin("wank.bt", "wank")
        return out, put, post

    def test_a_new_domain_gets_lets_encrypt_turned_on(self):
        out, put, _ = self._da(get_body={"enabled": False})
        put.assert_called_once()
        self.assertEqual(put.call_args.kwargs["json"]["enabled"], True)

    def test_a_domain_that_already_has_it_is_not_written_to(self):
        """Read first, so enabling is a no-op on the many domains that already
        work rather than a pointless write on every provisioning."""
        _, put, _ = self._da(get_body={"enabled": True})
        put.assert_not_called()

    def test_the_whole_object_is_sent(self):
        """A partial PUT is refused with 'unknown acme key type'. Found live."""
        _, put, _ = self._da(get_body={"enabled": False})
        sent = put.call_args.kwargs["json"]
        for field in ("keyType", "provider", "preferWildcard", "skipDNSNames"):
            self.assertIn(field, sent, f"{field} is required in full")

    def test_existing_settings_are_preserved(self):
        _, put, _ = self._da(get_body={"enabled": False, "keyType": "rsa",
                                       "preferWildcard": False})
        sent = put.call_args.kwargs["json"]
        self.assertEqual(sent["keyType"], "rsa")
        self.assertFalse(sent["preferWildcard"])

    def test_a_refused_enable_is_a_failure_and_does_not_issue(self):
        out, _, post = self._da(put_status=403)
        self.assertEqual(out["status"], ssl_service.FAILED)
        post.assert_not_called()

    def test_a_domain_off_because_acme_is_disabled_no_longer_blames_a_licence(self):
        """That diagnosis was wrong, and repeating it would send someone to buy a
        licence they do not need."""
        self.assertNotIn("licence-gated", ssl_service._REASONS[ssl_service.DA_ACME_DISABLED])
        self.assertIn("per-domain", ssl_service._REASONS[ssl_service.DA_ACME_DISABLED])


class TestSubdomainsThatDoNotResolve(unittest.TestCase):
    """
    Found live. DirectAdmin offers a standard set of subnames in a certificate
    whether or not they exist, and every one has to validate -- so ftp, pop, smtp
    and autodiscover, which resolve nowhere, failed the whole order for aaatt.bt.
    One bad name loses the entire certificate, including the bare domain that
    would otherwise have been fine.

    This is why samchar.bt already carries a skip list on this server.
    """

    def test_names_that_resolve_nowhere_are_found(self):
        with patch("dns_check.resolve_ips", return_value=[]):
            out = ssl_service.unresolved_names("wank.bt")
        self.assertIn("ftp.wank.bt", out)
        self.assertIn("smtp.wank.bt", out)

    def test_a_name_that_resolves_is_not_skipped(self):
        with patch("dns_check.resolve_ips", return_value=["202.144.128.216"]):
            self.assertEqual(ssl_service.unresolved_names("wank.bt"), [])

    def test_a_lookup_failure_counts_as_unresolved(self):
        """A resolver timeout must not be read as "this name is fine"."""
        with patch("dns_check.resolve_ips", side_effect=OSError("resolver down")):
            self.assertIn("www.wank.bt", ssl_service.unresolved_names("wank.bt"))

    def test_the_bare_domain_is_never_skipped(self):
        with patch("dns_check.resolve_ips", return_value=[]):
            self.assertNotIn("wank.bt", ssl_service.unresolved_names("wank.bt"))

    def test_the_skip_list_reaches_directadmin(self):
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": False}
        put_resp = Mock(status_code=204, text="")
        post = Mock(status_code=200)
        post.json.return_value = {"acmeEnabled": True, "certsFulfilled": []}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.get", return_value=acme), \
             patch("ssl_service.requests.put", return_value=put_resp) as put, \
             patch("ssl_service.requests.post", return_value=post), \
             patch("dns_check.resolve_ips", return_value=[]):
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            ssl_service.enable_directadmin("wank.bt", "wank")
        sent = put.call_args.kwargs["json"]["skipDNSNames"]
        self.assertIn("ftp.wank.bt", sent)

    def test_an_existing_skip_list_is_kept(self):
        """An operator who deliberately excluded a name keeps their entry."""
        acme = Mock(status_code=200)
        acme.json.return_value = {"enabled": False, "skipDNSNames": ["blog.wank.bt"]}
        put_resp = Mock(status_code=204, text="")
        post = Mock(status_code=200)
        post.json.return_value = {"acmeEnabled": True, "certsFulfilled": []}
        with patch("ssl_service.settings.DIRECTADMIN") as cfg, \
             patch("ssl_service.requests.get", return_value=acme), \
             patch("ssl_service.requests.put", return_value=put_resp) as put, \
             patch("ssl_service.requests.post", return_value=post), \
             patch("dns_check.resolve_ips", return_value=["202.144.128.216"]):
            cfg.host, cfg.tls_hostname = "203.0.113.1", ""
            cfg.api_user, cfg.api_password = "admin", "pw"
            ssl_service.enable_directadmin("wank.bt", "wank")
        self.assertIn("blog.wank.bt", put.call_args.kwargs["json"]["skipDNSNames"])


class TestTheTimeoutIsNotTheCause(unittest.TestCase):
    def test_provisioning_allows_minutes(self):
        """The first guess of 120s was hit on a domain that already held a
        certificate, so it was not the hard case."""
        self.assertGreaterEqual(settings.SSL_PROVISION_TIMEOUT_SECONDS, 300)
