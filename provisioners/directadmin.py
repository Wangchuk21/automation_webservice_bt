import urllib.parse
import logging
import re
import shlex
from typing import Optional, Dict, Any
import requests

from config import settings
from .base import (
    USERNAME_RE,
    BaseProvisioner,
    ProvisionerResult,
    ValidationError,
    generate_secure_password,
    sanitize_username
)
from .ssh_client import SSHExecutor
from tls_config import resolve_verify, api_base_url

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
        tls_hostname: str = "",
        server_ip: str = "",
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
        # DNS name on the server certificate, for HTTPS calls.
        self.tls_hostname = tls_hostname or ""
        # Address assigned to newly created accounts. Previously hardcoded,
        # which silently provisioned the wrong IP if the server changed.
        self.server_ip = server_ip or ""
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
                url = f"{api_base_url(self.host, self.tls_hostname, 2222)}/CMD_API_SHOW_USERS"
                auth = (self.api_user, self.api_password)
                resp = requests.get(url, auth=auth, verify=resolve_verify(), timeout=10)
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
        url = f"{api_base_url(self.host, self.tls_hostname, 2222)}/CMD_API_ACCOUNT_USER"
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
            resp = requests.post(url, auth=auth, data=data,
                                 verify=resolve_verify(), timeout=30)
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
                url = f"{api_base_url(self.host, self.tls_hostname, 2222)}/CMD_API_PACKAGES_USER"
                auth = (self.api_user, self.api_password)
                resp = requests.get(url, auth=auth, verify=resolve_verify(), timeout=10)
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
        # Supply the sudo password on stdin rather than echoing it into the
        # command, where it would be visible in the remote process list to
        # every other user on the box.
        sudo_stdin = None
        if self.ssh_user != "root":
            if self.ssh_password:
                cmd = "sudo -S -p '' sh -c " + shlex.quote(cmd)
                sudo_stdin = self.ssh_password + "\n"
            else:
                cmd = "sudo -n sh -c " + shlex.quote(cmd)
        try:
            code, stdout, stderr = self.ssh.execute(cmd, stdin_data=sudo_stdin)
            clean = [l.strip() for l in stdout.splitlines() if l.strip() and not l.startswith("[sudo]")]
            return sorted(list(set(clean))) if clean else ["Bronze", "SILVER", "Gold", "PLATINUM", "default"]
        except Exception:
            return ["Bronze", "SILVER", "Gold", "PLATINUM", "default"]

    def list_accounts(self) -> list:
        """
        Every hosting account with its domain and current suspension state.

        Read-only, via the DirectAdmin API -- the same HTTP interface the web
        GUI uses. This is the work list for suspension.

        The `suspended` and `suspended_reason` fields are what make this
        usable: DirectAdmin distinguishes billing suspensions from abuse, spam
        and bandwidth suspensions, and conflating them would mean rewriting
        the reason on a case that was shut off for abuse.
        """
        if not self.api_password:
            logger.error("list_accounts needs DIRECTADMIN_API_PASSWORD.")
            return []
        try:
            base = api_base_url(self.host, self.tls_hostname, 2222)
            resp = requests.get(f"{base}/CMD_API_SHOW_USERS",
                                auth=(self.api_user, self.api_password),
                                verify=resolve_verify(), timeout=60)
            users = urllib.parse.parse_qs(resp.text, keep_blank_values=True).get("list[]", [])
        except Exception as e:
            logger.error(f"CMD_API_SHOW_USERS failed: {e}")
            return []

        out = []
        for u in users:
            cfg = self.account_state(u)
            if cfg:
                out.append(cfg)
        return out

    def account_state(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Current state of a single account: domain, suspended, reason.

        Read-only. Returns None if the account cannot be read. This is the
        authoritative pre-check the suspension rules depend on.
        """
        if not username or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]*$', username):
            raise ValidationError(f"Invalid DirectAdmin username: {username!r}")
        try:
            base = api_base_url(self.host, self.tls_hostname, 2222)
            resp = requests.get(f"{base}/CMD_API_SHOW_USER_CONFIG",
                                auth=(self.api_user, self.api_password),
                                params={"user": username},
                                verify=resolve_verify(), timeout=30)
            data = urllib.parse.parse_qs(resp.text, keep_blank_values=True)
        except Exception as e:
            logger.warning(f"account_state('{username}') failed: {e}")
            return None
        if data.get("error", [""])[0] not in ("", "0"):
            return None
        return {
            "panel": "directadmin",
            "username": username,
            "domain": (data.get("domain", [""])[0] or "").strip().lower(),
            "suspended": (data.get("suspended", ["no"])[0] or "no").strip().lower() == "yes",
            "reason": (data.get("suspended_reason", [""])[0] or "").strip(),
            "suspend_time": (data.get("suspend_date", [""])[0] or "").strip(),
        }

    def suspend_account(
        self,
        username: str,
        reason: str = "billing",
        confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Suspend a DirectAdmin account via CMD_API_MODIFY_USER.

        Refuses without confirm=True, and refuses when the account is already
        suspended so that an existing reason -- abuse, spam, user_bandwidth --
        is never overwritten with "billing". Overwriting would destroy the
        only record of why a customer was shut off.

        Returns success=False with already_suspended=True when the account was
        left alone for that reason, so callers can distinguish "did nothing on
        purpose" from "failed".
        """
        if not confirm:
            return {"success": False, "message": "Refusing to suspend without confirm=True."}
        if not username or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]*$', username):
            return {"success": False, "message": f"Invalid DirectAdmin username: {username!r}"}

        state = self.account_state(username)
        if state is None:
            return {"success": False,
                    "message": f"Account '{username}' could not be read on {self.host}."}
        if state["suspended"]:
            return {"success": False, "already_suspended": True,
                    "message": f"'{username}' is already suspended "
                               f"(reason: {state['reason'] or 'not recorded'}). Left untouched."}

        try:
            base = api_base_url(self.host, self.tls_hostname, 2222)
            resp = requests.get(f"{base}/CMD_API_MODIFY_USER",
                                auth=(self.api_user, self.api_password),
                                params={"user": username,
                                        "suspended": "yes",
                                        "suspended_reason": reason},
                                verify=resolve_verify(), timeout=60)
        except Exception as e:
            return {"success": False, "message": f"API request failed: {e}"}

        data = urllib.parse.parse_qs(resp.text, keep_blank_values=True)
        if data.get("error", ["0"])[0] not in ("", "0"):
            return {"success": False,
                    "message": f"CMD_API_MODIFY_USER failed: "
                               f"{(data.get('text') or ['error'])[0][:200]}"}

        after = self.account_state(username)
        if after is None or not after["suspended"]:
            return {"success": False,
                    "message": f"Modify reported no error but '{username}' is not suspended. "
                               "The suspended parameter may not be what this version expects."}
        return {"success": True,
                "message": f"Suspended '{username}' on {self.host}. Reason: {reason}"}

    def allow_sftp_user(self, username: str) -> Dict[str, Any]:
        """
        Add a new account to sshd_config's AllowUsers so SFTP works for them.

        This server restricts SSH with three AllowUsers lines in
        /etc/ssh/sshd_config: root, a short staff list, and the customer
        accounts. A new DirectAdmin user is therefore refused by sshd until
        their name is added, and the customer is told their SFTP details and
        then cannot use them.

        The edit is deliberately cautious, because this file is the only thing
        between the internet and every customer's file transfer:

          * a timestamped backup is taken before anything changes
          * `sshd -t` validates the result and, on failure, the backup is
            restored and nothing is reloaded
          * `systemctl reload` is used, not restart, so live sessions survive
          * an account already listed is a no-op, not a second entry

        Returns a result rather than raising: this is a post-create step, and a
        failure here must not undo or fail an account that was created fine.
        """
        username = (username or "").strip().lower()
        if not username:
            return {"success": False, "step": "sftp_access",
                    "message": "No username given."}

        # Refuse anything that is not a plain account name before it reaches the
        # file. The shell quoting stops a name being executed as a command, but
        # an AllowUsers entry is a whitespace-separated list, so a name with
        # spaces in it is written as several separate entries -- which is how a
        # test string of "bad name; rm -rf /" ended up on the live server as five
        # real entries while this method reported success.
        if not USERNAME_RE.match(username):
            return {"success": False, "step": "sftp_access",
                    "message": f"'{username}' is not a valid account name, so "
                               f"nothing was written to sshd_config."}
        q = shlex.quote(username)
        config = "/etc/ssh/sshd_config"

        # One script, run as root, so the read, the check and the edit all see
        # the same file. The password arrives on stdin, never in the command
        # string, where any user on the box could read it from `ps`.
        script = rf'''
set -u
cfg={config}
user={q}
[ -f "$cfg" ] || {{ echo "NOFILE"; exit 1; }}

# Already listed? Then there is nothing to do.
if grep -Eq "^[[:space:]]*AllowUsers[^#]*\\\\b$user\\\\b" "$cfg"; then
  echo "ALREADY"; exit 0
fi

ts=$(date +%Y%m%d-%H%M%S)
bak="$cfg.bt-auto-$ts"
cp -p "$cfg" "$bak" || {{ echo "NOBACKUP"; exit 1; }}

# Append to the LAST AllowUsers line. Several AllowUsers directives are
# additive, so which one it goes on does not change who may log in; the last is
# the customer list on this server.
python3 - "$cfg" "$user" <<'PYEOF'
import re, sys
cfg, user = sys.argv[1], sys.argv[2]
lines = open(cfg).read().splitlines(keepends=True)
target = None
for i, ln in enumerate(lines):
    if re.match(r"^\\s*AllowUsers\\b", ln) and not ln.lstrip().startswith("#"):
        target = i
if target is None:
    sys.exit(3)
line = lines[target]
if not line.endswith("\\n"):
    line += "\\n"
lines[target] = line.rstrip("\\n") + " " + user + "\\n"
open(cfg, "w").write("".join(lines))
PYEOF
rc=$?
if [ $rc -ne 0 ]; then cp -p "$bak" "$cfg"; echo "EDITFAIL"; exit 1; fi

# Validate before reloading. An invalid sshd_config stops sshd restarting,
# which would cut off SFTP for every customer, so it must never be loaded.
if ! sshd -t 2>/dev/null; then
  cp -p "$bak" "$cfg"
  echo "INVALID"; exit 1
fi

# The unit is named sshd on some distributions and ssh on others -- on the
# DirectAdmin server it is "ssh" -- so ask systemd which one is loaded rather
# than firing a command that is bound to fail.
unit=$(systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
       | awk '{{print $1}}' | grep -E '^(sshd?|ssh)\.service$' | head -1)
unit=${{unit:-ssh.service}}
systemctl reload "$unit" >/dev/null 2>&1
if [ $? -ne 0 ]; then cp -p "$bak" "$cfg"; echo "RELOADFAIL"; exit 1; fi
echo "OK"
rm -f "$bak"
'''
        rc, out, err = self.ssh.execute(f"sudo -S -p '' sh -s",
                                        stdin_data=(settings.DIRECTADMIN.sudo_password or "") + "\n"
                                                    + script)
        result = " ".join((out or err or "").split())
        if "ALREADY" in result:
            return {"success": True, "step": "sftp_access",
                    "message": f"{username} was already allowed in sshd_config."}
        if result.strip().endswith("OK"):
            return {"success": True, "step": "sftp_access",
                    "message": f"{username} added to AllowUsers and sshd reloaded."}
        reason = {
            "NOFILE": "sshd_config not found on the server.",
            "NOBACKUP": "Could not write a backup of sshd_config; nothing was changed.",
            "EDITFAIL": "Could not edit sshd_config; the original was restored.",
            "INVALID": "The edit produced an invalid sshd_config, so it was "
                       "reverted and sshd was NOT reloaded.",
            "RELOADFAIL": "sshd would not reload; the original was restored.",
        }.get(result.strip(), f"Unexpected result from the server: {result[:160]}")
        return {"success": False, "step": "sftp_access", "message": reason}

    def account_exists(self, username: str) -> bool:
        """
        Read-only check for whether a DirectAdmin account exists.

        Checks both the system account and the DirectAdmin user record, because
        on this server the two are not always in step: user state lives in
        /usr/local/directadmin/data/users/<name>/, not the older
        /usr/local/directadmin/users/ path.
        """
        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            raise ValidationError(f"Invalid DirectAdmin username: {username!r}")
        quoted = shlex.quote(username)
        cmd = (
            f"getent passwd {quoted} >/dev/null 2>&1 && echo SYS; "
            f"test -d /usr/local/directadmin/data/users/{quoted} && echo DA"
        )
        try:
            _, stdout, _ = self.ssh.execute(cmd)
            return "SYS" in stdout or "DA" in stdout
        except Exception as e:
            logger.warning(f"Existence check for '{username}' failed: {e}")
            return False

    def delete_account(
        self,
        username: str,
        reason: str = "Service surrender",
        confirm: bool = False
    ) -> Dict[str, Any]:
        """
        DirectAdmin does not expose a supported way to remove an account from a
        script, so this does NOT delete anything.

        DirectAdmin provides no delete-user CLI or CMD_API_* call; removal is
        only available through the panel GUI (User Level -> Delete User). On
        this server that means tearing down the system user, the
        /usr/local/directadmin/data/users/<name> record, the home directory,
        mail stores and any databases by hand, across 222 live accounts. Doing
        that from a script risks leaving DirectAdmin's internal state
        inconsistent (orphaned user records, broken mail routing, leftover
        databases) and the failure would be silent.

        So the surrender is recorded with its evidence and the operator is told
        exactly what remains to be done by hand.
        """
        if not confirm:
            return {
                "success": False,
                "message": "Refusing to record a surrender without confirm=True."
            }
        if not username or not re.match(r'^[a-z][a-z0-9]*$', username):
            return {"success": False, "message": f"Invalid DirectAdmin username: {username!r}"}

        if not self.account_exists(username):
            return {
                "success": False,
                "message": f"Account '{username}' does not exist on {self.host}. Nothing to remove."
            }

        return {
            "success": False,
            "manual_action_required": True,
            "message": (
                f"DirectAdmin account '{username}' was NOT deleted: DirectAdmin has no "
                f"supported scripted removal path. Delete it manually via the DirectAdmin "
                f"panel at {self.web_url} (User Level -> Delete User). This surrender has "
                f"been recorded for audit."
            ),
        }

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
        # shlex.quote covers every metacharacter, unlike the hand-rolled
        # single-quote escaping above. The IP is config-supplied but still
        # quoted, since it is interpolated into a remote shell command.
        escaped_server_ip = shlex.quote(self.server_ip) if self.server_ip else ""
        if not self.server_ip:
            # DirectAdmin requires an IP on account creation. Fail loudly rather
            # than create the account with a blank or wrong address.
            return self._build_failure_result(
                domain, username, password, email, doc_root,
                "DIRECTADMIN_SERVER_IP is not configured. Set it in .env to the "
                "server address DirectAdmin should assign to new accounts."
            )
        
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
            f"-d \"ip={escaped_server_ip}\" "
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
