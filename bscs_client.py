"""
Read-only client for the Ericsson BSCS / CBiO CX Customer Center portal.

SCOPE: READ-ONLY BY DESIGN.

Bhutan Telecom's account on this portal (the shared BTSS-* user) has no rights
to change contracts, VAS packages or billing state, so this module exposes no
method that writes. There is deliberately no activate_service(),
deactivate_service() or contract-modifying call here. A method that cannot work
with the permissions we hold should not exist in the client, because its
presence would invite somebody to try it.

What it is for: confirming that a customer exists before provisioning, and
reading their contract and billing state as reference.

WHY THIS IS NOT A REST API
--------------------------
The portal is a server-rendered "Solution Unit" (SU) workflow, not an API:

  * Authentication is a Spring Security form POST to ``Login.au`` using
    ``j_username`` / ``j_password``.
  * A successful login redirects to ``SolutionUnitServlet`` with a ``SuToken``.
  * ``SuToken`` is ROTATED every time a Solution Unit is started. Caching it
    and reusing it on the next call fails, so every entry point re-reads the
    token from the response instead of storing one.
  * Search is a form POST to ``SolutionUnitServlet`` with a ``SuStepName``,
    the criteria fields, and a set of ``IVP_*`` echo-back fields that the
    portal expects to be echoed unchanged.
  * Modules are reached through ``<Module>SU.su?alias=<Alias>`` URLs.

The field names and step names below were read from the live portal, not
guessed. If the portal is upgraded, they are the first thing that will break,
so they are named as constants to make that obvious.
"""
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from config import settings

logger = logging.getLogger(__name__)

# --- Observed portal constants -------------------------------------------
# Credentials are posted here; the form declares this action itself.
LOGIN_PATH = "Login.au"
# Every module is started through this servlet with a SuName.
SU_SERVLET = "SolutionUnitServlet"
# Customer search module and the step its form posts back to.
CUSTOMER_SEARCH_SU = "PartySearchSU.su"
CUSTOMER_SEARCH_ALIAS = "CustomerSearchSU"
CUSTOMER_SEARCH_STEP = "CustomerSearch_Step"
# Contract search module and its step.
CONTRACT_SEARCH_SU = "SearchForPartyContractSU.su"
CONTRACT_SEARCH_ALIAS = "SearchForContractSU"
CONTRACT_SEARCH_STEP = "Search_Step"

# Criteria fields the customer search form exposes. Only the ones we let a
# caller set; anything else in the form is left at its default.
CUSTOMER_SEARCH_FIELDS = {
    "customer_id": "CS_ID_PUB",
    "customer_code": "CS_CODE",
    "full_name": "ADR_NAME",
    "first_name": "ADR_FNAME",
    "last_name": "ADR_LNAME",
    "status": "CS_STATUS",
    "document_id": "DOCUMENT_ID_PUB",
}

CONTRACT_SEARCH_FIELDS = {
    "customer_id": "CS_ID_PUB",
    "contract_id": "CO_ID_PUB",
    "status": "CO_STATUS",
    "service_code": "SCCODE",
    "plcode": "PLCODE",
    "rpc_code": "RPCODE",
}

# The portal echoes these back on submit. They are collected from the form and
# resent verbatim; dropping them makes the portal reject the search.
IVP_PREFIX = "IVP_"

# SuToken is currently 32 hex characters, but the character class is widened to
# include "-" and "_" so a future token format cannot silently truncate to a
# prefix and produce confusing downstream failures.
_TOKEN_RE = re.compile(r"SuToken=([A-Za-z0-9_\-]+)")
_TAG_RE = re.compile(r"<[^>]+>")


class BSCSError(RuntimeError):
    """Raised when the portal cannot be reached or returns something unusable."""


