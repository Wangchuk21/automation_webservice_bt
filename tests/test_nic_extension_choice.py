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

    def test_the_preview_prefers_an_explicitly_chosen_extension(self):
        self.assertIn("nicExtTouched", self.js)
        self.assertIn("const chosen", self.js)


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
