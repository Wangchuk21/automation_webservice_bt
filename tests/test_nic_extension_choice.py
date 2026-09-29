"""
Tests for the operator-chosen registry extension.

The extension used to be derived silently from the domain name. The dashboard
now offers the registry's own dropdown instead, because that dropdown is the
authority on what nic.bt.bt accepts. That adds one behaviour the derivation did
not have: the operator can pick an extension the domain name does not imply,
which means the extension carried by the name has to be stripped before the two
are recombined, or "wank.bt" chosen as .com.bt would be submitted as
"wank.com.bt" twice over.

Also covers the endpoint that serves the dropdown.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
import nic_client

# The six extensions the live nic.bt.bt dropdown offers.
LIVE = [".bt", ".com.bt", ".org.bt", ".gov.bt", ".edu.bt", ".net.bt"]

# The create form as the portal renders it: the empty option is labelled
# "Choose.." but carries the value NULL_VALUE, which is the trap.
FORM = """<form><select name="ext">
<option value="NULL_VALUE" selected>Choose..</option>
<option value=".bt">.bt</option>
<option value=".com.bt">.com.bt</option>
<option value=".org.bt">.org.bt</option>
<option value=".gov.bt">.gov.bt</option>
<option value=".edu.bt">.edu.bt</option>
<option value=".net.bt">.net.bt</option>
</select></form>"""


def _base_and_ext(domain, ext=None):
    """
    The part of register_or_update_domain that decides what gets submitted,
    without authenticating against the portal or writing anything.
    """
    base_domain, derived_ext = nic_client.split_domain_ext(domain)
    if ext:
        ext = ext.strip()
        if base_domain.lower().endswith(ext.lower()):
            base_domain = base_domain[: -len(ext)]
    else:
        ext = derived_ext
    return base_domain, ext


class TestExtensionChoice(unittest.TestCase):
    def test_derives_from_the_domain_when_none_is_chosen(self):
        self.assertEqual(_base_and_ext("wank.bt"), ("wank", ".bt"))
        self.assertEqual(_base_and_ext("wank.com.bt"), ("wank", ".com.bt"))

    def test_the_chosen_extension_wins(self):
        self.assertEqual(_base_and_ext("wank", ".com.bt"), ("wank", ".com.bt"))
        self.assertEqual(_base_and_ext("wank", ".net.bt"), ("wank", ".net.bt"))

    def test_a_mismatched_choice_is_honoured_not_corrected(self):
        """The operator is registering wank under .com.bt. Silently changing
        their answer to .bt would register something they did not ask for, so
        the choice stands and the dashboard preview shows it."""
        self.assertEqual(_base_and_ext("wank.bt", ".com.bt"), ("wank", ".com.bt"))

    def test_recombining_gives_the_domain_they_typed(self):
        """Round trip: stripping must not leave the extension doubled."""
        for domain, ext in [("wank.bt", ".com.bt"), ("wank.com.bt", ".com.bt"),
                            ("wank.edu.bt", ".edu.bt")]:
            base, chosen = _base_and_ext(domain, ext)
            self.assertEqual(f"{base}{chosen}", f"wank{ext}", domain)

    def test_whitespace_in_the_choice_is_tolerated(self):
        self.assertEqual(_base_and_ext("wank", " .bt "), ("wank", ".bt"))

    def test_choice_is_case_insensitive(self):
        self.assertEqual(_base_and_ext("wank.com.bt", ".COM.BT"), ("wank", ".COM.BT"))

    def test_every_derivable_extension_is_offered_by_the_portal(self):
        for domain in ["a.bt", "a.com.bt", "a.org.bt", "a.gov.bt",
                       "a.edu.bt", "a.net.bt"]:
            _, ext = _base_and_ext(domain)
            self.assertIn(ext, LIVE, domain)


class TestListExtensions(unittest.TestCase):
    def setUp(self):
        # Never let one test's cached list leak into the next.
        nic_client.NICClient._ext_cache = (None, 0.0)

    def tearDown(self):
        nic_client.NICClient._ext_cache = (None, 0.0)

    def test_serves_the_portals_own_options(self):
        """The real method, with only the two network steps replaced."""
        resp = types.SimpleNamespace(text=FORM)
        with patch.object(nic_client.NICClient, "login", return_value=(True, "")), \
             patch.object(nic_client, "_request_with_retry", return_value=resp):
            self.assertEqual(nic_client.NICClient.list_extensions(), LIVE)

    def test_a_second_call_is_served_from_cache(self):
        resp = types.SimpleNamespace(text=FORM)
        with patch.object(nic_client.NICClient, "login", return_value=(True, "")) as login, \
             patch.object(nic_client, "_request_with_retry", return_value=resp):
            nic_client.NICClient.list_extensions()
            nic_client.NICClient.list_extensions()
        self.assertEqual(login.call_count, 1, "should not log in on every page load")

    def test_falls_back_to_the_last_good_list_when_the_portal_fails(self):
        """A registry outage must not leave the operator unable to pick an
        extension, and must not invent one either."""
        nic_client.NICClient._ext_cache = (LIVE, __import__("time").time())
        with patch.object(nic_client.NICClient, "__init__", side_effect=OSError("registry down")):
            self.assertEqual(nic_client.NICClient.list_extensions(), LIVE)

    def test_empty_when_never_read(self):
        with patch.object(nic_client.NICClient, "__init__", side_effect=OSError("no route")):
            self.assertEqual(nic_client.NICClient.list_extensions(), [])

    def test_a_failed_login_does_not_cache_an_empty_list(self):
        with patch.object(nic_client.NICClient, "login", return_value=(False, "bad password")):
            self.assertEqual(nic_client.NICClient.list_extensions(), [])
        self.assertIsNone(nic_client.NICClient._ext_cache[0])


class TestDashboardAgreesWithTheServer(unittest.TestCase):
    """
    The dashboard previews the payload before anything is sent, so its splitter
    has to agree with the server's. The two are separate implementations in
    separate languages and cannot be compared by running them together, so
    these assert on the source: the behaviours most likely to drift.

    The preview is not load-bearing -- a disagreement shows a wrong preview, and
    the submission still goes through the server. It is still worth catching,
    because the operator reads the preview as the truth.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent.parent
                 / "static" / "js" / "app.js").read_text()

    def _body(self):
        start = self.js.index("function splitDomainExt")
        return self.js[start:self.js.index("\n}\n", start)]

    def test_uses_the_offered_list_not_a_hardcoded_one(self):
        body = self._body()
        self.assertIn("offered", body)
        self.assertNotIn('".com.bt"', body,
                         "the dashboard must not carry its own extension list")

    def test_fallback_default_matches_the_server(self):
        base, ext = nic_client.split_domain_ext("bt")
        self.assertIn(f'"{ext}"', self._body(),
                      "the dashboard's no-dot fallback must equal the server's")

    def test_normalises_case_and_trailing_dots(self):
        base, ext = nic_client.split_domain_ext("  Wank.BT.  ")
        self.assertEqual(base, "wank")
        body = self._body()
        self.assertIn("toLowerCase()", body)
        # The JS source line is: .replace(/\.+$/, "")
        self.assertIn(r"/\.+$/", body)
        self.assertIn('replace(', body)

    def test_a_manual_choice_is_not_overridden_by_the_domain(self):
        """The generated fields must respect a deliberate pick too. Tracked
        per instance now, since there are two of these forms on the page."""
        self.assertTrue("this.touched" in self.js)
        self.assertTrue("if (this.touched.has(f.name)) return;" in self.js)