def _text(html: str) -> str:
    """Strip tags and collapse whitespace, for log-safe snippets."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()


def _mask(value: Optional[str], keep: int = 2) -> str:
    """Mask a value for logging. Never log a credential or a full identifier."""
    if not value:
        return "<empty>"
    v = str(value)
    return f"{v[:keep]}***{v[-1]}" if len(v) > keep + 1 else "***"


class BSCSClient:
    """Read-only BSCS CX portal client.

    One instance holds one authenticated session. The portal expires idle
    sessions, so every public method calls _ensure_session() first and
    re-authenticates transparently when needed.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[int] = None,
        verify_ssl: Optional[bool] = None,
    ):
        # "is not None" rather than "or": an explicitly empty value must be
        # honoured, not silently replaced from settings. With "or", passing
        # username="" to mean "unconfigured" would quietly pick up the real
        # credential from .env instead.
        self.base_url = (base_url if base_url is not None
                         else getattr(settings, "BSCS_BASE_URL", "")).rstrip("/")
        self.username = username if username is not None else getattr(settings, "BSCS_USERNAME", "")
        self.password = password if password is not None else getattr(settings, "BSCS_PASSWORD", "")
        self.timeout = timeout or getattr(settings, "BSCS_TIMEOUT", 45)
        self.verify = getattr(settings, "BSCS_VERIFY_SSL", True) if verify_ssl is None else verify_ssl
        self.max_results = getattr(settings, "BSCS_MAX_RESULTS", 25)

        if not self.base_url:
            raise BSCSError("BSCS_BASE_URL is not configured.")

        self.session = requests.Session()
        self.session.verify = self.verify
        # The portal is plain HTTP; suppress the per-request warning so it does
        # not drown real errors.
        requests.packages.urllib3.disable_warnings()
        self._su_token: Optional[str] = None
        self._logged_in = False

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """One HTTP call with bounded retries on connection-level faults.

        Retries timeouts and resets, which the CX portal does produce under
        load. Never retries an HTTP error response: a 4xx/5xx is the portal
        having made a decision, and repeating it will not change that.
        """
        attempts = 3
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                # Re-arm the session flag if we were redirected to the login page.
                if "login" in resp.url.lower() and "/login" in resp.url.lower():
                    self._logged_in = False
                return resp
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                logger.warning(
                    "BSCS %s %s failed (attempt %d/%d): %s",
                    method, url.rsplit("/", 1)[-1][:60], attempt, attempts, type(e).__name__,
                )
                if attempt < attempts:
                    time.sleep(2 * attempt)
        raise BSCSError(f"BSCS portal unreachable: {type(last_exc).__name__}")

    @staticmethod
    def _extract_token(*texts: str) -> Optional[str]:
        for text in texts:
            if not text:
                continue
            m = _TOKEN_RE.search(text)
            if m:
                return m.group(1)
        return None

    def login(self) -> Tuple[bool, str]:
        """Authenticate and capture the initial SuToken.

        Returns (ok, message). The message never contains the password.
        """
        if not self.username or not self.password:
            return False, "BSCS_USERNAME / BSCS_PASSWORD are not configured."

        try:
            self._request("GET", f"{self.base_url}/login")
            resp = self._request(
                "POST",
                f"{self.base_url}/{LOGIN_PATH}",
                data={
                    "j_username": self.username,
                    "j_password": self.password,
                    "submit": "Login",
                },
                allow_redirects=True,
            )
        except BSCSError as e:
            return False, str(e)

        if resp.status_code != 200:
            return False, f"Login returned HTTP {resp.status_code}."

        # A failed login re-renders the login form; a good one lands on the
        # Solution Unit servlet carrying a SuToken.
        token = self._extract_token(resp.url, resp.text)
        if not token:
            body = _text(resp.text)[:200]
            logger.warning("BSCS login produced no SuToken. body=%s", body)
            return False, f"Login did not yield a session token. Response began: {body!r}"

        self._su_token = token
        self._logged_in = True
        logger.info("BSCS login OK for user %s (SuToken %s)", _mask(self.username), _mask(token))
        return True, f"Authenticated to BSCS CX as {_mask(self.username)}."

    def _ensure_session(self) -> bool:
        if self._logged_in and self._su_token:
            return True
        ok, msg = self.login()
        if not ok:
            raise BSCSError(msg)
        return True

    def _start_su(self, su_file: str, alias: str) -> Tuple[str, str]:
        """
        Start a Solution Unit and return (su_token, html).

        The portal rotates SuToken on every start, so the token is taken from
        this response and replaces any previously held one. Reusing a stale
        token is the failure mode that makes hand-rolled clients brittle.
        """
        self._ensure_session()
        url = (
            f"{self.base_url}/{su_file}"
            f"?alias={alias}&SuStepName=UserHome_Step&SuToken={self._su_token}"
        )
        resp = self._request("GET", url, allow_redirects=True)
        if resp.status_code != 200:
            raise BSCSError(f"Starting {alias} returned HTTP {resp.status_code}.")

        token = self._extract_token(resp.url, resp.text)
        if token:
            self._su_token = token
        return self._su_token or "", resp.text

    @staticmethod
    def _step_url(su_token: str, step_name: str) -> str:
        """Build the SolutionUnitServlet URL a wizard step posts back to."""
        return (
            f"{SU_SERVLET}?SuStepName={step_name}"
            f"&SuToken={su_token}&RequestTimeStamp={int(datetime.now().timestamp() * 1000)}"
        )

    @staticmethod
    def _collect_form_fields(html: str) -> Dict[str, str]:
        """
        Collect every control a browser would submit from the search form.

        Includes <select> elements, which a browser always submits and which
        earlier attempts omitted, and skips submit/reset buttons (only the
        clicked control is sent) and unchecked checkboxes.

        The IVP_* fields among these are view/session state the portal expects
        echoed back; they are gathered from the form rather than hardcoded,
        because missing them makes a search quietly return nothing.
        """
        fields: Dict[str, str] = {}
        for tag in re.findall(r"<input[^>]*>", html, re.I):
            name = re.search(r'name\s*=\s*["\']?([^"\'\s>]+)', tag, re.I)
            if not name:
                continue
            value = re.search(r'value\s*=\s*["\']([^"\']*)["\']', tag)
            typ = re.search(r'type\s*=\s*["\']?([^"\'\s>]+)', tag, re.I)
            kind = (typ.group(1) if typ else "text").lower()
            if kind in ("submit", "button", "reset", "image"):
                continue
            if kind in ("checkbox", "radio") and "checked" not in tag.lower():
                continue
            fields[name.group(1)] = value.group(1) if value else ""

        for tag in re.findall(r"<select[^>]*>.*?</select>", html, re.I | re.S):
            name = re.search(r'name\s*=\s*["\']?([^"\'\s>]+)', tag, re.I)
            if not name:
                continue
            options = re.findall(r"<option([^>]*)>", tag, re.I)
            chosen = ""
            for attrs in options:
                if "selected" in attrs.lower():
                    m = re.search(r'value\s*=\s*["\']([^"\']*)["\']', attrs, re.I)
                    chosen = m.group(1) if m else ""
                    break
            else:
                if options:
                    m = re.search(r'value\s*=\s*["\']([^"\']*)["\']', options[0], re.I)
                    chosen = m.group(1) if m else ""
            fields[name.group(1)] = chosen
        return fields

    @staticmethod
    def _ivp_fields(html: str) -> Dict[str, str]:
        """Just the IVP_* echo-back fields, for callers that want only those."""
        return {k: v for k, v in BSCSClient._collect_form_fields(html).items()
                if k.startswith(IVP_PREFIX)}

    @staticmethod
    def _results_present(html: str) -> bool:
        """
        Did the search actually run?

        Distinguishes "the portal executed the search and found nothing" from
        "the portal ignored the request and re-rendered the form". These look
        identical if you only count rows: a search that fails to execute and a
        search with no matches both yield zero data rows.

        After a successful search the portal adds a "Search results" heading and
        a "<shown> / <total>" counter. Neither is present when the form is
        simply echoed back.
        """
        text = _text(html)
        if re.search(r"search\s*results", text, re.I):
            return True
        # The counter renders as e.g. "0 / 0" on an empty result set.
        if re.search(r"\b\d+\s*/\s*\d+\b", text):
            return True
        return False

    # -- read-only operations --------------------------------------------
    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and report whether the portal is usable."""
        try:
            ok, msg = self.login()
        except BSCSError as e:
            return {"success": False, "message": str(e), "base_url": self.base_url}
        return {
            "success": ok,
            "message": msg,
            "base_url": self.base_url,
            "read_only": True,
        }

    def search_customers(self, **criteria: Any) -> Dict[str, Any]:
        """
        Search the customer index.

        Accepts any of CUSTOMER_SEARCH_FIELDS as keyword arguments, e.g.
        search_customers(customer_id="12345") or search_customers(full_name="...").
        At least one criterion is required: an empty search would pull the
        entire customer index.

        Returns {"success", "count", "customers", "message"}. Raw rows are not
        returned wholesale -- see _parse_customer_rows for what is extracted.
        """
        # Strip before testing: a whitespace-only value is truthy, and letting
        # one through would send a broad query to the billing system.
        criteria = {
            k: (v.strip() if isinstance(v, str) else v)
            for k, v in criteria.items()
        }
        if not any(criteria.get(k) for k in CUSTOMER_SEARCH_FIELDS):
            return {
                "success": False,
                "count": 0,
                "customers": [],
                "message": "Provide at least one search criterion: "
                           + ", ".join(sorted(CUSTOMER_SEARCH_FIELDS)),
            }

        try:
            _, html = self._start_su(CUSTOMER_SEARCH_SU, CUSTOMER_SEARCH_ALIAS)
        except BSCSError as e:
            return {"success": False, "count": 0, "customers": [], "message": str(e)}

        data = self._collect_form_fields(html)
        for key, value in criteria.items():
            field = CUSTOMER_SEARCH_FIELDS.get(key)
            if field and value:
                data[field] = str(value)
        data["FW_SubmittedFormPath"] = "form"
        data["Search_Button"] = "Search"
        data["SRCH_COUNT"] = str(min(self.max_results, 100))

        try:
            resp = self._request(
                "POST",
                urljoin(f"{self.base_url}/", self._step_url(self._su_token or "", CUSTOMER_SEARCH_STEP)),
                data=data,
                allow_redirects=True,
            )
        except BSCSError as e:
            return {"success": False, "count": 0, "customers": [], "message": str(e)}

        if resp.status_code != 200:
            return {"success": False, "count": 0, "customers": [],
                    "message": f"Search returned HTTP {resp.status_code}."}

        # The step may hand back a fresh token for the next screen.
        token = self._extract_token(resp.url, resp.text)
        if token:
            self._su_token = token

        customers = self._parse_customer_rows(resp.text)
        executed = self._results_present(resp.text)
        if not executed:
            return {
                "success": False,
                "search_executed": False,
                "count": 0,
                "customers": [],
                "message": "The portal did not execute the search: it re-rendered the "
                           "search form. The form fields this client sends are no longer "
                           "what the portal expects, so the result must not be read as "
                           "'no such customer'.",
            }
        if not customers:
            return {
                "success": True,
                "search_executed": True,
                "count": 0,
                "customers": [],
                "message": "The search ran and matched no customers. This is a confirmed "
                           "absence, not a failed query.",
            }
        return {
            "success": True,
            "search_executed": True,
            "count": len(customers),
            "customers": customers,
            "message": f"Found {len(customers)} customer(s).",
        }

    def _parse_customer_rows(self, html: str) -> List[Dict[str, Any]]:
        """
        Extract customer rows from a search result page.

        Deliberately conservative: only table rows that look like results are
        considered, and a hard cap is applied. Header cells become keys so the
        shape survives portal changes, and a row that yields nothing usable is
        dropped rather than returned as an empty dict.
        """
        rows: List[Dict[str, Any]] = []
        for table_match in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.I | re.S):
            table = table_match.group(1)
            body = re.search(r"<tbody[^>]*>(.*?)</tbody>", table, re.I | re.S)
            if not body:
                continue

            # Column names usually live in <thead>, which sits OUTSIDE <tbody>.
            # Reading headers only from inside the body loses them and silently
            # degrades every record to col_0, col_1, ... which makes the result
            # far less useful to a caller.
            headers: List[str] = []
            thead = re.search(r"<thead[^>]*>(.*?)</thead>", table, re.I | re.S)
            if thead:
                headers = [_text(c) for c in
                           re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", thead.group(1), re.I | re.S)]

            for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.I | re.S):
                row = tr.group(1)
                # Accept either closing tag. Portal-generated tables are not
                # always well formed -- a <td> closed by </th> is common enough
                # that a strict </td> match silently drops the whole column.
                cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)]
                if not cells or not any(cells):
                    continue

                # A row made only of <th> is a header, not a result. Treating
                # it as data inflates the count by one and yields a bogus record.
                if not re.search(r"<td\b", row, re.I):
                    if not headers:
                        headers = cells
                    continue

                record = {}
                for idx, cell in enumerate(cells):
                    key = headers[idx] if idx < len(headers) else f"col_{idx}"
                    if key and cell:
                        record[key] = cell
                if record:
                    rows.append(record)
                if len(rows) >= self.max_results:
                    return rows
        return rows

    def search_contracts(self, customer_id: str, **criteria: Any) -> Dict[str, Any]:
        """
        Search contracts for a customer.

        Read-only. Returns the same shape as search_customers().
        """
        if not customer_id:
            return {"success": False, "count": 0, "contracts": [],
                    "message": "customer_id is required."}

        try:
            _, html = self._start_su(CONTRACT_SEARCH_SU, CONTRACT_SEARCH_ALIAS)
        except BSCSError as e:
            return {"success": False, "count": 0, "contracts": [], "message": str(e)}

        data = self._collect_form_fields(html)
        data["CS_ID_PUB"] = str(customer_id)
        for key, value in criteria.items():
            field = CONTRACT_SEARCH_FIELDS.get(key)
            if field and value:
                data[field] = str(value)
        data["FW_SubmittedFormPath"] = "form"
        data["SuSubmitButton"] = "Search"

        try:
            resp = self._request(
                "POST",
                urljoin(f"{self.base_url}/", self._step_url(self._su_token or "", CONTRACT_SEARCH_STEP)),
                data=data,
                allow_redirects=True,
            )
        except BSCSError as e:
            return {"success": False, "count": 0, "contracts": [], "message": str(e)}

        if resp.status_code != 200:
            return {"success": False, "count": 0, "contracts": [],
                    "message": f"Contract search returned HTTP {resp.status_code}."}

        token = self._extract_token(resp.url, resp.text)
        if token:
            self._su_token = token

        contracts = self._parse_customer_rows(resp.text)
        executed = self._results_present(resp.text)
        if not executed:
            return {
                "success": False,
                "search_executed": False,
                "count": 0,
                "contracts": [],
                "message": "The portal did not execute the contract search: it "
                           "re-rendered the search form. Do not read this as "
                           "'no contracts'.",
            }
        return {
            "success": True,
            "search_executed": True,
            "count": len(contracts),
            "contracts": contracts,
            "message": (f"Found {len(contracts)} contract(s)." if contracts else
                        "The search ran and matched no contracts."),
        }
