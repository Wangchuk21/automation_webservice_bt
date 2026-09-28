import logging
import re
import time
from datetime import date
from typing import Dict, Any, List, Optional, Tuple
import requests
from config import settings
from tls_config import resolve_verify

logger = logging.getLogger(__name__)

# nic.bt.bt is slow: a single domain write has been observed taking around 40
# seconds end to end. The previous flat 15s timeout therefore failed
# intermittently, which in a surrender produced partial results -- the hosting
# account removed but the domain registration left behind. Reads and writes get
# a generous timeout, and writes are retried once on a transient network error.
REGISTRY_TIMEOUT = 60
REGISTRY_WRITE_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3


def _request_with_retry(session, method: str, url: str, attempts: int = REGISTRY_WRITE_ATTEMPTS, **kwargs):
    """
    Issue a registry request on the given session, retrying transient network
    failures.

    The session must be passed in and used for the call: the registry relies on
    the session cookie and the CSRF token fetched from the login page, and the
    session also carries the TLS verification setting. Issuing the request
    through the bare `requests` module instead silently drops both and the
    portal rejects the login.

    Only connection-level problems (timeouts, resets) are retried. An HTTP
    error response is returned as-is, because a 4xx/5xx from the portal is a
    decision the server has already made and repeating the call would not
    change it.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return session.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            logger.warning(
                "nic.bt.bt %s %s failed (attempt %d/%d): %s",
                method, url, attempt, attempts, e,
            )
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_exc


# The nic.bt.bt domain form's 23 required fields, in the portal's own order.
#
# "source" says where each value comes from:
#   customer  -- collected from the customer on the dashboard
#   derived   -- copied from another customer field
#   default   -- Bhutan Telecom's own details, from config
#   computed  -- built from the domain name
#
# "group" says which section of the registry's form the field belongs to, so the
# dashboard can present the four sections the way the portal does rather than as
# one flat list of 23 inputs.
#
# This is the single source of truth. The client builds its payload from it and
# the dashboard renders its form from it, so the two cannot drift apart -- which
# is how ten hardcoded values went unnoticed, invisible from both sides.
REGISTRY_FIELDS = (
    {"name": "domain", "label": "Domain name", "source": "computed", "required": True},
    {"name": "ext", "label": "Extension", "source": "computed", "required": True,
     "options": [".bt", ".com.bt", ".org.bt", ".gov.bt", ".edu.bt"]},
    {"name": "registrar", "label": "Registrar", "source": "default", "required": True},
    {"name": "reg_renewal", "label": "Renewal date", "source": "customer", "required": True},
    {"name": "customername", "label": "Customer name", "source": "customer", "required": True},
    {"name": "address", "label": "Address", "source": "customer", "required": True},
    {"name": "postalcode", "label": "Postal code", "source": "customer", "required": True},
    {"name": "phone", "label": "Telephone", "source": "customer", "required": True},
    {"name": "email", "label": "Email", "source": "customer", "required": True},
    {"name": "country", "label": "Country", "source": "customer", "required": True},
    # Technical contact is the DOMAIN OWNER, per the international convention
    # seen on real registry records, so these follow the customer's details.
    {"name": "tech_name", "label": "Technical contact name", "source": "derived",
     "derived_from": "customername", "required": True},
    {"name": "tech_address", "label": "Technical contact address", "source": "derived",
     "derived_from": "address", "required": True},
    {"name": "tech_postalcode", "label": "Technical postal code", "source": "derived",
     "derived_from": "postalcode", "required": True},
    {"name": "tech_phone", "label": "Technical telephone", "source": "derived",
     "derived_from": "phone", "required": True},
    {"name": "tech_fax", "label": "Technical fax", "source": "default", "required": True},
    {"name": "tech_country", "label": "Technical country", "source": "derived",
     "derived_from": "country", "required": True},
    {"name": "tech_email", "label": "Technical email", "source": "derived",
     "derived_from": "email", "required": True},
    # Billing contact is the REGISTRAR or its agent -- Bhutan Telecom here.
    {"name": "billing_name", "label": "Billing name", "source": "derived",
     "derived_from": "customername", "required": True},
    {"name": "billing_address", "label": "Billing address", "source": "derived",
     "derived_from": "address", "required": True},
    {"name": "billing_contact", "label": "Billing contact", "source": "derived",
     "derived_from": "phone", "required": True},
    {"name": "billing_fax", "label": "Billing fax", "source": "default", "required": True},
    {"name": "billing_country", "label": "Billing country", "source": "derived",
     "derived_from": "country", "required": True},
    {"name": "billing_email", "label": "Billing email", "source": "derived",
     "derived_from": "email", "required": True},
)


def registry_field_spec() -> Dict[str, Any]:
    """
    The registry's required fields with the value each takes by default.

    The dashboard renders its form from this, so the operator sees every field
    the registry demands -- including the ten Bhutan Telecom technical details
    that were previously hardcoded and invisible -- and can override any of them
    before submitting.
    """
    s = settings
    # The registrar is the only value Bhutan Telecom supplies; every contact
    # field on the record is the customer's own. The two fax fields have no
    # customer equivalent collected, so the registry's placeholder is used.
    defaults = {
        "registrar": s.NIC_REGISTRAR,
        "tech_fax": s.NIC_PLACEHOLDER,
        "billing_fax": s.NIC_PLACEHOLDER,
    }
    counts: Dict[str, int] = {}
    for f in REGISTRY_FIELDS:
        counts[f["source"]] = counts.get(f["source"], 0) + 1
    return {
        "fields": [dict(f, default=defaults.get(f["name"], "")) for f in REGISTRY_FIELDS],
        "required_count": sum(1 for f in REGISTRY_FIELDS if f.get("required")),
        "source_counts": counts,
        # The portal presents the form in four sections. Presenting them the
        # same way keeps 23 inputs legible instead of one long wall.
        "groups": [list(g) for g in REGISTRY_GROUPS],
    }


def _strip_tags(html: str) -> str:
    """Strip tags and collapse whitespace, for reading option labels."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _reject_unsupported_ext(ext: str, offered: List[str]) -> None:
    """
    Fail with a clear message when the derived extension is not one the
    registry offers.

    Without this the submission reaches the portal and comes back as an opaque
    validation error, which is hard to trace back to a mistyped domain. Skipped
    when the dropdown could not be read, so a change in the form's markup
    degrades to the portal's own error rather than a false rejection.
    """
    if not offered or ext in offered:
        return
    raise ValueError(f"Extension '{ext}' is not offered by nic.bt.bt. "
                     f"Available: {', '.join(offered)}")


