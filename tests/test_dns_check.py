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
    check_forwarding,
    split_target,
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
        cls.index = (root / "templates" / "index.html").read_text()

    def test_provisioning_does_not_call_the_check(self):
        # The endpoint's call, and nothing else. Any other call site would mean
        # a lookup had crept back onto the provisioning path.
        self.assertEqual(self.app.count("check_domain(domain, panel=panel"), 1)
        self.assertNotIn("check_domain(result.domain", self.app)

    def test_the_check_runs_before_anything_is_created(self):
        """The point of moving it: a domain that does not point here should be
        known before an account exists for it."""
        self.assertTrue("scheduleDnsPrecheck" in self.js)
        self.assertTrue("dns-precheck" in self.index)
        body = self.js.split("function scheduleDnsPrecheck()")[1][:900]
        self.assertIn("runDnsPrecheck", body)
        self.assertIn("setTimeout", body)

    def test_the_precheck_is_never_a_block(self):
        """DNS is routinely pointed at a new account after the account is made.
        Refusing to provision on an unresolvable domain would break the normal
        order of work, and our own resolver failing must not block anyone.

        Checked by reading only the part of the submit handler that runs before
        the request is sent: it must contain no DNS logic and no early exit.
        """
        body = self.js.split("async function handleProvisionSubmit")[1]
        before_request = body.split("const response = await fetch")[0]
        self.assertNotIn("dns", before_request.lower().replace("nic", ""),
                         "DNS must not appear in the path that can block a submit")

    def test_the_handover_kit_reuses_the_precheck(self):
        """The same domain seconds later; a second lookup would only add a wait
        and could disagree with what was already shown."""
        self.assertTrue("LAST_DNS" in self.js)
        self.assertTrue("pre.domain === result.data.domain" in self.js)

    def test_a_stale_in_flight_result_is_discarded(self):
        """Typing continues while a lookup is in flight; a late reply for an
        earlier domain must not overwrite the current one."""
        self.assertTrue("dnsPrecheckSeq" in self.js)
        self.assertTrue("if (seq !== dnsPrecheckSeq" in self.js)

    def test_the_standalone_checker_still_works(self):
        """Now that the form pre-checks, the separate box is for a domain
        unrelated to the one being provisioned."""
        self.assertTrue("checkDnsFromToolbar" in self.js)
        self.assertTrue('id="dns-check"' in self.index)

    def test_a_failed_check_shows_a_status_rather_than_an_error(self):
        """The operator must not read a DNS problem as a provisioning problem."""
        self.assertIn("This does not affect provisioning", self.js)

    def test_an_unexpected_error_becomes_a_status_not_a_500(self):
        # Anchored to the endpoint's own body: app.py has other except
        # handlers, and scanning the whole file finds the wrong one.
        body = self.app.split('@app.get("/api/v1/dns/check"')[1].split("@app.get(", 1)[0]
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


def js_function(source: str, name: str, signature: bool = False) -> str:
    """One JS function, by brace matching.

    Reading a fixed line range out of a script breaks the moment a line is added
    above, and then asserts against the wrong lines while still passing.

    The slice starts at the opening brace, so the signature is excluded. Pass
    signature=True to get the declaration line as well, for when the parameters
    are the thing under test.
    """
    start = source.index(f"function {name}(")
    brace = source.index("{", source.index(")", start))
    if signature:
        # From the start of the line through the opening brace, so the modifiers
        # ("async") and the parameters are both included. Taking just the line up
        # to "function" drops the parameters, and the signature then reads "async {".
        head = source[source.rindex("\n", 0, start) + 1:brace]
    else:
        head = ""
    start = brace
    depth, i = 0, start
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return head + source[start:i + 1]
        i += 1
    raise AssertionError(f"{name} has no closing brace")


