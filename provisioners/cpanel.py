import json
import logging
import shlex
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

class CPanelProvisioner(BaseProvisioner):
    """
    Automates account creation on cPanel/WHM server using either SSH commands
    (whmapi1 / scripts/createacct) or WHM REST API 1.
    """
    def __init__(
        self,
        host: str,
        ssh_port: int = 22,
        ssh_user: str = "root",
        ssh_password: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
        whm_api_token: Optional[str] = None,
        whm_password: Optional[str] = None,
        whm_user: str = "root",
        web_url: Optional[str] = None,
        sftp_port: int = 22,
        default_plan: str = "default",
        nameservers: str = "ns1.yourdomain.bt, ns2.yourdomain.bt"
    ):
        self.host = host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.ssh_key_path = ssh_key_path
        self.whm_api_token = whm_api_token
        self.whm_password = whm_password
        self.whm_user = whm_user
        self.web_url = web_url or f"https://{host}:2083"
        # Port extracted so each customer gets their own domain-based login URL
        try:
            from urllib.parse import urlparse as _uparse
            self.web_port = _uparse(self.web_url).port or 2083
        except Exception:
            self.web_port = 2083
        self.sftp_port = sftp_port
        self.default_plan = default_plan
        self.nameservers = nameservers
        
        self.ssh = SSHExecutor(
            host=self.host,
            port=self.ssh_port,
            user=self.ssh_user,
            password=self.ssh_password,
            key_path=self.ssh_key_path
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test connectivity via API (if token or password provided) or SSH."""
        if self.whm_api_token or self.whm_password:
            try:
                url = f"https://{self.host}:2087/json-api/version?api.version=1"
                headers = {}
                auth = None
                if self.whm_api_token:
                    headers["Authorization"] = f"whm {self.whm_user}:{self.whm_api_token}"
                else:
                    auth = (self.whm_user, self.whm_password)
                
                resp = requests.get(url, headers=headers, auth=auth, verify=False, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    return {"success": True, "method": "WHM_API", "message": f"Connected to cPanel/WHM API (Version: {data.get('version', 'unknown')})"}
            except Exception as e:
                logger.warning(f"WHM API test failed, falling back to SSH: {e}")

        # Test SSH
        ssh_ok, ssh_msg = self.ssh.test_connection()
        return {"success": ssh_ok, "method": "SSH", "message": ssh_msg}

    def list_packages(self) -> list:
        """Fetch available packages on the cPanel server."""
        cmd = "whmapi1 --output=json listpkgs"
        sudo_stdin = None
        if self.ssh_user != "root":
            if self.ssh_password:
                cmd = "sudo -S -p '' " + cmd
                sudo_stdin = self.ssh_password + "\n"
            else:
                cmd = "sudo -n " + cmd

        try:
            code, stdout, stderr = self.ssh.execute(cmd, stdin_data=sudo_stdin)
            clean = "\n".join(l for l in stdout.splitlines() if not l.startswith("[sudo]")).strip()
            data = json.loads(clean)
            return [p.get("name") for p in data.get("data", {}).get("pkg", [])]
        except Exception:
            return ["Bronze", "Silver", "Gold", "Platinum", "default"]

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
        Creates a new cPanel account and configures it for customer Web UI & SFTP access.
        """
        domain = domain.strip().lower()
        username = username.strip().lower() if username else sanitize_username(domain, max_length=16)
        password = password or generate_secure_password(16)
        email = email.strip() if email else f"admin@{domain}"
        plan = package or self.default_plan
        # Customer's own domain is the login URL host, not the server hostname
        customer_web_url = f"https://{domain}:{self.web_port}"

        if dry_run:
            handover_text = self.format_handover(
                panel="cPanel",
                domain=domain,
                username=username,
                password=password,
                web_url=customer_web_url,
                sftp_host=self.host,
                sftp_port=self.sftp_port,
                doc_root="public_html/",
                nameservers=self.nameservers
            )
            return ProvisionerResult(
                success=True,
                panel="cpanel",
                domain=domain,
                username=username,
                password=password,
                email=email,
                web_url=customer_web_url,
                sftp_host=self.host,
                sftp_port=self.sftp_port,
                doc_root="public_html/",
                nameservers=self.nameservers,
                message=f"[DRY-RUN] Simulated cPanel user creation for '{domain}'. (SSH command: whmapi1 createacct username='{username}' domain='{domain}')",
                handover_text=handover_text,
                raw_response={"simulated": True, "target_host": self.host}
            )

        # Attempt Method 1: WHM REST API if token exists
        if self.whm_api_token:
            result = self._create_via_api(domain, username, password, email, plan, quota_mb, customer_web_url)
            if result.success:
                return result
            logger.warning(f"WHM API account creation failed ({result.message}). Trying SSH fallback...")

        # Method 2: SSH execution (whmapi1 or /scripts/createacct)
        return self._create_via_ssh(domain, username, password, email, plan, quota_mb, customer_web_url)

    def _create_via_ssh(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        plan: str,
        quota_mb: Optional[int],
        customer_web_url: str = ""
    ) -> ProvisionerResult:
        # Every value interpolated into the remote shell command MUST go through
        # shlex.quote(). Escaping only the password left username, domain,
        # contactemail and plan injectable: a domain of
        #   x.bt'; id > /tmp/pwn; echo '
        # closed the quote and executed a second command as root via sudo.
        # shlex.quote() neutralises every shell metacharacter, not just quotes.
        q_username = shlex.quote(username)
        q_domain = shlex.quote(domain)
        q_password = shlex.quote(password)
        q_email = shlex.quote(email)
        q_plan = shlex.quote(plan)

        # Command using WHM API CLI tool directly on the server
        # whmapi1 createacct --output=json username=... domain=... password=... plan=...
        cmd = (
            f"whmapi1 --output=json createacct "
            f"username={q_username} "
            f"domain={q_domain} "
            f"password={q_password} "
            f"contactemail={q_email}"
        )
        if plan and plan.lower() != "default":
            cmd += f" plan={q_plan}"
        if quota_mb:
            # quota_mb is an int, so it cannot carry shell syntax.
            cmd += f" quota={int(quota_mb)}"

        # The sudo password is fed via stdin, never interpolated into the command.
        sudo_stdin = None
        if self.ssh_user != "root":
            if self.ssh_password:
                # -S reads the password from stdin; -p '' suppresses the prompt so
                # nothing is echoed back into stdout.
                cmd = "sudo -S -p '' " + cmd
                sudo_stdin = self.ssh_password + "\n"
            else:
                cmd = "sudo -n " + cmd

        try:
            exit_code, stdout, stderr = self.ssh.execute(cmd, stdin_data=sudo_stdin)
            clean_stdout = "\n".join(l for l in stdout.splitlines() if not l.startswith("[sudo]")).strip()
            
            # Check if whmapi1 succeeded
            if exit_code == 0 and clean_stdout:
                try:
                    data = json.loads(clean_stdout)
                    metadata = data.get("metadata", {})
                    if metadata.get("result") == 1:
                        handover_text = self.format_handover(
                            panel="cPanel",
                            domain=domain,
                            username=username,
                            password=password,
                            web_url=customer_web_url,
                            sftp_host=self.host,
                            sftp_port=self.sftp_port,
                            doc_root="public_html/",
                            nameservers=self.nameservers
                        )
                        return ProvisionerResult(
                            success=True,
                            panel="cpanel",
                            domain=domain,
                            username=username,
                            password=password,
                            email=email,
                            web_url=customer_web_url,
                            sftp_host=self.host,
                            sftp_port=self.sftp_port,
                            doc_root="public_html/",
                            nameservers=self.nameservers,
                            message="cPanel account created successfully via SSH (whmapi1).",
                            handover_text=handover_text,
                            raw_response=data
                        )
                    else:
                        error_msg = metadata.get("reason", "Unknown WHM error")
                        return self._build_failure_result(domain, username, password, email, error_msg, data)
                except json.JSONDecodeError:
                    pass

            # Fallback to /scripts/createacct
            fallback_cmd = (
                f"/usr/local/cpanel/scripts/createacct "
                f"--domain={q_domain} --user={q_username} "
                f"--pass={q_password} --contactemail={q_email}"
            )
            if plan and plan.lower() != "default":
                fallback_cmd += f" --plan={q_plan}"

            if self.ssh_user != "root":
                if self.ssh_password:
                    fallback_cmd = "sudo -S -p '' " + fallback_cmd
                    sudo_stdin = self.ssh_password + "\n"
                else:
                    fallback_cmd = "sudo -n " + fallback_cmd

            fb_code, fb_out, fb_err = self.ssh.execute(fallback_cmd, stdin_data=sudo_stdin)
            if fb_code == 0 and ("Account Creation Complete" in fb_out or "WWWAcct" in fb_out):
                handover_text = self.format_handover(
                    panel="cPanel",
                    domain=domain,
                    username=username,
                    password=password,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root="public_html/",
                    nameservers=self.nameservers
                )
                return ProvisionerResult(
                    success=True,
                    panel="cpanel",
                    domain=domain,
                    username=username,
                    password=password,
                    email=email,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root="public_html/",
                    nameservers=self.nameservers,
                    message="cPanel account created successfully via /scripts/createacct.",
                    handover_text=handover_text,
                    raw_response={"stdout": fb_out}
                )
            
            return self._build_failure_result(
                domain, username, password, email,
                f"SSH command failed (code {exit_code}): {stderr or stdout or fb_err or fb_out}"
            )
        except Exception as e:
            return self._build_failure_result(
                domain, username, password, email,
                f"Failed to execute SSH provisioning: {str(e)}"
            )

    def _create_via_api(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        plan: str,
        quota_mb: Optional[int],
        customer_web_url: str = ""
    ) -> ProvisionerResult:
        url = f"https://{self.host}:2087/json-api/createacct?api.version=1"
        headers = {}
        auth = None
        if self.whm_api_token:
            headers["Authorization"] = f"whm {self.whm_user}:{self.whm_api_token}"
        elif self.whm_password:
            auth = (self.whm_user, self.whm_password)

        params = {
            "username": username,
            "domain": domain,
            "password": password,
            "contactemail": email,
        }
        if plan and plan.lower() != "default":
            params["plan"] = plan
        if quota_mb:
            params["quota"] = quota_mb

        try:
            response = requests.get(url, headers=headers, auth=auth, params=params, verify=False, timeout=30)
            data = response.json()
            metadata = data.get("metadata", {})
            if metadata.get("result") == 1:
                handover_text = self.format_handover(
                    panel="cPanel",
                    domain=domain,
                    username=username,
                    password=password,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root="public_html/",
                    nameservers=self.nameservers
                )
                return ProvisionerResult(
                    success=True,
                    panel="cpanel",
                    domain=domain,
                    username=username,
                    password=password,
                    email=email,
                    web_url=customer_web_url,
                    sftp_host=self.host,
                    sftp_port=self.sftp_port,
                    doc_root="public_html/",
                    nameservers=self.nameservers,
                    message="cPanel account created successfully via WHM REST API.",
                    handover_text=handover_text,
                    raw_response=data
                )
            else:
                return self._build_failure_result(
                    domain, username, password, email,
                    metadata.get("reason", "API returned failure status"),
                    data
                )
        except Exception as e:
            return self._build_failure_result(
                domain, username, password, email,
                f"WHM API request failed: {str(e)}"
            )

    def _build_failure_result(
        self,
        domain: str,
        username: str,
        password: str,
        email: str,
        message: str,
        raw: Any = None
    ) -> ProvisionerResult:
        return ProvisionerResult(
            success=False,
            panel="cpanel",
            domain=domain,
            username=username,
            password=password,
            email=email,
            web_url=self.web_url,
            sftp_host=self.host,
            sftp_port=self.sftp_port,
            doc_root="public_html/",
            nameservers=self.nameservers,
            message=message,
            raw_response=raw
        )
