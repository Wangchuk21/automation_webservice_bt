"""
Tests for the read-only BSCS client.

These never contact the portal. They use fake responses that mirror the HTML
observed on the live system, so the parsing and session logic can be checked
without pulling real customer records.

The read-only guarantee is itself tested: if somebody later adds an
activate/deactivate method, that test fails. We do not hold the rights to
change contracts, so the client must not grow the ability.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import bscs_client as bscs
from bscs_client import BSCSClient, BSCSError

# Mirrors the real login redirect: SolutionUnitServlet with a SuToken.
# Distinctive so a test can assert it never appears in a log line.
DISTINCTIVE_PW = "Zq7-Curver-Gloss-4412"

LOGIN_OK = (
    '<html><body><a href="/custcare_cu/SolutionUnitServlet?StartSu=true'
    '&SuName=HomeSU&SuToken=aaaabbbbccccddddeeeeffff00001111'
    '&RequestTimeStamp=1790480014703">Home</a></body></html>'
)
LOGIN_FAIL = '<html><body><form action="Login.au"><p>Invalid username or password</p></form></body></html>'

# Mirrors the customer search form: hidden IVP_* echoes plus criteria inputs.
SEARCH_FORM = """<html><body><form action="SolutionUnitServlet?SuStepName=CustomerSearch_Step">
<input type="hidden" name="IVP_CS_ID_PUB" value="">
<input type="hidden" name="IVP_CS_CODE" value="">
<input type="hidden" name="IVP_ADR_NAME" value="">
<input type="hidden" name="IVP_RESOURCE" value="PARTY">
<input type="hidden" name="IVP_SRCH_COUNT" value="10">
<input type="hidden" name="FW_SubmittedFormPath" value="/x">
<input name="CS_ID_PUB" type="text">
<input name="Search_Button" type="submit" value="Search">
</form></body></html>"""

# Mirrors what the portal actually returns for a search that matched: a
# "Search results" heading, a "<shown> / <total>" counter, and a grid whose
# headers are Customer code / Public key / Customer / City / Street / Status.
RESULTS = """<html><body>
<h2>Search results</h2>
<table><thead><tr><th>Customer code</th><th>Public key</th><th>Customer</th>
<th>City</th><th>Street</th><th>Status</th></tr></thead>
<tbody>
<tr><td>100001</td><td>CUST-1</td><td>Test Customer One</td><td>Thimphu</td><td>Main St</td><td>Active</td></tr>
<tr><td>100002</td><td>CUST-2</td><td>Test Customer Two</td><td>Paro</td><td>Side Rd</td><td>Suspended</td></tr>
</tbody></table>
<div>2 / 2</div></body></html>"""

EMPTY_RESULTS = "<html><body><p>No records found</p></body></html>"

# What the portal actually returns after a search that matched nothing: a
# "Search results" heading and a "0 / 0" counter, with no data rows.
RESULTS_BUT_NO_MATCHES = """<html><body>
<h2>Search results</h2>
<table><thead><tr><th>Customer code</th><th>Public key</th><th>Customer</th>
<th>City</th><th>Street</th><th>Status</th></tr></thead>
<tbody></tbody></table>
<div>0 / 0</div></body></html>"""

# What the portal returns when it ignores the request and re-renders the form.
FORM_ECHO = SEARCH_FORM


class FakeResponse:
    def __init__(self, text, status_code=200, url="http://portal/custcare_cu/"):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeSession:
    """Scripted session. `responses` is a list consumed in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.verify = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0) if self.responses else FakeResponse("")
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses):
    c = BSCSClient(base_url="http://portal/custcare_cu", username="u", password=DISTINCTIVE_PW,
                   timeout=1, verify_ssl=False)
    c.session = FakeSession(responses)
    return c


def login_script(form_html=SEARCH_FORM, results=RESULTS, step=bscs.CUSTOMER_SEARCH_STEP):
    """
    Build the full response sequence for one search.

    Authenticating costs two requests (GET the login page, then POST the
    form), then the Solution Unit is started, then the step is posted -- four
    in total. Handing the client a shorter list silently returns empty
    responses, which reads as a parsing bug rather than a fixture mistake.
    """
    return [
        FakeResponse("<html/>"),                        # 1. GET /login
        FakeResponse(LOGIN_OK),                         # 2. POST Login.au
        FakeResponse(form_html),                        # 3. GET PartySearchSU.su
        FakeResponse(results),                          # 4. POST step
    ]