def available_extensions(html: str) -> List[str]:
    """
    Read the extension options the registry's own form offers.

    That dropdown is the authority on what nic.bt.bt accepts, so it is read
    from the page being submitted rather than hardcoded. A hardcoded list
    drifts silently: an extension the portal adds would be rejected with an
    opaque error, and one it removes would still be sent.
    """
    opts: List[str] = []
    for tag in re.findall(r"<select[^>]*>.*?</select>", html or "", re.I | re.S):
        name = re.search(r'name\s*=\s*["\']?([^"\'\s>]+)', tag, re.I)
        if not name or name.group(1) != "ext":
            continue
        for attrs, label in re.findall(r"<option([^>]*)>(.*?)</option>", tag, re.I | re.S):
            text = _strip_tags(label)
            # The empty option is rendered as "Choose.." but carries the value
            # NULL_VALUE, so the label is what identifies it as a placeholder.
            if not text or text.lower().startswith(("choose", "select", "-")):
                continue
            value = re.search(r'value\s*=\s*["\']([^"\']*)["\']', attrs, re.I)
            val = (value.group(1) if value else text).strip()
            if not val or val.upper() in ("NULL_VALUE", "NULL", "NONE"):
                continue
            if val not in opts:
                opts.append(val)
    return opts


def split_domain_ext(full_domain: str) -> Tuple[str, str]:
    """
    Splits domain into base name and extension.
    Example:
        'karunabhutantravel.bt' -> ('karunabhutantravel', '.bt')
        'test.com.bt' -> ('test', '.com.bt')
    """
    clean = full_domain.strip().lower().rstrip('.')
    known_exts = [".com.bt", ".org.bt", ".net.bt", ".gov.bt", ".edu.bt", ".bt"]
    for ext in known_exts:
        if clean.endswith(ext):
            base = clean[:-len(ext)]
            return base, ext
    parts = clean.split(".", 1)
    if len(parts) > 1:
        return parts[0], "." + parts[1]
    return clean, ".bt"


