import urllib.parse
import logging
from typing import Optional, Dict, Any
import requests
import urllib3

from .base import (
    BaseProvisioner,
    ProvisionerResult,
    generate_secure_password,
    sanitize_username
)
from .ssh_client import SSHExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

class DirectAdminProvisioner(BaseProvisioner):
    """
    Automates account creation on DirectAdmin server using either DirectAdmin API
    (CMD_API_ACCOUNT_USER) or SSH execution.
    """
    def __init__(
        self,
        host: str,
        ssh_port: int = 22,
        ssh_user: str = "root",
        ssh_password: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
        api_user: str = "admin",
        api_password: Optional[str] = None,
        web_url: Optional[str] = None,
        sftp_port: int = 22,
        default_package: str = "default",
        nameservers: str = "ns1.yourdomain.bt, ns2.yourdomain.bt"
    ):
        self.host = host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.ssh_key_path = ssh_key_path
        self.api_user = api_user
        self.api_password = api_password
        self.web_url = web_url or f"https://{host}:2222"
        # Port extracted so each customer gets their own domain-based login URL
        try:
            from urllib.parse import urlparse as _uparse
            self.web_port = _uparse(self.web_url).port or 2222
        except Exception:
            self.web_port = 2222
        self.sftp_port = sftp_port
        self.default_package = default_package
        self.nameservers = nameservers
        
        self.ssh = SSHExecutor(
            host=self.host,
            port=self.ssh_port,
            user=self.ssh_user,
            password=self.ssh_password,
            key_path=self.ssh_key_path
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test DirectAdmin connection via API or SSH."""
        if self.api_password:
            try:
                url = f"https://{self.host}:2222/CMD_API_SHOW_USERS"
                auth = (self.api_user, self.api_password)
                resp = requests.get(url, auth=auth, verify=False, timeout=10)
                if resp.status_code == 200 and "error=1" not in resp.text:
                    return {"success": True, "method": "DIRECTADMIN_API", "message": "Connected to DirectAdmin API successfully."}
            except Exception as e:
                logger.warning(f"DirectAdmin API test failed, testing SSH: {e}")

        ssh_ok, ssh_msg = self.ssh.test_connection()
        return {"success": ssh_ok, "method": "SSH", "message": ssh_msg}

    def create_account(
        self,
        domain: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        email: Optional[str] = None,
        package: Optional[str] = None,
        quota_mb: Optional[int] = None,
        dry_run: bool = False
    ) -> ProvisionerResult:
        """
        Creates a new user account in DirectAdmin.
        """
        domain = domain.strip().lower()
        username = username.strip().lower() if username else sanitize_username(domain, max_length=14)
        password = password or generate_secure_password(16)
        email = email.strip() if email else f"admin@{domain}"
        pkg = package or self.default_package
        doc_root = f"domains/{domain}/public_html/"
        # Customer's own domain is the login URL host, not the server hostname
        customer_web_url = f"https://{domain}:{self.web_port}"

        if dry_run:
            handover_text = self.format_handover(
                panel="DirectAdmin",
                domain=domain,
                username=username,
                password=password,
                web_url=customer_web_url,
                sftp_host=self.host,
                sftp_port=self.sftp_port,
                doc_root=doc_root,
                nameservers=self.nameservers
            )
            return ProvisionerResult(
                success=True,
                panel="directadmin",
                domain=domain,
                username=username,
                password=password,
                email=email,
                web_url=customer_web_url,
                sftp_host=self.host,
                sftp_port=self.sftp_port,
                doc_root=doc_root,
                nameservers=self.nameservers,
                message=f"[DRY-RUN] Simulated DirectAdmin user creation for '{domain}'. (DocRoot: {doc_root})",
                handover_text=handover_text,
                raw_response={"simulated": True, "target_host": self.host}
            )

        # Method 1: DirectAdmin API (CMD_API_ACCOUNT_USER)
        if self.api_password:
            result = self._create_via_api(domain, username, password, email, pkg, doc_root, customer_web_url)
            if result.success:
                return result
            logger.warning(f"DirectAdmin API failed: {result.message}. Trying SSH fallback...")

        # Method 2: SSH execution
        return self._create_via_ssh(domain, username, password, email, pkg, doc_root, customer_web_url)

    def _create_via_api(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        pkg: str,
        doc_root: str,
        customer_web_url: str = ""
    ) -> ProvisionerResult:
        url = f"https://{self.host}:2222/CMD_API_ACCOUNT_USER"
        auth = (self.api_user, self.api_password)
        data = {
            "action": "create",
            "add": "Submit",
            "username": username,
            "email": email,
            "passwd": password,
            "passwd2": password,
            "domain": domain,
            "package": pkg,
            "notify": "no"
        }

        try:
            resp = requests.post(url, auth=auth, data=data, verify=False, timeout=30)
            parsed = urllib.parse.parse_qs(resp.text)
            
            # DirectAdmin API returns 'error=0' or details on success, or 'error=1' on error
            has_error = parsed.get("error", ["0"])[0] == "1"
            details = parsed.get("details", [resp.text])[0]
            text = parsed.get("text", [""])[0]

            if not has_error and resp.status_code == 200 and ("User Created" in details or "error=0" in resp.text):
                handover_text = self.format_handover(
                    panel="DirectAdmin",
                    domain=domain,
                    username=username,
                    password=password,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root=doc_root,
                    nameservers=self.nameservers
                )
                return ProvisionerResult(
                    success=True,
                    panel="directadmin",
                    domain=domain,
                    username=username,
                    password=password,
                    email=email,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root=doc_root,
                    nameservers=self.nameservers,
                    message="DirectAdmin user account created successfully via API.",
                    handover_text=handover_text,
                    raw_response=resp.text
                )
            else:
                return self._build_failure_result(
                    domain, username, password, email, doc_root,
                    f"DirectAdmin Error: {text} - {details}",
                    resp.text
                )
        except Exception as e:
            return self._build_failure_result(
                domain, username, password, email, doc_root,
                f"DirectAdmin API request failed: {str(e)}"
            )

    def list_packages(self) -> list:
        """Fetch available packages on the DirectAdmin server via API (preferred) or SSH fallback."""
        # Prefer API method — faster, no sudo escalation needed
        if self.api_password:
            try:
                url = f"https://{self.host}:2222/CMD_API_PACKAGES_USER"
                auth = (self.api_user, self.api_password)
                resp = requests.get(url, auth=auth, verify=False, timeout=10)
                if resp.status_code == 200:
                    import urllib.parse as _up
                    parsed = _up.parse_qs(resp.text)
                    pkgs = parsed.get("list[]", [])
                    if pkgs:
                        return sorted(list(set(pkgs)))
            except Exception as e:
                logger.warning(f"DA API package list failed, trying SSH fallback: {e}")

        # SSH fallback
        cmd = "ls -1 /usr/local/directadmin/data/users/admin/packages/ 2>/dev/null | sed 's/\\.pkg$//'"
        if self.ssh_user != "root":
            if self.ssh_password:
                cmd = f"echo '{self.ssh_password}' | sudo -S sh -c \"{cmd}\""
            else:
                cmd = f"sudo -n sh -c \"{cmd}\""
        try:
            code, stdout, stderr = self.ssh.execute(cmd)
            clean = [l.strip() for l in stdout.splitlines() if l.strip() and not l.startswith("[sudo]")]
            return sorted(list(set(clean))) if clean else ["Bronze", "SILVER", "Gold", "PLATINUM", "default"]
        except Exception:
            return ["Bronze", "SILVER", "Gold", "PLATINUM", "default"]

    def _create_via_ssh(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        pkg: str,
        doc_root: str,
        customer_web_url: str = ""
    ) -> ProvisionerResult:
        escaped_password = password.replace("'", "'\\''")
        escaped_user = username.replace("'", "'\\''")
        escaped_domain = domain.replace("'", "'\\''")
        escaped_email = email.replace("'", "'\\''")
        escaped_pkg = pkg.replace("'", "'\\''")
        
        # Use on-demand authorized API URL from /usr/bin/da api-url inside server
        da_cmd = (
            f"API_URL=$(/usr/bin/da api-url | tail -n1); "
            f"curl -s -k -X POST \"$API_URL/CMD_API_ACCOUNT_USER\" "
            f"-d \"action=create\" "
            f"-d \"add=Submit\" "
            f"-d \"username={escaped_user}\" "
            f"-d \"email={escaped_email}\" "
            f"-d \"passwd={escaped_password}\" "
            f"-d \"passwd2={escaped_password}\" "
            f"-d \"domain={escaped_domain}\" "
            f"-d \"package={escaped_pkg}\" "
            f"-d \"ip=202.144.128.131\" "
            f"-d \"notify=no\""
        )

        if self.ssh_user != "root":
            if self.ssh_password:
                escaped_sudo_pw = self.ssh_password.replace("'", "'\\''")
                da_cmd = f"echo '{escaped_sudo_pw}' | sudo -S sh -c '{da_cmd}'"
            else:
                da_cmd = f"sudo -n sh -c '{da_cmd}'"

        try:
            exit_code, stdout, stderr = self.ssh.execute(da_cmd)
            clean_out = "\n".join(l for l in stdout.splitlines() if not l.startswith("[sudo]")).strip()
            parsed = urllib.parse.parse_qs(clean_out)
            has_error = parsed.get("error", ["0"])[0] == "1"
            details = parsed.get("details", [clean_out])[0]
            text = parsed.get("text", [""])[0]

            if not has_error and exit_code == 0 and ("User Created" in clean_out or "error=0" in clean_out):
                handover_text = self.format_handover(
                    panel="DirectAdmin",
                    domain=domain,
                    username=username,
                    password=password,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root=doc_root,
                    nameservers=self.nameservers
                )
                return ProvisionerResult(
                    success=True,
                    panel="directadmin",
                    domain=domain,
                    username=username,
                    password=password,
                    email=email,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root=doc_root,
                    nameservers=self.nameservers,
                    message="DirectAdmin account created successfully via SSH.",
                    handover_text=handover_text,
                    raw_response={"stdout": clean_out}
                )
            else:
                return self._build_failure_result(
                    domain, username, password, email, doc_root,
                    f"DirectAdmin Error: {text} - {details}",
                    {"stdout": clean_out, "stderr": stderr}
                )
        except Exception as e:
            return self._build_failure_result(
                domain, username, password, email, doc_root,
                f"DirectAdmin SSH command execution error: {str(e)}"
            )

    def _build_failure_result(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        doc_root: str,
        message: str,
        raw: Any = None
    ) -> ProvisionerResult:
        return ProvisionerResult(
            success=False,
            panel="directadmin",
            domain=domain,
            username=username,
            password=password,
            email=email,
            web_url=self.web_url,
            sftp_host=self.host,
            sftp_port=self.sftp_port,
            doc_root=doc_root,
            nameservers=self.nameservers,
            message=message,
            raw_response=raw
        )
