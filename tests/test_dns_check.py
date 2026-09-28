"""
Tests for the DNS mapping check.

The distinction being tested is the one that matters after a provisioning: a
created hosting account and a domain that reaches it are different states, and
the handover kit looks identical in both. "Not mapped" and "unresolved" are
reported separately because they need different fixes -- one needs the A record
changed, the other needs it created.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
import dns_check
from dns_check import (
    MAPPED, NOT_MAPPED, UNRESOLVED, check_domain, hosting_addresses, resolve_ips,
)

OURS = "202.144.128.216"
THEIRS = "203.0.113.9"


def fake_resolver(mapping):
    """resolve_ips backed by a dict instead of the real resolver."""
    def _resolve(domain, timeout=dns_check.RESOLVE_TIMEOUT):
        return list(mapping.get((domain or "").strip().lower(), []))
    return _resolve


class TestResolveIps(unittest.TestCase):
    def test_returns_unique_addresses_in_order(self):
        """A round-robin record should not print the same address three times."""
        with patch.object(dns_check.socket, "getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.51.100.1", 0)),
            (2, 1, 6, "", ("198.51.100.2", 0)),
            (2, 1, 6, "", ("198.51.100.1", 0)),
        ]):
            self.assertEqual(resolve_ips("x.bt"), ["198.51.100.1", "198.51.100.2"])

    def test_no_address_record_is_an_empty_list(self):
        with patch.object(dns_check.socket, "getaddrinfo", side_effect=socket.gaierror):
            self.assertEqual(resolve_ips("x.bt"), [])

    def test_a_nameserver_that_never_answers_is_also_empty(self):
        """From the hosting side these are the same problem, and neither is
        worth a stack trace in front of an operator."""
        with patch.object(dns_check.socket, "getaddrinfo", side_effect=OSError("timeout")):
            self.assertEqual(resolve_ips("x.bt"), [])

    def test_blank_and_trailing_dot_are_handled(self):
        self.assertEqual(resolve_ips(""), [])
        self.assertEqual(resolve_ips("   "), [])
        with patch.object(dns_check.socket, "getaddrinfo",
                          return_value=[(2, 1, 6, "", (OURS, 0))]) as g:
            resolve_ips("  Wank.BT.  ")
        self.assertEqual(g.call_args[0][0], "wank.bt")

    def test_ipv6_is_reported_too(self):
        with patch.object(dns_check.socket, "getaddrinfo",
                          return_value=[(10, 1, 6, "", ("2001:db8::1", 0, 0, 0))]):
            self.assertEqual(resolve_ips("x.bt"), ["2001:db8::1"])


class TestHostingAddresses(unittest.TestCase):
    def test_the_configured_server_address_counts_as_ours(self):
        with patch.object(dns_check, "resolve_ips", fake_resolver({})):
            self.assertIn(app_module.settings.CPANEL.host, hosting_addresses("cpanel"))
            self.assertIn(app_module.settings.DIRECTADMIN.server_ip,
                          hosting_addresses("directadmin"))

    def test_extra_shared_addresses_can_be_added(self):
        """A shared-hosting address is not always the control panel's own."""
        with patch.object(dns_check, "resolve_ips", fake_resolver({})), \
             patch.object(app_module.settings, "HOSTING_SERVER_IPS", "198.51.100.7,198.51.100.8"):
            got = hosting_addresses("cpanel")
        self.assertIn("198.51.100.7", got)
        self.assertIn("198.51.100.8", got)

    def test_hostnames_in_the_setting_are_resolved_not_ignored(self):
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"panel.example": ["198.51.100.9"]})), \
             patch.object(app_module.settings, "HOSTING_SERVER_IPS", "panel.example"):
            self.assertIn("198.51.100.9", hosting_addresses("cpanel"))


class TestClassification(unittest.TestCase):
    """The three outcomes need different fixes, so they must not collapse."""

    def _check(self, resolved, **kw):
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"wank.bt": resolved})), \
             patch.object(dns_check, "hosting_addresses", return_value=[OURS]):
            return check_domain("wank.bt", "cpanel", **kw)

    def test_mapped_when_one_address_is_ours(self):
        self.assertEqual(self._check([THEIRS, OURS], probe=False)["status"], MAPPED)

    def test_not_mapped_when_the_address_is_someone_elses(self):
        got = self._check([THEIRS], probe=False)
        self.assertEqual(got["status"], NOT_MAPPED)
        self.assertIn(THEIRS, got["message"])
        self.assertIn(OURS, got["message"])

    def test_unresolved_when_there_is_no_address_record(self):
        self.assertEqual(self._check([], probe=False)["status"], UNRESOLVED)

    def test_the_two_failure_modes_read_differently(self):
        not_mapped = self._check([THEIRS], probe=False)["message"]
        unresolved = self._check([], probe=False)["message"]
        self.assertNotEqual(not_mapped, unresolved)
        self.assertIn("A record", unresolved, "one needs a record created")


