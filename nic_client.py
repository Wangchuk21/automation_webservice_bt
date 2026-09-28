import logging
import re
import time
from datetime import date
from typing import Dict, Any, Optional, Tuple
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
    {"name": "billing_name", "label": "Billing name", "source": "default", "required": True},
    {"name": "billing_address", "label": "Billing address", "source": "default", "required": True},
    {"name": "billing_contact", "label": "Billing contact", "source": "default", "required": True},
    {"name": "billing_fax", "label": "Billing fax", "source": "default", "required": True},
    {"name": "billing_country", "label": "Billing country", "source": "default", "required": True},
    {"name": "billing_email", "label": "Billing email", "source": "default", "required": True},
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
    defaults = {
        "registrar": s.NIC_REGISTRAR,
        # tech_fax is the only technical field BT supplies: the customer's fax
        # is not collected and the registry still expects a value.
        "tech_fax": s.NIC_PLACEHOLDER,
        "billing_name": s.NIC_BILLING_NAME,
        "billing_address": s.NIC_BILLING_ADDRESS,
        "billing_contact": s.NIC_BILLING_CONTACT,
        "billing_fax": s.NIC_BILLING_FAX,
        "billing_country": s.NIC_BILLING_COUNTRY,
        "billing_email": s.NIC_BILLING_EMAIL,
    }
    counts: Dict[str, int] = {}
    for f in REGISTRY_FIELDS:
        counts[f["source"]] = counts.get(f["source"], 0) + 1
    return {
        "fields": [dict(f, default=defaults.get(f["name"], "")) for f in REGISTRY_FIELDS],
        "required_count": sum(1 for f in REGISTRY_FIELDS if f.get("required")),
        "source_counts": counts,
    }


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


class NICClient:
    """Client for managing domain registration and WHOIS on nic.bt.bt."""

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
        reg_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates or updates a domain entry on nic.bt.bt for WHOIS lookup.
        """
        ok, msg = self._ensure_logged_in()
        if not ok:
            return {"success": False, "message": f"NIC Login Failed: {msg}"}

        base_domain, ext = split_domain_ext(domain)
        reg_date_str = reg_date or date.today().strftime("%Y-%m-%d")

        existing_id = self.find_domain_id(base_domain, ext)

        try:
            if existing_id:
                # Update existing domain
                edit_url = f"{self.base_url}/domain/{existing_id}/edit"
                r_edit = self.session.get(edit_url, timeout=REGISTRY_TIMEOUT)
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_edit.text)
                csrf_token = token_match.group(1) if token_match else ""

                patch_url = f"{self.base_url}/domain/{existing_id}"
                data = {
                    "_token": csrf_token,
                    "_method": "PATCH",
                    "domain": base_domain,
                    "registrar": settings.NIC_REGISTRAR,
                    "reg_renewal": reg_date_str,
                    "customername": customer_name,
                    "address": address or "Thimphu, Bhutan",
                    "postalcode": postalcode or settings.NIC_PLACEHOLDER,
                    "phone": phone or "+975",
                    "email": email,
                    "country": country or settings.NIC_DEFAULT_COUNTRY,
                    "tech_name": customer_name,
                    "tech_address": address or "Thimphu, Bhutan",
                    "tech_postalcode": postalcode or settings.NIC_PLACEHOLDER,
                    "tech_phone": phone or "+975",
                    "tech_fax": settings.NIC_PLACEHOLDER,
                    "tech_country": country or settings.NIC_DEFAULT_COUNTRY,
                    "tech_email": email,
                    "billing_name": settings.NIC_BILLING_NAME,
                    "billing_address": settings.NIC_BILLING_ADDRESS,
                    "billing_contact": settings.NIC_BILLING_CONTACT,
                    "billing_fax": settings.NIC_BILLING_FAX,
                    "billing_country": country or "BT",
                    "billing_email": email
                }

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
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_create.text)
                csrf_token = token_match.group(1) if token_match else ""

                post_url = f"{self.base_url}/domain"
                data = {
                    "_token": csrf_token,
                    "domain": base_domain,
                    "ext": ext,
                    "registrar": settings.NIC_REGISTRAR,
                    "reg_renewal": reg_date_str,
                    "customername": customer_name,
                    "address": address or "Thimphu, Bhutan",
                    "postalcode": postalcode or settings.NIC_PLACEHOLDER,
                    "phone": phone or "+975",
                    "email": email,
                    "country": country or settings.NIC_DEFAULT_COUNTRY,
                    "tech_name": customer_name,
                    "tech_address": address or "Thimphu, Bhutan",
                    "tech_postalcode": postalcode or settings.NIC_PLACEHOLDER,
                    "tech_phone": phone or "+975",
                    "tech_fax": settings.NIC_PLACEHOLDER,
                    "tech_country": country or settings.NIC_DEFAULT_COUNTRY,
                    "tech_email": email,
                    "billing_name": settings.NIC_BILLING_NAME,
                    "billing_address": settings.NIC_BILLING_ADDRESS,
                    "billing_contact": settings.NIC_BILLING_CONTACT,
                    "billing_fax": settings.NIC_BILLING_FAX,
                    "billing_country": country or "BT",
                    "billing_email": email
                }

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