class TestReadOnlyGuarantee(unittest.TestCase):
    """We have no rights to write to BSCS, so the client must not offer it."""

    FORBIDDEN = ("activate", "deactivate", "suspend", "modify", "update",
                 "create", "delete", "remove", "assign", "charge", "post")

    def test_no_write_capable_methods(self):
        offenders = [
            name for name in dir(BSCSClient)
            if not name.startswith("_") and any(k in name.lower() for k in self.FORBIDDEN)
        ]
        self.assertEqual(
            offenders, [],
            f"BSCSClient must stay read-only, found write-capable methods: {offenders}",
        )

    def test_module_exposes_no_write_helper(self):
        offenders = [
            name for name in dir(bscs)
            if not name.startswith("_") and any(k in name.lower() for k in self.FORBIDDEN)
        ]
        self.assertEqual(offenders, [], f"write helper present in module: {offenders}")

    def test_source_mentions_read_only(self):
        source = Path(bscs.__file__).read_text()
        self.assertIn("READ-ONLY", source.upper())


class TestLogin(unittest.TestCase):
    def test_login_captures_token(self):
        c = make_client([FakeResponse("<html/>"), FakeResponse(LOGIN_OK)])
        ok, msg = c.login()
        self.assertTrue(ok)
        self.assertEqual(c._su_token, "aaaabbbbccccddddeeeeffff00001111")
        self.assertNotIn("p", msg.split("as")[-1].strip(" ."), "message should mask the username")

    def test_login_failure_detected(self):
        c = make_client([FakeResponse("<html/>"), FakeResponse(LOGIN_FAIL)])
        ok, msg = c.login()
        self.assertFalse(ok)
        self.assertIn("session token", msg.lower())

    def test_login_http_error(self):
        c = make_client([FakeResponse("<html/>"), FakeResponse("nope", status_code=500)])
        ok, msg = c.login()
        self.assertFalse(ok)
        self.assertIn("500", msg)

    def test_missing_credentials_reported_without_leaking(self):
        c = BSCSClient(base_url="http://portal/custcare_cu", username="", password="")
        ok, msg = c.login()
        self.assertFalse(ok)
        self.assertIn("not configured", msg)

    def test_password_never_logged(self):
        import logging
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("bscs_client")
        handler = Capture()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            c = make_client([FakeResponse("<html/>"), FakeResponse(LOGIN_OK)])
            c.login()
        finally:
            logger.removeHandler(handler)
        blob = " ".join(records)
        self.assertNotIn(c.password, blob, "password must never reach the logs")


class TestSessionAndToken(unittest.TestCase):
    def test_token_rotates_and_is_re_read(self):
        """
        The portal issues a new SuToken on every Solution Unit start. A client
        that caches one breaks on the second call, so this is the behaviour
        most worth pinning.
        """
        first = LOGIN_OK.replace("aaaabbbbccccddddeeeeffff00001111", "TOKEN-ONE-0000000000")
        second_url = ("http://portal/custcare_cu/SolutionUnitServlet?StartSu=true"
                      "&SuName=PartySearchSU&SuToken=TOKEN-TWO-000000000000")
        c = make_client([
            FakeResponse("<html/>"),                  # 1. GET /login
            FakeResponse(first),                      # 2. POST Login.au
            FakeResponse(SEARCH_FORM, url=second_url)  # 3. GET the SU
        ])
        c.login()
        self.assertEqual(c._su_token, "TOKEN-ONE-0000000000")
        c._start_su(bscs.CUSTOMER_SEARCH_SU, bscs.CUSTOMER_SEARCH_ALIAS)
        self.assertEqual(c._su_token, "TOKEN-TWO-000000000000")

    def test_ensure_session_logs_in_when_not_authenticated(self):
        c = make_client([FakeResponse("<html/>"), FakeResponse(LOGIN_OK)])
        self.assertTrue(c._ensure_session())
        self.assertEqual(c.session.calls[0][0], "GET")

    def test_redirect_to_login_marks_session_lost(self):
        c = make_client([FakeResponse("<html/>"), FakeResponse(LOGIN_OK)])
        c.login()
        c._logged_in = True
        c._request("GET", "http://portal/custcare_cu/whatever")
        c.session.responses = [FakeResponse("<html/>", url="http://portal/custcare_cu/login")]
        c._request("GET", "http://portal/custcare_cu/whatever")
        self.assertFalse(c._logged_in)

    def test_unreachable_portal_raises_bscs_error(self):
        c = make_client([requests.ConnectionError("refused")] * 3)
        with unittest.mock.patch.object(bscs.time, "sleep", lambda s: None):
            with self.assertRaises(BSCSError):
                c._request("GET", "http://portal/custcare_cu/login")

    def test_http_error_is_not_retried(self):
        c = make_client([FakeResponse("err", status_code=500)])
        c._request("GET", "http://portal/custcare_cu/login")
        self.assertEqual(len(c.session.calls), 1, "a 500 must not be retried")

    def test_timeout_is_retried_then_raises(self):
        c = make_client([requests.Timeout("slow")] * 3)
        with unittest.mock.patch.object(bscs.time, "sleep", lambda s: None):
            with self.assertRaises(BSCSError):
                c._request("GET", "http://portal/custcare_cu/login")
        self.assertEqual(len(c.session.calls), 3)


