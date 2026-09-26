import logging
import re
from datetime import date
from typing import Dict, Any, Optional, Tuple
import requests
from config import settings
from tls_config import resolve_verify

logger = logging.getLogger(__name__)


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
            r = self.session.get(login_url, timeout=15)
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

            resp = self.session.post(login_url, data=payload, timeout=15, allow_redirects=True)
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
            r = self.session.get(f"{self.base_url}/domain", timeout=15)
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
                r_edit = self.session.get(edit_url, timeout=15)
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_edit.text)
                csrf_token = token_match.group(1) if token_match else ""

                patch_url = f"{self.base_url}/domain/{existing_id}"
                data = {
                    "_token": csrf_token,
                    "_method": "PATCH",
                    "domain": base_domain,
                    "registrar": "DrukNet",
                    "reg_renewal": reg_date_str,
                    "customername": customer_name,
                    "address": address or "Thimphu, Bhutan",
                    "postalcode": postalcode or "-",
                    "phone": phone or "+975",
                    "email": email,
                    "country": country or "BT",
                    "tech_name": "DrukNet Systems",
                    "tech_address": "Bhutan Telecom Ltd, Thimphu",
                    "tech_postalcode": "-",
                    "tech_phone": "+975-2-343434",
                    "tech_fax": "-",
                    "tech_country": "BT",
                    "tech_email": "systems@bt.bt",
                    "billing_name": customer_name,
                    "billing_address": address or "Thimphu, Bhutan",
                    "billing_contact": phone or "+975",
                    "billing_fax": "-",
                    "billing_country": country or "BT",
                    "billing_email": email
                }

                resp = self.session.post(patch_url, data=data, timeout=15, allow_redirects=True)
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
                r_create = self.session.get(create_url, timeout=15)
                token_match = re.search(r'name=["\x27]_token["\x27]\s+value=["\x27]([^"\x27]+)["\x27]', r_create.text)
                csrf_token = token_match.group(1) if token_match else ""

                post_url = f"{self.base_url}/domain"
                data = {
                    "_token": csrf_token,
                    "domain": base_domain,
                    "ext": ext,
                    "registrar": "DrukNet",
                    "reg_renewal": reg_date_str,
                    "customername": customer_name,
                    "address": address or "Thimphu, Bhutan",
                    "postalcode": postalcode or "-",
                    "phone": phone or "+975",
                    "email": email,
                    "country": country or "BT",
                    "tech_name": "DrukNet Systems",
                    "tech_address": "Bhutan Telecom Ltd, Thimphu",
                    "tech_postalcode": "-",
                    "tech_phone": "+975-2-343434",
                    "tech_fax": "-",
                    "tech_country": "BT",
                    "tech_email": "systems@bt.bt",
                    "billing_name": customer_name,
                    "billing_address": address or "Thimphu, Bhutan",
                    "billing_contact": phone or "+975",
                    "billing_fax": "-",
                    "billing_country": country or "BT",
                    "billing_email": email
                }

                resp = self.session.post(post_url, data=data, timeout=15, allow_redirects=True)
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

    def query_whois(self, domain: str) -> Dict[str, Any]:
        """
        Public WHOIS lookup from nic.bt.bt for the given domain.
        """
        base_domain, ext = split_domain_ext(domain)
        search_url = f"{self.base_url}/search?query={base_domain}&ext={ext}"

        try:
            r = self.session.get(search_url, timeout=15)
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
