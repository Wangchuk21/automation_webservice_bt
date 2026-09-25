import os
import warnings
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL 1.1.1+.*")
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

# Load .env if present
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

class ServerConfig:
    def __init__(self, prefix: str):
        self.host = os.getenv(f"{prefix}_SERVER_HOST", "localhost")
        self.web_port = int(os.getenv(f"{prefix}_WEB_PORT", "2083" if "CPANEL" in prefix else "2222"))
        self.web_url = os.getenv(f"{prefix}_WEB_URL", f"https://{self.host}:{self.web_port}")
        
        # SSH settings
        self.ssh_port = int(os.getenv(f"{prefix}_SSH_PORT", "22"))
        self.ssh_user = os.getenv(f"{prefix}_SSH_USER", "root")
        self.ssh_password = os.getenv(f"{prefix}_SSH_PASSWORD", "")
        self.ssh_key_path = os.getenv(f"{prefix}_SSH_KEY_PATH", "")
        
        # API settings
        if "CPANEL" in prefix:
            self.whm_user = os.getenv("CPANEL_WHM_USER", "root")
            self.whm_password = os.getenv("CPANEL_WHM_PASSWORD", "")
            self.whm_api_token = os.getenv("CPANEL_WHM_API_TOKEN", "")
            self.default_plan = os.getenv("CPANEL_DEFAULT_PLAN", "default")
            self.sftp_port = int(os.getenv("CPANEL_SFTP_PORT", str(self.ssh_port)))
            self.nameservers = os.getenv("CPANEL_NAMESERVERS", "ns1.bt.bt, ns2.bt.bt")
        else:
            self.api_user = os.getenv("DIRECTADMIN_API_USER", "admin")
            self.api_password = os.getenv("DIRECTADMIN_API_PASSWORD", "")
            self.default_package = os.getenv("DIRECTADMIN_DEFAULT_PACKAGE", "default")
            self.sftp_port = int(os.getenv("DIRECTADMIN_SFTP_PORT", "22"))
            self.nameservers = os.getenv("DIRECTADMIN_NAMESERVERS", "ns1.bt.bt, ns2.bt.bt")


class Config:
    PORT: int = int(os.getenv("PORT", "8000"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "automation-secret-bt")
    API_AUTH_TOKEN: Optional[str] = os.getenv("API_AUTH_TOKEN", None)
    
    # Servers
    CPANEL = ServerConfig("CPANEL")
    DIRECTADMIN = ServerConfig("DIRECTADMIN")
    
    # SMTP
    SMTP_ENABLED: bool = os.getenv("SMTP_ENABLED", "false").lower() in ("true", "1", "yes")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_NAME: str = os.getenv("SMTP_FROM_NAME", "Web Hosting Support")
    SMTP_FROM_EMAIL: str = os.getenv("SMTP_FROM_EMAIL", "support@bt.bt")

settings = Config()