class TestPortProbe(unittest.TestCase):
    """The probe confirms the web server answers, but must never be pointed at a
    third party's address -- this host should not be connecting out to servers
    named by a customer's DNS on the strength of a provisioning check."""

    def _check(self, resolved):
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"wank.bt": resolved})), \
             patch.object(dns_check, "hosting_addresses", return_value=[OURS]), \
             patch.object(dns_check, "_port_answers", return_value=True) as probe:
            got = check_domain("wank.bt", "cpanel", probe=True)
        return got, probe

    def test_probes_our_own_address_when_mapped(self):
        got, probe = self._check([OURS])
        self.assertTrue(got["web_answers"])
        self.assertEqual(probe.call_args[0][0], OURS)

    def test_never_probes_an_address_that_is_not_ours(self):
        _, probe = self._check([THEIRS])
        probe.assert_not_called()

    def test_no_probe_when_disabled(self):
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"wank.bt": [OURS]})), \
             patch.object(dns_check, "hosting_addresses", return_value=[OURS]), \
             patch.object(dns_check, "_port_answers") as probe:
            check_domain("wank.bt", "cpanel", probe=False)
        probe.assert_not_called()

    def test_a_quiet_port_is_not_reported_as_a_failure(self):
        """A brand new account has no content, so a closed port is expected."""
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"wank.bt": [OURS]})), \
             patch.object(dns_check, "hosting_addresses", return_value=[OURS]), \
             patch.object(dns_check, "_port_answers", return_value=False):
            got = check_domain("wank.bt", "cpanel", probe=True)
        self.assertEqual(got["status"], MAPPED)
        self.assertFalse(got["web_answers"])
        self.assertIn("normal", got["message"])


class TestCheckDomainEdgeCases(unittest.TestCase):
    def test_a_blank_domain_says_so_rather_than_resolving_something(self):
        got = check_domain("   ")
        self.assertEqual(got["status"], UNRESOLVED)
        self.assertIn("No domain", got["message"])

    def test_a_resolver_that_hangs_does_not_hold_the_request(self):
        """Called from the page straight after a provisioning; a nameserver
        that never answers must not block it."""
        import time
        with patch.object(dns_check, "resolve_ips", side_effect=lambda *a, **k: time.sleep(60)), \
             patch.object(dns_check, "CHECK_DEADLINE", 0.5):
            started = time.time()
            got = check_domain("slow.bt")
            elapsed = time.time() - started
        self.assertEqual(got["status"], UNRESOLVED)
        self.assertIn("did not answer", got["message"])
        # The bug this covers: leaving via "with ThreadPoolExecutor(...)" joins
        # the stuck worker on the way out, so the deadline returned but the
        # response was still held for the full length of the hang.
        self.assertLess(elapsed, 5, "the deadline did not actually release the request")

    def test_the_domain_is_normalised(self):
        with patch.object(dns_check, "resolve_ips",
                          fake_resolver({"wank.bt": [OURS]})), \
             patch.object(dns_check, "hosting_addresses", return_value=[OURS]):
            got = check_domain("  WANK.BT  ", probe=False)
        self.assertEqual(got["domain"], "wank.bt")


class TestDnsEndpoint(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def test_reports_the_status(self):
        with patch.object(app_module, "check_domain",
                          return_value={"domain": "wank.bt", "status": MAPPED,
                                        "resolved": [OURS], "ours": [OURS],
                                        "web_answers": True, "message": "ok"}) as call:
            r = self.client.get("/api/v1/dns/check", params={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], MAPPED)
        self.assertEqual(call.call_args.kwargs["panel"], "cpanel")

    def test_the_panel_is_passed_through(self):
        with patch.object(app_module, "check_domain",
                          return_value={"domain": "wank.bt", "status": MAPPED,
                                        "resolved": [], "ours": [], "message": ""}) as call:
            self.client.get("/api/v1/dns/check",
                            params={"domain": "wank.bt", "panel": "directadmin"})
        self.assertEqual(call.call_args.kwargs["panel"], "directadmin")

    def test_a_missing_domain_is_rejected(self):
        r = self.client.get("/api/v1/dns/check", params={"domain": "  "})
        self.assertEqual(r.status_code, 422)


class TestItCannotBlockOrFailProvisioning(unittest.TestCase):
    """
    A DNS lookup is informational. The asked-for guarantee is that an unresolvable
    domain neither stops nor undoes anything, and the second is the one that
    matters: a lookup that failed *after* the account was created would return an
    error, the dashboard would report "Provisioning Failed", and an operator
    retrying would create a duplicate account.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.app = (root / "app.py").read_text()
        cls.js = (root / "static" / "js" / "app.js").read_text()

    def test_provisioning_does_not_call_the_check(self):
        # The endpoint's call, and nothing else. Any other call site would mean
        # a lookup had crept back onto the provisioning path.
        self.assertEqual(self.app.count("check_domain(domain, panel=panel"), 1)
        self.assertNotIn("check_domain(result.domain", self.app)

    def test_the_check_is_requested_after_the_account_exists(self):
        self.assertIn("checkDnsAfterProvisioning", self.js)
        body = self.js.split("async function checkDnsAfterProvisioning")[1][:1200]
        self.assertIn("renderDns", body)
        self.assertIn("catch", body)

    def test_a_failed_check_shows_a_status_rather_than_an_error(self):
        """The operator must not read a DNS problem as a provisioning problem."""
        self.assertIn("The hosting account is unaffected", self.js)

    def test_an_unexpected_error_becomes_a_status_not_a_500(self):
        # Anchored to the endpoint's own body: app.py has other except
        # handlers, and scanning the whole file finds the wrong one.
        body = self.app.split('@app.get("/api/v1/dns/check"')[1].split("\n\n\n")[0]
        self.assertIn("except Exception as e:", body)
        self.assertIn('"status": "error"', body)

    def test_the_endpoint_never_raises_to_the_caller(self):
        import app as app_module
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        try:
            client = TestClient(app_module.app, raise_server_exceptions=False)
            with patch.object(app_module, "check_domain", side_effect=RuntimeError("boom")):
                r = client.get("/api/v1/dns/check", params={"domain": "wank.bt"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "error")
            self.assertIn("unaffected", r.json()["message"])
        finally:
            app_module.app.dependency_overrides = {}


if __name__ == "__main__":
    unittest.main(verbosity=2)