class TestThePrecheckRendersSomewhereVisible(unittest.TestCase):
    """
    The operator typed a domain and the panel said "Checking DNS..." for good,
    with no result ever appearing.

    renderDns was handed the domain text input instead of the precheck box. The
    record went into an <input>, where innerHTML is not rendered, so it was
    visible nowhere; and because the input's className was overwritten on the way,
    the field itself picked up the green result styling. The box was never
    touched, so it kept the placeholder written before the request went out.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_the_result_goes_into_the_box_it_was_given(self):
        body = js_function(self.js, "runDnsPrecheck")
        self.assertIn("renderDns(out,", body,
                      "the precheck must render into the box passed to it")
        self.assertNotIn("renderDns(box,", body)

    def test_the_field_is_only_read_for_the_staleness_check(self):
        """It is legitimate to read the input back, to drop a result the operator
        has already typed past. It is not legitimate to write to it."""
        body = js_function(self.js, "runDnsPrecheck")
        for line in body.splitlines():
            stripped = line.strip()
            if "getElementById(\"domain\")" in stripped:
                self.assertNotIn("renderDns", stripped)
                self.assertIn("const field =", stripped,
                              "the field must be a distinct name from the box")
        self.assertIn("field.value", body,
                      "the staleness guard must still compare against the field")

    def test_no_shadowing_declaration_inside_the_function(self):
        """`const box` inside a function whose parameter is `box` silently
        reassigns the target. This is the bug itself."""
        body = js_function(self.js, "runDnsPrecheck")
        for decl in ("const box =", "let box ="):
            self.assertNotIn(decl, body,
                             f"`{decl}` shadows the box parameter and is the bug")

    def test_the_error_path_uses_the_box_too(self):
        """It already did, which is why the placeholder was the only thing that
        ever changed -- the two branches disagreed about what they were writing."""
        body = js_function(self.js, "runDnsPrecheck")
        self.assertIn("out.className =", body)
        self.assertIn("out.innerHTML =", body)

    def test_the_box_the_precheck_writes_to_exists(self):
        self.assertIn('id="dns-precheck"', self.html)

    def test_every_box_renderdns_touches_keeps_its_own_styling(self):
        """
        Three boxes share the renderer. Keying the base class off one exact id
        meant the other two were restyled as a toolbar result, losing the
        border and padding that makes them read as part of the form.
        """
        body = js_function(self.js, "renderDns")
        self.assertIn("classList.contains(\"res-dns-status\")", body,
                      "the base class must be matched by class, not by one id")
        self.assertNotIn('box.id === "res-dns-status"', body)
        boxes = [ln for ln in self.html.splitlines() if 'class="res-dns-status' in ln]
        self.assertGreaterEqual(len(boxes), 2,
                                "expected several boxes to share the renderer")


class TestSplittingAPastedTarget(unittest.TestCase):
    """
    "lina.ns.cloudflare.com.sleo.ns.cloudflare.com." was registered for
    goldentakinholidays.bt, so the check compared against one hostname that can
    never exist and the domain sat on mismatch for ever.

    Commas and semicolons already separated. Whitespace did not, and a name server
    list copied from a registrar or a ticket very often arrives space-separated.
    """

    def test_commas_separate(self):
        self.assertEqual(
            split_target("lina.ns.cloudflare.com, sleo.ns.cloudflare.com"),
            ["lina.ns.cloudflare.com", "sleo.ns.cloudflare.com"])

    def test_spaces_separate(self):
        self.assertEqual(
            split_target("lina.ns.cloudflare.com sleo.ns.cloudflare.com"),
            ["lina.ns.cloudflare.com", "sleo.ns.cloudflare.com"])

    def test_semicolons_separate(self):
        self.assertEqual(
            split_target("lina.ns.cloudflare.com;sleo.ns.cloudflare.com"),
            ["lina.ns.cloudflare.com", "sleo.ns.cloudflare.com"])

    def test_a_trailing_dot_does_not_prevent_a_match(self):
        """Valid FQDN notation, and DNS would accept it, but comparing it as a
        string against the same name without the dot reports a difference that is
        not there."""
        self.assertEqual(split_target("ns1.example.com."),
                         ["ns1.example.com"])

    def test_extra_separators_do_not_produce_empty_values(self):
        self.assertEqual(split_target("a.b,  ; c.d,"), ["a.b", "c.d"])

    def test_case_is_normalised(self):
        self.assertEqual(split_target("NS1.Example.COM"), ["ns1.example.com"])

    def test_a_dot_joined_pair_is_left_alone(self):
        """
        Deliberately not split. It is a syntactically valid hostname, and guessing
        where a name ends and the next begins would invent a target nobody asked
        for. Caught by showing the operator the parse instead.
        """
        self.assertEqual(
            split_target("lina.ns.cloudflare.com.sleo.ns.cloudflare.com."),
            ["lina.ns.cloudflare.com.sleo.ns.cloudflare.com"])


class TestAPartialMatchIsNotReportedAsRequested(unittest.TestCase):
    """
    Found while correcting goldentakinholidays.bt.

    The domain is delegated to alec.ns.cloudflare.com and lina.ns.cloudflare.com.
    The operator asked for lina and sleo. The old rule accepted any overlap and
    then said "as requested" -- so a green result sat next to a name that was
    never in place, and the operator had no way to learn that before telling a
    customer.

    It still counts as forwarded: registrars rotate and add nameservers, and
    blocking here would leave a correctly working domain unconfirmable. What it no
    longer does is claim the request was met.
    """

    def _check(self, expected):
        with patch("dns_check.resolve_ns",
                   return_value=["alec.ns.cloudflare.com", "lina.ns.cloudflare.com"]):
            return check_forwarding("x.bt", "nameserver", expected)

    def test_it_still_counts_as_forwarded(self):
        self.assertEqual(self._check("lina.ns.cloudflare.com").get("status"), "forwarded")

    def test_it_does_not_claim_it_was_as_requested(self):
        msg = self._check("lina.ns.cloudflare.com, sleo.ns.cloudflare.com")["message"]
        self.assertNotIn("as requested", msg)

    def test_it_names_the_name_that_is_not_there(self):
        r = self._check("lina.ns.cloudflare.com, sleo.ns.cloudflare.com")
        self.assertTrue(r.get("partial"))
        self.assertEqual(r["missing"], ["sleo.ns.cloudflare.com"])
        self.assertIn("sleo.ns.cloudflare.com", r["message"])

    def test_it_names_what_is_in_place_instead(self):
        r = self._check("lina.ns.cloudflare.com, sleo.ns.cloudflare.com")
        self.assertEqual(r["extra"], ["alec.ns.cloudflare.com"])
        self.assertIn("alec.ns.cloudflare.com", r["message"])

    def test_an_exact_match_still_says_as_requested(self):
        r = self._check("alec.ns.cloudflare.com, lina.ns.cloudflare.com")
        self.assertIn("as requested", r["message"])
        self.assertFalse(r.get("partial"))
        self.assertIsNone(r.get("missing"))

    def test_a_complete_non_overlap_is_still_a_mismatch(self):
        with patch("dns_check.resolve_ns",
                   return_value=["alec.ns.cloudflare.com", "lina.ns.cloudflare.com"]):
            r = check_forwarding("x.bt", "nameserver", "ns1.elsewhere.com")
        self.assertEqual(r["status"], "mismatch")
        self.assertFalse(r.get("partial"))

    def test_the_row_distinguishes_a_partial_match(self):
        """A green forwarded next to a name that was never in place is the same
        overstatement in the dashboard."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "app.js").read_text()
        self.assertIn("r.last_check_partial", js_function(js, "loadDomainServiceQueue"))
