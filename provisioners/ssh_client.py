import os
import paramiko
from typing import Tuple, Optional

class SSHExecutor:
    """Helper to run commands on remote hosting server via SSH."""
    def __init__(
        self,
        host: str,
        port: int = 22,
        user: str = "root",
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        timeout: int = 25
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self.timeout = timeout
        # Set to "key", "password" or "none" after a successful connect.
        self.last_auth_method: Optional[str] = None

    def _auth_attempts(self) -> list:
        """
        Build the ordered list of authentication methods to try.

        A key file existing on disk does NOT mean the remote host trusts it, so
        the key must not permanently shadow a working password. Previously this
        was an either/or: if the key file existed the password was silently
        discarded, which broke every server where the key was not installed in
        authorized_keys.
        """
        base = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": self.timeout,
        }
        attempts = []
        if self.key_path and os.path.isfile(self.key_path):
            attempts.append(("key", dict(base, key_filename=self.key_path)))
        if self.password:
            attempts.append(("password", dict(base, password=self.password)))
        # Preserve previous behaviour when no credentials are configured at all.
        if not attempts:
            attempts.append(("none", dict(base)))
        return attempts

    def _get_client(self) -> paramiko.SSHClient:
        attempts = self._auth_attempts()
        last_error = None

        for method, kwargs in attempts:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**kwargs)
                self.last_auth_method = method
                return client
            except paramiko.AuthenticationException as e:
                last_error = e
                client.close()
            except Exception:
                # Non-auth failures (DNS, refused, timeout) will not be fixed by
                # switching method, so surface them immediately.
                client.close()
                raise

        available = ", ".join(m for m, _ in attempts)
        raise paramiko.AuthenticationException(
            f"SSH authentication failed for {self.user}@{self.host}:{self.port} "
            f"using all configured method(s) [{available}]. "
            f"If a private key is configured, confirm its public key is present in "
            f"the remote account's authorized_keys, or clear the key path to force "
            f"password authentication. Last error: {last_error}"
        )

    def execute(self, command: str) -> Tuple[int, str, str]:
        """Execute command and return (exit_code, stdout, stderr)."""
        client = None
        try:
            client = self._get_client()
            stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            out_str = stdout.read().decode('utf-8', errors='replace').strip()
            err_str = stderr.read().decode('utf-8', errors='replace').strip()
            return exit_code, out_str, err_str
        finally:
            if client:
                client.close()

    def test_connection(self) -> Tuple[bool, str]:
        """Test if SSH connection succeeds."""
        try:
            code, out, err = self.execute("echo 'SSH_CONNECTION_SUCCESS'")
            if code == 0 and "SSH_CONNECTION_SUCCESS" in out:
                method = self.last_auth_method or "unknown"
                return True, f"SSH connection verified successfully (auth: {method})."
            return False, f"SSH returned error code {code}: {err or out}"
        except Exception as e:
            return False, f"SSH connection failed: {str(e)}"