# Values used when the operator leaves a field blank. Chosen to match what the
# client has always sent, so a form that is filled in as before produces a
# byte-identical payload to the previous hardcoded one.
NIC_FALLBACKS = {
    "address": "Thimphu, Bhutan",
    "postalcode": settings.NIC_PLACEHOLDER,
    "phone": "+975",
    "country": settings.NIC_DEFAULT_COUNTRY,
    "registrar": settings.NIC_REGISTRAR,
    "tech_fax": settings.NIC_PLACEHOLDER,
    "billing_fax": settings.NIC_PLACEHOLDER,
    "billing_country": settings.NIC_DEFAULT_COUNTRY,
}


def build_registry_payload(
    provided: Dict[str, Any],
    base_domain: str,
    ext: str,
    reg_date: str,
) -> Dict[str, str]:
    """
    Build the form body nic.bt.bt expects, from whatever the operator entered.

    Every one of the 23 fields the registry requires is resolved here, in one
    place, driven by REGISTRY_FIELDS -- the same list the dashboard renders its
    form from. Before this existed the body was written out field by field and
    duplicated across the create and update paths, so adding a field meant
    editing two literals and neither could be checked against the form.

    Resolution order, per field:
      1. the operator's own value, if they gave one
      2. the field's derived source (tech_* from the customer, billing_* from
         the customer), which is the registry's own convention
      3. a Bhutan Telecom fallback, where the registry requires a value the
         customer has no part in -- the registrar, and the two fax numbers

    Unknown keys are dropped rather than forwarded: the registry is a
    form-encoded endpoint, and passing a field it does not recognise risks it
    being stored somewhere unintended.
    """
    known = {f["name"]: f for f in REGISTRY_FIELDS}
    clean: Dict[str, str] = {}
    for name, value in (provided or {}).items():
        if name not in known or value is None:
            continue
        text = str(value).strip()
        if text:
            clean[name] = text

    def resolved(name: str) -> str:
        if clean.get(name):
            return clean[name]
        field = known.get(name) or {}
        source = field.get("source")
        if source == "derived":
            parent = field.get("derived_from") or ""
            return resolved(parent) if parent in known else ""
        if name == "domain":
            return base_domain
        if name == "ext":
            return ext
        if name == "reg_renewal":
            return reg_date
        return NIC_FALLBACKS.get(name, clean.get(name, ""))

    payload: Dict[str, str] = {}
    for name in known:
        payload[name] = resolved(name)
    return payload


def missing_registry_fields(payload: Dict[str, str]) -> List[str]:
    """The required fields the registry would reject the submission for."""
    return [f["name"] for f in REGISTRY_FIELDS
            if f.get("required") and not (payload.get(f["name"]) or "").strip()]


REGISTRY_GROUPS = (
    ("domain", "Domain"),
    ("contact", "Registrant / contact"),
    ("technical", "Technical contact"),
    ("billing", "Billing contact"),
)


def _group_of(name: str) -> str:
    """Which section of the registry's form a field belongs to."""
    if name in ("domain", "ext", "registrar", "reg_renewal"):
        return "domain"
    if name.startswith("tech_"):
        return "technical"
    if name.startswith("billing_"):
        return "billing"
    return "contact"


# Assign each field its section. Done programmatically rather than by hand in
# the literals: a newly added field gets a sensible section automatically
# instead of silently landing in the wrong one.
REGISTRY_FIELDS = tuple(
    dict(f, group=_group_of(f["name"])) for f in REGISTRY_FIELDS
)


