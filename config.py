import os
import warnings
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Silence only the specific "urllib3 v2 needs OpenSSL 1.1.1+" environment
# notice. A blanket UserWarning filter for urllib3 would also hide genuine
# certificate problems, which is exactly what must stay visible.
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL 1.1.1+.*")

# Load .env if present
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


def _unescape_dollar_values(path: Path) -> None:
    """
    Collapse the "$$" escape to "$" for values this process loaded from the
    .env FILE.

    Why: Docker Compose treats "$NAME" in a value as a variable reference and
    substitutes an empty string, so a password containing "$" arrives in the
    container silently truncated. Compose's documented escape for a literal "$"
    is "$$", so secrets stored for Compose are written escaped. python-dotenv
    does no such unescaping, so the host has to undo it here.

    Scoped deliberately to keys that came from the .env file:
      - In Docker the values arrive via env_file already unescaped by Compose,
        and .env is not in the image, so this is a no-op there.
      - A plain single "$" (not doubled) is left exactly as written, so a value
        that legitimately contains "$@" is untouched.
    """
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in os.environ or "$$" not in os.environ[key]:
            continue
        os.environ[key] = os.environ[key].replace("$$", "$")


_unescape_dollar_values(env_path)


class ServerConfig:
    def __init__(self, prefix: str):
        self.host = os.getenv(f"{prefix}_SERVER_HOST", "localhost")
        self.web_port = int(os.getenv(f"{prefix}_WEB_PORT", "2083" if "CPANEL" in prefix else "2222"))
        self.web_url = os.getenv(f"{prefix}_WEB_URL", f"https://{self.host}:{self.web_port}")

        # Hostname used for HTTPS/TLS calls and SNI when the server is reached
        # by IP. The cPanel and DirectAdmin certificates carry DNS SANs only
        # (CN=thimpchu.druknet.bt, CN=yongnay.druknet.bt), so verifying a
        # connection opened to the bare IP fails hostname matching even though
        # the certificate chain itself is trusted. Leave empty when SERVER_HOST
        # is already a hostname.
        self.tls_hostname = os.getenv(f"{prefix}_TLS_HOSTNAME", "")
        # Address to assign to newly created accounts on this server.
        self.server_ip = os.getenv(f"{prefix}_SERVER_IP", "")
        
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

    # --- TLS ---
    # Verify server certificates on every HTTPS call (WHM, DirectAdmin,
    # nic.bt.bt). All of these servers present publicly-trusted Let's Encrypt /
    # Sectigo certificates, so verification works. Set false only if you must,
    # and prefer pointing TLS_CA_BUNDLE at a private CA instead.
    TLS_VERIFY: bool = os.getenv("TLS_VERIFY", "true").lower() in ("true", "1", "yes")
    # Optional path to a CA bundle (e.g. an internal root) for private CAs.
    TLS_CA_BUNDLE: str = os.getenv("TLS_CA_BUNDLE", "")
    
    # Servers
    CPANEL = ServerConfig("CPANEL")
    DIRECTADMIN = ServerConfig("DIRECTADMIN")
    
    # SMTP
    SMTP_ENABLED: bool = os.getenv("SMTP_ENABLED", "false").lower() in ("true", "1", "yes")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_SSL: bool = os.getenv("SMTP_SSL", "false").lower() in ("true", "1", "yes")
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_NAME: str = os.getenv("SMTP_FROM_NAME", "Web Hosting Support")
    SMTP_FROM_EMAIL: str = os.getenv("SMTP_FROM_EMAIL", "support@bt.bt")
    SMTP_CC_EMAIL: str = os.getenv("SMTP_CC_EMAIL", "systems@bt.bt")

    # NIC / WHOIS Registry (nic.bt.bt)
    NIC_ENABLED: bool = os.getenv("NIC_ENABLED", "true").lower() in ("true", "1", "yes")
    NIC_URL: str = os.getenv("NIC_URL", "https://nic.bt.bt")
    NIC_USER: str = os.getenv("NIC_USER", "admin@bt.bt")
    NIC_PASSWORD: str = os.getenv("NIC_PASSWORD", "")

    # --- nic.bt.bt registry field defaults ---
    # The registry's domain form has 23 required fields, and it follows the
    # international convention -- confirmed against real records on the portal,
    # where tech_email was identical to the registrant's own email on every
    # record sampled:
    #
    #   tech_*    = the DOMAIN OWNER. The technical contact is the customer,
    #               not the registrar.
    #   billing_* = the REGISTRAR or its billing agent. Those same records
    #               carried the agent's details (MarkMonitor / CCOPs) here, not
    #               the customer's.
    #
    # So Bhutan Telecom's own details belong in the BILLING block and the
    # technical block is derived from the customer. Having it the other way
    # round publishes BT as the technical contact for every customer domain,
    # which is wrong for any domain whose owner manages their own DNS.
    #
    # These values are still unconfirmed with the registry; they are
    # configurable, and the dashboard shows every field before submitting.
    NIC_REGISTRAR: str = os.getenv("NIC_REGISTRAR", "DrukNet")
    NIC_BILLING_NAME: str = os.getenv("NIC_BILLING_NAME", "Bhutan Telecom Ltd")
    NIC_BILLING_ADDRESS: str = os.getenv("NIC_BILLING_ADDRESS", "Thimphu, Bhutan")
    NIC_BILLING_CONTACT: str = os.getenv("NIC_BILLING_CONTACT", "+975-2-343434")
    NIC_BILLING_FAX: str = os.getenv("NIC_BILLING_FAX", "-")
    NIC_BILLING_COUNTRY: str = os.getenv("NIC_BILLING_COUNTRY", "BT")
    NIC_BILLING_EMAIL: str = os.getenv("NIC_BILLING_EMAIL", "systems@bt.bt")
    # Placeholder used where the registry marks a field required but there is
    # genuinely no value. Real records on the registry use "-" for these.
    NIC_PLACEHOLDER: str = os.getenv("NIC_PLACEHOLDER", "-")
    NIC_DEFAULT_COUNTRY: str = os.getenv("NIC_DEFAULT_COUNTRY", "BT")

    # --- Service surrender (termination) ---
    # A surrender destroys customer data, so it is deliberately harder to
    # trigger than account creation: the scanned surrender letter is required
    # as evidence, and every action is written to an append-only audit log.
    SURRENDER_UPLOAD_DIR: str = os.getenv("SURRENDER_UPLOAD_DIR", "./data/surrenders")
    SURRENDER_AUDIT_LOG: str = os.getenv("SURRENDER_AUDIT_LOG", "./data/surrenders/audit.jsonl")
    SURRENDER_MAX_UPLOAD_MB: int = int(os.getenv("SURRENDER_MAX_UPLOAD_MB", "10"))
    SURRENDER_REQUIRE_EVIDENCE: bool = os.getenv("SURRENDER_REQUIRE_EVIDENCE", "true").lower() in ("true", "1", "yes")
    # Accepted evidence formats. The scanner's output is a PDF or a photo of
    # the letter, so JPEG is allowed alongside it.
    SURRENDER_ALLOWED_EXTENSIONS: str = os.getenv("SURRENDER_ALLOWED_EXTENSIONS", ".pdf,.jpg,.jpeg")

    # --- Automated suspension ---
    # Kept separate from the surrender audit: the record shapes differ, and
    # mixing them in one file would make both harder to read.
    SUSPENSION_AUDIT_LOG: str = os.getenv("SUSPENSION_AUDIT_LOG", "./suspension/audit.jsonl")

    # --- Ericsson BSCS / CBiO CX (read-only reference) ---
    # STRICTLY READ-ONLY. We have no rights to write to BSCS, so this client
    # deliberately exposes no activate/deactivate/contract-modifying method.
    # It is a lookup aid: confirm the customer exists, read their contract and
    # billing state, and use that as reference when provisioning or suspending.
    # Anything that would change a contract or a VAS package belongs in the
    # portal, performed by an authorised operator.
    BSCS_ENABLED: bool = os.getenv("BSCS_ENABLED", "false").lower() in ("true", "1", "yes")
    BSCS_BASE_URL: str = os.getenv("BSCS_BASE_URL", "")
    BSCS_USERNAME: str = os.getenv("BSCS_USERNAME", "")
    BSCS_PASSWORD: str = os.getenv("BSCS_PASSWORD", "")
    # The CX portal is slow and issues several chained requests per operation,
    # so this is per-request, not per-operation.
    BSCS_TIMEOUT: int = int(os.getenv("BSCS_TIMEOUT", "45"))
    # The portal is served over plain HTTP on the internal network, so there is
    # no certificate to verify. Kept configurable for a future HTTPS rollout.
    BSCS_VERIFY_SSL: bool = os.getenv("BSCS_VERIFY_SSL", "true").lower() in ("true", "1", "yes")
    # Cap on rows returned by a search, so a broad query cannot pull the whole
    # customer table into memory (and into a log).
    BSCS_MAX_RESULTS: int = int(os.getenv("BSCS_MAX_RESULTS", "25"))

settings = Config()
