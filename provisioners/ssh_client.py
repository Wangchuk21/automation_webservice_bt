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

    def _get_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": self.timeout
        }
        
        if self.key_path and os.path.isfile(self.key_path):
            connect_kwargs["key_filename"] = self.key_path
        elif self.password:
            connect_kwargs["password"] = self.password
        
        client.connect(**connect_kwargs)
        return client

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
                return True, "SSH connection verified successfully."
            return False, f"SSH returned error code {code}: {err or out}"
        except Exception as e:
            return False, f"SSH connection failed: {str(e)}"