class TestExtensionsEndpoint(unittest.TestCase):

    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def test_returns_the_options(self):
        with patch.object(nic_client.NICClient, "list_extensions",
                          return_value=LIVE):
            r = self.client.get("/api/v1/nic/extensions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["extensions"], LIVE)
        self.assertTrue(r.json()["available"])

    def test_unavailable_is_reported_not_hidden(self):
        with patch.object(nic_client.NICClient, "list_extensions", return_value=[]):
            r = self.client.get("/api/v1/nic/extensions")
        body = r.json()
        self.assertEqual(body["extensions"], [])
        self.assertFalse(body["available"])
        self.assertIn("derived", body["message"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRegistryFieldsAreNotDuplicated(unittest.TestCase):
    """
    The provisioning form once carried a panel listing all 23 registry fields
    as a read-only mirror of the fields directly above it. Every value was
    therefore shown twice, and the operator had two places to look for the same
    thing. The fields above are the only place registry details are captured;
    the full list is a reference on its own page.

    These hold that line: the form captures, the page explains, and neither
    re-implements the other.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.index = (root / "templates" / "index.html").read_text()
        cls.ref = (root / "templates" / "registry_fields.html").read_text()
        cls.js = (root / "static" / "js" / "app.js").read_text()

    def test_the_old_read_only_mirror_is_gone(self):
        """It duplicated the form's own fields rather than adding anything."""
        for gone in ("nic_spec_fields", "nic_spec_payload", 'id="nic_spec"'):
            self.assertNotIn(gone, self.index)

    def test_the_form_renders_every_field_from_the_spec(self):
        """Not eight hand-written inputs, and not a hardcoded 23 either: the
        form is generated from the same list the client submits, so a field the
        registry gains appears without anyone editing the template."""
        self.assertIn('id="nic-reg-form"', self.index)
        self.assertIn("/api/v1/nic/field-spec", self.js)
        self.assertIn("/api/v1/nic/extensions", self.js)
        for name in ("customername", "tech_fax", "billing_email", "reg_renewal"):
            self.assertNotIn(f'name="{name}"', self.index,
                             f"{name} is written into the template by hand")

    def test_the_form_submits_the_whole_field_set(self):
        self.assertIn("hostingRegistry.values()", self.js)
        self.assertIn("nic_fields: nicFields", self.js)

    def test_the_reference_page_does_not_re_capture_the_fields(self):
        """It must not offer inputs for the customer's details, or the
        duplication returns in the other direction."""
        for field in ('id="customer_name"', 'id="phone"', 'id="address"',
                      'id="postal_code"', 'id="renewal_date"'):
            self.assertNotIn(field, self.ref,
                             f"{field} would capture the value a second time")

    def test_the_reference_page_reads_the_same_spec_and_extension_sources(self):
        self.assertIn("/api/v1/nic/field-spec", self.ref)
        self.assertIn("/api/v1/nic/extensions", self.ref,
                      "the extension list must come from the registry, not a copy")

    def test_dead_javascript_is_gone(self):
        """The removed panel's renderers had no elements left to bind to."""
        for gone in ("renderNicPayload", "syncNicMirrors", "loadNicSpec",
                     "NIC_SPEC", "syncNicExtFromDomain", "loadNicExtensions"):
            self.assertFalse(gone in self.js, f"{gone} is dead code now")

    def test_the_extension_still_comes_from_the_registry(self):
        """The selector is now one of the generated fields rather than a
        hand-written one, but it must still be a select fed by the portal."""
        self.assertIn('data-reg="ext"', self.js)
        self.assertIn("<select", self.js)
        self.assertIn("REG_EXTENSIONS", self.js)

    def test_edited_fields_are_not_overwritten(self):
        """A field the operator changed by hand must survive a later domain
        edit, or the override silently reverts."""
        self.assertTrue("this.touched.has(f.name)" in self.js)
        self.assertTrue("this.touched.clear()" in self.js)


class TestRegistryFieldsPageIsServed(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def test_the_page_loads(self):
        r = self.client.get("/registry-fields")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_it_reads_both_sources_the_client_does(self):
        body = self.client.get("/registry-fields").text
        self.assertIn("/api/v1/nic/field-spec", body)
        self.assertIn("/api/v1/nic/extensions", body)

    def test_it_offers_a_way_back(self):
        self.assertIn('href="/"', self.client.get("/registry-fields").text)
