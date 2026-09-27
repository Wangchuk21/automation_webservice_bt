import json
import logging
import re
import shlex
from typing import Optional, Dict, Any
import requests

from .base import (
    BaseProvisioner,
    ProvisionerResult,
    ValidationError,
    generate_secure_password,
    sanitize_username
)
from .ssh_client import SSHExecutor
from tls_config import resolve_verify, api_base_url

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
        tls_hostname: str = "",
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
        # DNS name on the server certificate; used for HTTPS calls so hostname
        # verification succeeds when the server is addressed by IP.
        self.tls_hostname = tls_hostname or ""
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
                url = f"{api_base_url(self.host, self.tls_hostname, 2087)}/json-api/version?api.version=1"
                headers = {}
                auth = None
                if self.whm_api_token:
                    headers["Authorization"] = f"whm {self.whm_user}:{self.whm_api_token}"
                else:
                    auth = (self.whm_user, self.whm_password)

                resp = requests.get(url, headers=headers, auth=auth,
                                    verify=resolve_verify(), timeout=10)
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
        url = f"{api_base_url(self.host, self.tls_hostname, 2087)}/json-api/createacct?api.version=1"
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
            response = requests.get(url, headers=headers, auth=auth, params=params,
                                    verify=resolve_verify(), timeout=30)
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

    def account_exists(self, username: str) -> bool:
        """
        Read-only check for whether a system account exists on the server.

        Uses getent(1), which only reads the account database. Used to make
        deletion idempotent and to confirm a target before acting on it.
        """
        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            raise ValidationError(f"Invalid cPanel username: {username!r}")
        try:
            code, stdout, _ = self.ssh.execute(f"getent passwd {shlex.quote(username)}")
            return code == 0 and bool(stdout.strip())
        except Exception as e:
            logger.warning(f"Existence check for '{username}' failed: {e}")
            return False

    def account_state(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Current suspension state of a single account, read from listaccts.

        Read-only. Returns None when the account does not exist. This is the
        authoritative pre-check the suspension rules depend on: it is what
        distinguishes "already suspended for billing" (skip) from "suspended
        for abuse" (skip, and do not rewrite the reason) from "not suspended"
        (the only state in which suspension is allowed).
        """
        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            raise ValidationError(f"Invalid cPanel username: {username!r}")
        for acct in self.list_accounts():
            if acct["username"] == username:
                return acct
        return None

    def list_accounts(self) -> list:
        """
        Every hosting account, with its domain and current suspension state.

        Read-only. This is the work list for suspension: it is complete and
        authoritative for the accounts this panel manages, unlike enumerating
        from the billing system, which cannot be paged through.
        """
        cmd = "whmapi1 --output=json listaccts"
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
            accts = json.loads(clean)["data"]["acct"]
        except Exception as e:
            logger.error(f"list_accounts failed: {e}")
            return []

        out = []
        for a in accts:
            out.append({
                "panel": "cpanel",
                "username": a.get("user", ""),
                "domain": (a.get("domain") or "").strip().lower(),
                # listaccts reports these as strings; coerce defensively.
                "suspended": str(a.get("suspended", "0")).strip() in ("1", "yes", "true"),
                "reason": (a.get("suspendreason") or "").strip(),
                "suspend_time": a.get("suspendtime") or "",
            })
        return out

    def suspend_account(
        self,
        username: str,
        reason: str = "billing",
        confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Suspend a hosting account, stopping the website.

        Unlike removal, suspension is reversible (whmapi1 unsuspendacct), but it
        still takes a live site offline, so it is gated on confirm=True and
        refuses to touch an account that is already suspended -- overwriting the
        reason would destroy the record of why it was suspended (abuse, spam and
        so on are not billing).

        The reason is recorded in the server audit log.
        """
        if not confirm:
            return {"success": False,
                    "message": "Refusing to suspend without confirm=True."}
        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            return {"success": False, "message": f"Invalid cPanel username: {username!r}"}

        state = self.account_state(username)
        if state is None:
            return {"success": False,
                    "message": f"Account '{username}' does not exist on {self.host}."}
        if state["suspended"]:
            return {"success": False, "already_suspended": True,
                    "message": f"'{username}' is already suspended "
                               f"(reason: {state['reason'] or 'not recorded'}). Left untouched."}

        cmd = (f"whmapi1 --output=json suspendacct "
               f"user={shlex.quote(username)} reason={shlex.quote(reason)}")
        sudo_stdin = None
        if self.ssh_user != "root":
            if self.ssh_password:
                cmd = "sudo -S -p '' " + cmd
                sudo_stdin = self.ssh_password + "\n"
            else:
                cmd = "sudo -n " + cmd
        try:
            code, stdout, stderr = self.ssh.execute(cmd, stdin_data=sudo_stdin)
        except Exception as e:
            return {"success": False, "message": f"SSH execution failed: {e}"}

        clean = "\n".join(l for l in stdout.splitlines() if not l.startswith("[sudo]")).strip()
        # whmapi1 reports failure in JSON with exit code 0, so parse the result
        # rather than trusting the exit status.
        if clean:
            try:
                meta = json.loads(clean).get("metadata", {})
                if str(meta.get("result")) == "0":
                    return {"success": False,
                            "message": f"suspendacct failed: {meta.get('reason', clean[:200])}"}
            except json.JSONDecodeError:
                pass

        after = self.account_state(username)
        if after is None or not after["suspended"]:
            return {"success": False,
                    "message": f"suspendacct reported no error but '{username}' is not suspended."}
        return {"success": True,
                "message": f"Suspended '{username}' on {self.host}. Reason: {reason}",
                "raw_response": clean}

    def delete_account(
        self,
        username: str,
        reason: str = "Test account cleanup",
        confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Permanently remove a cPanel account and all associated data.

        THIS IS IRREVERSIBLE. It destroys the home directory, mail stores and
        any databases belonging to the account. There is no undo.

        Intentionally exposed through the CLI only and never through the web
        dashboard or the /api/v1 endpoints, so that an unauthenticated or
        mistaken HTTP call cannot destroy a customer's hosting.

        Args:
            username: The cPanel/system account to remove.
            reason: Recorded in the server audit log via --reason.
            confirm: Must be True. Guards against accidental invocation.
        """
        if not confirm:
            return {
                "success": False,
                "message": "Refusing to delete without confirm=True. "
                           "This operation permanently destroys all account data."
            }

        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            return {"success": False, "message": f"Invalid cPanel username: {username!r}"}

        if not self.account_exists(username):
            return {
                "success": False,
                "message": f"Account '{username}' does not exist on {self.host}. Nothing to delete."
            }

        # whmapi1 removeacct destroys the account and all of its data. The
        # username and reason are quoted as discrete argv words; reason is
        # single-quoted via shlex so a quote in it cannot break out and be
        # read as shell syntax.
        cmd = (
            "whmapi1 --output=json removeacct "
            f"user={shlex.quote(username)} "
            f"reason={shlex.quote(reason)}"
        )
        sudo_stdin = None
        if self.ssh_user != "root":
            if self.ssh_password:
                cmd = "sudo -S -p '' " + cmd
                sudo_stdin = self.ssh_password + "\n"
            else:
                cmd = "sudo -n " + cmd

        try:
            code, stdout, stderr = self.ssh.execute(cmd, stdin_data=sudo_stdin)
        except Exception as e:
            return {"success": False, "message": f"SSH execution failed: {e}"}

        clean = "\n".join(l for l in stdout.splitlines() if not l.startswith("[sudo]")).strip()

        if code != 0:
            return {
                "success": False,
                "message": f"removeacct exited {code}: {stderr or clean or 'no output'}",
                "raw_response": clean,
            }

        # Confirm the account is really gone rather than trusting the exit code.
        if self.account_exists(username):
            return {
                "success": False,
                "message": f"removeacct reported success but account '{username}' still exists. "
                           "Investigate manually before retrying.",
                "raw_response": clean,
            }

        return {
            "success": True,
            "message": f"cPanel account '{username}' deleted from {self.host}. Reason: {reason}",
            "raw_response": clean,
        }

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