class TestIvpFields(unittest.TestCase):
    def test_collects_only_ivp_fields(self):
        fields = BSCSClient._ivp_fields(SEARCH_FORM)
        self.assertIn("IVP_CS_ID_PUB", fields)
        self.assertIn("IVP_RESOURCE", fields)
        self.assertNotIn("FW_SubmittedFormPath", fields)
        self.assertNotIn("CS_ID_PUB", fields, "non-IVP form inputs must be excluded")
        self.assertNotIn("Search_Button", fields)

    def test_values_captured(self):
        fields = BSCSClient._ivp_fields(SEARCH_FORM)
        self.assertEqual(fields["IVP_RESOURCE"], "PARTY")
        self.assertEqual(fields["IVP_SRCH_COUNT"], "10")

    def test_malformed_html_is_survivable(self):
        self.assertEqual(BSCSClient._ivp_fields("<<<>>> not html"), {})
        self.assertEqual(BSCSClient._ivp_fields(""), {})


class TestSearchCustomers(unittest.TestCase):
    def _search_client(self, results=RESULTS):
        return make_client(login_script(results=results))

    def test_requires_at_least_one_criterion(self):
        c = make_client([])
        res = c.search_customers()
        self.assertFalse(res["success"])
        self.assertIn("criterion", res["message"].lower())
        self.assertEqual(c.session.calls, [], "must not hit the portal at all")

    def test_blank_criterion_is_not_accepted(self):
        """Whitespace is truthy, so it must be stripped before the check."""
        c = make_client([])
        res = c.search_customers(customer_id="   ")
        self.assertFalse(res["success"])
        self.assertEqual(c.session.calls, [], "a blank query must not reach the portal")

    def test_successful_search_parses_rows(self):
        c = self._search_client()
        res = c.search_customers(customer_id="100001")
        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["customers"][0]["Customer code"], "100001")
        self.assertEqual(res["customers"][1]["Status"], "Suspended")

    def _step_posts(self, client):
        """POSTs to a wizard step, excluding the login form POST."""
        return [c for c in client.session.calls if c[0] == "POST" and "SuStepName" in c[1]]

    def test_search_posts_to_the_right_step(self):
        c = self._search_client()
        c.search_customers(customer_id="100001")
        posts = self._step_posts(c)
        self.assertEqual(len(posts), 1, "expected exactly one wizard-step POST")
        self.assertIn(f"SuStepName={bscs.CUSTOMER_SEARCH_STEP}", posts[0][1])
        self.assertIn("SuToken=", posts[0][1])
        self.assertIn("RequestTimeStamp=", posts[0][1])
        self.assertEqual(posts[0][2]["data"]["CS_ID_PUB"], "100001")

    def test_search_echoes_ivp_fields(self):
        c = self._search_client()
        c.search_customers(customer_id="100001")
        data = self._step_posts(c)[0][2]["data"]
        self.assertEqual(data["IVP_RESOURCE"], "PARTY")
        self.assertIn("IVP_SRCH_COUNT", data)

    def test_search_that_ran_with_no_matches_is_a_confirmed_absence(self):
        """
        The portal ran the search and found nobody. That is a real answer and
        must be reported as success with a confirmed absence.
        """
        c = self._search_client(RESULTS_BUT_NO_MATCHES)
        res = c.search_customers(full_name="wons.bt")
        self.assertTrue(res["success"])
        self.assertTrue(res["search_executed"])
        self.assertEqual(res["count"], 0)
        self.assertIn("matched no customers", res["message"])

    def test_search_that_did_not_run_is_not_reported_as_no_match(self):
        """
        A form echo looks identical to an empty result if you only count rows.
        Reporting it as 'no such customer' would be a confident wrong answer.
        """
        c = self._search_client(FORM_ECHO)
        res = c.search_customers(full_name="wons.bt")
        self.assertFalse(res["success"])
        self.assertFalse(res["search_executed"])
        self.assertIn("did not execute", res["message"].lower())
        self.assertIn("must not be read as", res["message"].lower())

    def test_empty_result_is_reported_honestly(self):
        c = self._search_client(EMPTY_RESULTS)
        res = c.search_customers(customer_id="999999")
        # No results marker present, so this is an indeterminate response.
        self.assertFalse(res["search_executed"])

    def test_result_cap_is_applied(self):
        c = BSCSClient(base_url="http://portal/custcare_cu", username="u", password=DISTINCTIVE_PW)
        c.max_results = 2
        rows = "".join(f"<tr><td>{i}</td><td>Name {i}</td></tr>" for i in range(50))
        html = f"<table><tbody><tr><th>A</th><th>B</th></tr>{rows}</tbody></table>"
        parsed = c._parse_customer_rows(html)
        self.assertLessEqual(len(parsed), 2)

    def test_header_row_is_not_counted_as_a_result(self):
        """A <th>-only row is a header; counting it inflates results by one."""
        c = make_client(login_script(results=RESULTS))
        res = c.search_customers(customer_id="100001")
        self.assertEqual(res["count"], 2)
        # The header text is a legitimate *key*; it must never appear as a
        # *value*, which is what a header row parsed as data would produce.
        for customer in res["customers"]:
            self.assertNotIn("Customer code", customer.values())
        self.assertEqual(res["customers"][0]["Customer code"], "100001")

    def test_thead_headers_are_used(self):
        """
        Live data proved this matters: with headers in <thead> (outside
        <tbody>) the parser emitted col_0..col_5 instead of the real column
        names, which makes the result much less useful to a caller.
        """
        c = BSCSClient(base_url="http://p/c", username="u", password=DISTINCTIVE_PW)
        rows = c._parse_customer_rows(RESULTS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Customer code"], "100001")
        self.assertEqual(rows[0]["Status"], "Active")
        self.assertEqual(rows[1]["Status"], "Suspended")
        self.assertNotIn("col_0", rows[0], "headers must come from <thead>")

    def test_table_without_header_uses_positional_keys(self):
        """No header row at all: fall back to positional keys. Parser-level, so
        it does not depend on the results-presence marker."""
        c = BSCSClient(base_url="http://p/c", username="u", password=DISTINCTIVE_PW)
        rows = c._parse_customer_rows(
            "<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>")
        self.assertEqual(len(rows), 1)
        self.assertIn("col_0", rows[0])

    def test_malformed_results_do_not_crash(self):
        """Garbage in the response must not raise; it is reported as a
        non-executed search rather than a confirmed absence."""
        c = make_client(login_script(results="<<<"))
        res = c.search_customers(customer_id="1")
        self.assertFalse(res["search_executed"])
        self.assertEqual(res["count"], 0)


class TestSearchContracts(unittest.TestCase):
    def test_requires_customer_id(self):
        c = make_client([])
        res = c.search_contracts("")
        self.assertFalse(res["success"])
        self.assertIn("customer_id", res["message"])

    def test_contract_search_posts_customer_id(self):
        form = SEARCH_FORM.replace("CustomerSearch_Step", "Search_Step")
        c = make_client(login_script(form_html=form))
        res = c.search_contracts("100001")
        self.assertTrue(res["success"])
        posts = [x for x in c.session.calls if x[0] == "POST" and "SuStepName" in x[1]]
        self.assertEqual(posts[0][2]["data"]["CS_ID_PUB"], "100001")
        self.assertIn(f"SuStepName={bscs.CONTRACT_SEARCH_STEP}", posts[0][1])


class TestMasking(unittest.TestCase):
    def test_mask_hides_middle(self):
        masked = bscs._mask("abcdefghij")
        self.assertNotIn("cdefgh", masked)
        self.assertTrue(masked.startswith("ab"))

    def test_mask_handles_empty_and_short(self):
        self.assertEqual(bscs._mask(""), "<empty>")
        self.assertEqual(bscs._mask(None), "<empty>")
        self.assertEqual(bscs._mask("ab"), "***")

    def test_text_strips_tags(self):
        self.assertEqual(bscs._text("<b>Hello</b>   <i>World</i>"), "Hello World")


if __name__ == "__main__":
    unittest.main(verbosity=2)