class NICClient:
    """Client for managing domain registration and WHOIS on nic.bt.bt."""

    # Extension lists change rarely and reading them means authenticating
    # against the portal. Cached briefly so opening the dashboard does not log
    # in on every page load, while a portal-side change still surfaces.
    _ext_cache: Tuple[Optional[List[str]], float] = (None, 0.0)
    _EXT_CACHE_TTL = 900  # seconds

    @classmethod
    def list_extensions(cls, force_refresh: bool = False) -> List[str]:
        """
        The extensions the registry's own dropdown offers.

        This is the authority: nic.bt.bt decides what it accepts, so the
        dashboard's dropdown is built from this rather than a hardcoded list.
        Falls back to an empty list, which the caller renders as "unavailable",
        rather than guessing.
        """
        cached, when = cls._ext_cache
        if not force_refresh and cached and (time.time() - when) < cls._EXT_CACHE_TTL:
            return list(cached)
        try:
            client = cls()
            if not client.login()[0]:
                return list(cached or [])
            resp = _request_with_retry(client.session, "GET",
                                       f"{client.base_url}/domain/create",
                                       attempts=2, timeout=REGISTRY_TIMEOUT)

            opts = available_extensions(resp.text)
            if opts:
                cls._ext_cache = (opts, time.time())
                return list(opts)
        except Exception as e:
            logger.warning("Could not read the nic.bt.bt extension list: %s", e)
        return list(cached or [])

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None
    ):
        self.base_url = (base_url or getattr(settings, "NIC_URL", "https://nic.bt.bt")).rstrip("/")
        self.username = username or getattr(settings, "NIC_USER", "admin@bt.bt")
        self.password = password or getattr(settings, "NIC_PASSWORD", "")
        self.session = requests.Session()
        # nic.bt.bt presents a valid Let's Encrypt certificate, so verification
        # is enabled. This session carries the registry admin credentials, which
        # is exactly the traffic that must not be interceptable.
        self.session.verify = resolve_verify()
        self._logged_in = False

    def login(self) -> Tuple[bool, str]:
        """Authenticate with nic.bt.bt admin portal."""
        login_url = f"{self.base_url}/login"
        try:
            r = self.session.get(login_url, timeout=REGISTRY_TIMEOUT)
            if r.status_code != 200:
                return False, f"Failed to reach login page: HTTP {r.status_code}"

            token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r.text)
            if not token_match:
                return False, "CSRF token not found on login page."

            csrf_token = token_match.group(1)
            payload = {
                "_token": csrf_token,
                "email": self.username,
                "password": self.password
            }

            resp = _request_with_retry(
            self.session, "POST", login_url, data=payload,
            timeout=REGISTRY_TIMEOUT, allow_redirects=True,
        )
            if "/login" not in resp.url and (resp.status_code == 200 or "domain" in resp.url):
                self._logged_in = True
                return True, "Successfully logged into nic.bt.bt admin portal."
            else:
                return False, "Authentication failed on nic.bt.bt (invalid credentials or redirected back to login)."
        except Exception as e:
            logger.error(f"Error logging into nic.bt.bt: {e}")
            return False, f"Connection error: {str(e)}"

    def _ensure_logged_in(self) -> Tuple[bool, str]:
        if not self._logged_in:
            return self.login()
        return True, "Already logged in"

    def find_domain_id(self, domain_name: str, ext: str) -> Optional[str]:
        """Search the admin domain list for an existing domain to get its ID."""
        ok, _ = self._ensure_logged_in()
        if not ok:
            return None

        try:
            r = self.session.get(f"{self.base_url}/domain", timeout=REGISTRY_TIMEOUT)
            if r.status_code != 200:
                return None

            for row_match in re.finditer(r"<tr>(.*?)</tr>", r.text, re.DOTALL):
                row_html = row_match.group(1)
                if domain_name.lower() in row_html.lower() and ext.lower() in row_html.lower():
                    edit_match = re.search(r"/domain/(\d+)/edit", row_html)
                    if edit_match:
                        return edit_match.group(1)
            return None
        except Exception as e:
            logger.error(f"Error checking domain list: {e}")
            return None

    def register_or_update_domain(
        self,
        domain: str,
        customer_name: str,
        email: str,
        phone: str = "+975",
        address: str = "Thimphu, Bhutan",
        postalcode: str = "-",
        country: str = "BT",
        reg_date: Optional[str] = None,
        ext: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Creates or updates a domain entry on nic.bt.bt for WHOIS lookup.

        `ext` is the registry extension (.bt, .com.bt, ...). It is taken from
        the operator when given -- the dashboard offers the registry's own
        dropdown, since the portal is the authority on what it accepts -- and
        otherwise derived from the domain name. Either way it is validated
        against the live dropdown before submitting.

        `fields` is the operator's full set of registry values, keyed by the
        names the registry uses. It is the form rendered from REGISTRY_FIELDS,
        so the operator can set any of the 23 fields rather than only the
        handful that used to have named parameters. Blank entries fall back to
        the derivation the client has always applied, so filling the form in as
        before produces the same submission.
        """
        ok, msg = self._ensure_logged_in()
        if not ok:
            return {"success": False, "message": f"NIC Login Failed: {msg}"}

        base_domain, derived_ext = split_domain_ext(domain)
        if ext:
            ext = ext.strip()
            # The operator chose from the registry's dropdown, so the domain may
            # already include the extension. Drop it before re-adding.
            if base_domain.lower().endswith(ext.lower()):
                base_domain = base_domain[: -len(ext)]
        else:
            ext = derived_ext
        reg_date_str = reg_date or date.today().strftime("%Y-%m-%d")

        # Seed the builder with the named parameters, then let the full field
        # set from the form override them. Kept in this order so an explicit
        # form value always wins over a parameter default.
        provided: Dict[str, Any] = {
            "customername": customer_name, "address": address,
            "postalcode": postalcode, "phone": phone, "email": email,
            "country": country, "reg_renewal": reg_date_str,
        }
        provided.update(fields or {})
        data = build_registry_payload(provided, base_domain, ext, reg_date_str)

        missing = missing_registry_fields(data)
        if missing:
            # Caught here rather than at the portal: these are the registry's
            # required fields, and a rejection there gives no field names.
            return {"success": False,
                    "message": "nic.bt.bt requires these fields, which are empty: "
                               + ", ".join(missing)}

        existing_id = self.find_domain_id(base_domain, ext)

        try:
            if existing_id:
                # Update existing domain
                edit_url = f"{self.base_url}/domain/{existing_id}/edit"
                r_edit = self.session.get(edit_url, timeout=REGISTRY_TIMEOUT)
                _reject_unsupported_ext(ext, available_extensions(r_edit.text))
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_edit.text)
                csrf_token = token_match.group(1) if token_match else ""

                patch_url = f"{self.base_url}/domain/{existing_id}"
                data["_token"] = csrf_token
                data["_method"] = "PATCH"

                resp = _request_with_retry(
                    self.session, "POST", patch_url, data=data,
                    timeout=REGISTRY_TIMEOUT, allow_redirects=True,
                )
                if resp.status_code in (200, 302):
                    return {
                        "success": True,
                        "action": "updated",
                        "domain_id": existing_id,
                        "domain": f"{base_domain}{ext}",
                        "message": f"Domain {base_domain}{ext} successfully updated on nic.bt.bt (ID: {existing_id})."
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to update domain: HTTP {resp.status_code}"
                    }
            else:
                # Create new domain
                create_url = f"{self.base_url}/domain/create"
                r_create = self.session.get(create_url, timeout=REGISTRY_TIMEOUT)
                _reject_unsupported_ext(ext, available_extensions(r_create.text))
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_create.text)
                csrf_token = token_match.group(1) if token_match else ""

                post_url = f"{self.base_url}/domain"
                data["_token"] = csrf_token

                resp = _request_with_retry(
                    self.session, "POST", post_url, data=data,
                    timeout=REGISTRY_TIMEOUT, allow_redirects=True,
                )
                if resp.status_code in (200, 302) and "login" not in resp.url:
                    new_id = self.find_domain_id(base_domain, ext)
                    return {
                        "success": True,
                        "action": "created",
                        "domain_id": new_id,
                        "domain": f"{base_domain}{ext}",
                        "message": f"Domain {base_domain}{ext} successfully registered on nic.bt.bt."
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to register domain on nic.bt.bt (status {resp.status_code})."
                    }
        except Exception as e:
            logger.error(f"Failed to submit domain on nic.bt.bt: {e}")
            return {"success": False, "message": f"Exception occurred: {str(e)}"}

    def delete_domain(self, domain: str, confirm: bool = False) -> Dict[str, Any]:
        """
        Permanently delete a domain registration from the nic.bt.bt registry.

        THIS IS IRREVERSIBLE. The portal's per-row Delete control issues
        POST /domain/{id} with a _method=DELETE override plus a CSRF token,
        which is the same Laravel mechanism used for the PATCH update path
        above. Success is confirmed by re-querying the record rather than by
        trusting the response status alone.

        Args:
            domain: Full domain, e.g. 'wank.bt'.
            confirm: Must be True. Guards against accidental invocation.
        """
        if not confirm:
            return {
                "success": False,
                "message": "Refusing to delete without confirm=True. "
                           "This permanently removes the domain registration."
            }

        ok, msg = self._ensure_logged_in()
        if not ok:
            return {"success": False, "message": f"NIC Login Failed: {msg}"}

        base_domain, ext = split_domain_ext(domain)
        existing_id = self.find_domain_id(base_domain, ext)
        if not existing_id:
            return {
                "success": False,
                "message": f"Domain {base_domain}{ext} was not found in the nic.bt.bt "
                           "admin list. Nothing to delete."
            }

        try:
            # Re-read the listing to obtain a CSRF token bound to this session.
            # The token is not cached: a stale token would fail the POST.
            r_list = self.session.get(f"{self.base_url}/domain", timeout=REGISTRY_TIMEOUT)
            if r_list.status_code != 200:
                return {
                    "success": False,
                    "message": f"Could not load the domain list to obtain a CSRF token: "
                               f"HTTP {r_list.status_code}"
                }
            token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_list.text)
            if not token_match:
                return {
                    "success": False,
                    "message": "CSRF token not found on the domain list page; refusing to POST."
                }
            csrf_token = token_match.group(1)

            delete_url = f"{self.base_url}/domain/{existing_id}"
            resp = _request_with_retry(
                self.session, "POST", delete_url,
                data={"_token": csrf_token, "_method": "DELETE"},
                timeout=REGISTRY_TIMEOUT,
                allow_redirects=True,
            )

            if "login" in resp.url:
                return {
                    "success": False,
                    "message": "Session expired during delete; the request was not applied."
                }

            # A 200 is also returned on validation errors, so confirm the record
            # is actually gone instead of inferring success from the status code.
            if self.find_domain_id(base_domain, ext):
                return {
                    "success": False,
                    "message": f"Delete request returned HTTP {resp.status_code} but domain "
                               f"{base_domain}{ext} (ID: {existing_id}) still exists. "
                               "The record was not removed."
                }

            return {
                "success": True,
                "action": "deleted",
                "domain_id": existing_id,
                "domain": f"{base_domain}{ext}",
                "message": f"Domain {base_domain}{ext} (ID: {existing_id}) successfully deleted "
                           "from nic.bt.bt."
            }
        except Exception as e:
            logger.error(f"Failed to delete domain on nic.bt.bt: {e}")
            return {"success": False, "message": f"Exception occurred: {str(e)}"}

    def query_whois(self, domain: str) -> Dict[str, Any]:
        """
        Public WHOIS lookup from nic.bt.bt for the given domain.
        """
        base_domain, ext = split_domain_ext(domain)
        search_url = f"{self.base_url}/search?query={base_domain}&ext={ext}"

        try:
            r = self.session.get(search_url, timeout=REGISTRY_TIMEOUT)
            if r.status_code != 200:
                return {
                    "found": False,
                    "domain": domain,
                    "message": f"WHOIS query failed: HTTP {r.status_code}"
                }

            text = r.text
            if "Could not be found in this query" in text:
                return {
                    "found": False,
                    "domain": f"{base_domain}{ext}",
                    "message": f"Domain {base_domain}{ext} not found in nic.bt.bt WHOIS database."
                }

            # Parse WHOIS fields
            details = {}
            for line in re.sub(r"<[^>]+>", "\n", text).splitlines():
                line = line.strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    k_clean = k.strip()
                    v_clean = v.strip()
                    if k_clean and v_clean:
                        details[k_clean] = v_clean

            return {
                "found": True,
                "domain": f"{base_domain}{ext}",
                "data": details,
                "raw_text": "\n".join([f"{k}: {v}" for k, v in details.items()])
            }
        except Exception as e:
            return {"found": False, "domain": domain, "message": str(e)}
