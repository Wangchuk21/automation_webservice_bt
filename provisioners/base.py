import secrets
import string
import re
from typing import Dict, Any, Optional
from dataclasses import dataclass
from jinja2 import Template


# ---------------------------------------------------------------------------
# Input validation
#
# These are defence in depth. The provisioners shlex.quote() everything they
# interpolate into a remote shell, which is the actual guarantee; these
# validators reject obviously-malformed input early, before it reaches a
# provisioner or a live server.
# ---------------------------------------------------------------------------

# A domain label: alphanumeric, inner hyphens, 1-63 chars, at least two labels.
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)"
    r"(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)

# cPanel/DA usernames: lowercase alphanumerics, optionally with a single inner
# hyphen or underscore. Must start with a letter.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")

# Intentionally conservative: one @, no whitespace, no shell metacharacters.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}$")

# Plan/package names as exposed by whmapi1 listpkgs / DA package lists.
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ValidationError(ValueError):
    """Raised when a provisioning input fails validation."""


def validate_domain(domain: str) -> str:
    """Return a normalised domain, or raise ValidationError."""
    value = (domain or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(value):
        raise ValidationError(
            f"Invalid domain '{domain}'. Expected a real hostname such as "
            f"'clientdomain.bt' (letters, digits, hyphens and dots only)."
        )
    return value


def validate_username(username: str) -> str:
    """Return a normalised username, or raise ValidationError."""
    value = (username or "").strip().lower()
    if not USERNAME_RE.match(value):
        raise ValidationError(
            f"Invalid username '{username}'. Use 1-16 characters: a leading letter "
            f"followed by letters, digits, hyphen or underscore."
        )
    return value


def validate_email(email: str) -> str:
    """Return a trimmed email, or raise ValidationError."""
    value = (email or "").strip()
    if not EMAIL_RE.match(value):
        raise ValidationError(f"Invalid email address '{email}'.")
    return value


def validate_package(package: str) -> str:
    """Return a trimmed package/plan name, or raise ValidationError."""
    value = (package or "").strip()
    if not PACKAGE_RE.match(value):
        raise ValidationError(
            f"Invalid package/plan '{package}'. Use letters, digits, dot, "
            f"hyphen or underscore only."
        )
    return value

@dataclass
class ProvisionerResult:
    success: bool
    panel: str
    domain: str
    username: str
    password: str
    email: str
    web_url: str
    sftp_host: str
    sftp_port: int
    doc_root: str
    nameservers: str
    message: str
    handover_text: str = ""
    raw_response: Any = None


def generate_secure_password(length: int = 16) -> str:
    """Generate a strong password matching cPanel & DirectAdmin password strength requirements."""
    chars = string.ascii_letters + string.digits + "!@#$%&*-_=+"
    while True:
        password = ''.join(secrets.choice(chars) for _ in range(length))
        if (any(c.islower() for c in password)
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
                and any(c in "!@#$%&*-_=+" for c in password)):
            return password


def sanitize_username(domain: str, max_length: int = 8) -> str:
    """Derive a valid Linux/hosting username from domain name (alphanumeric, max length)."""
    # Remove TLD and special characters
    base = domain.split('.')[0]
    cleaned = re.sub(r'[^a-zA-Z0-9]', '', base).lower()
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "u" + cleaned
    # Ensure length between 4 and max_length (cPanel usually defaults to max 8 or 16 chars)
    if len(cleaned) < 4:
        cleaned = cleaned + secrets.token_hex(2)
    return cleaned[:max_length]


HANDOVER_TEXT_TEMPLATE = """Dear Customer,

We are pleased to inform you that your domain is successfully registered. We have also successfully hosted your website. The credentials for the same are shared below. Please note that the domain and web hosting should be renewed every year from the date of registration. If you wish to discontinue the service, please submit a surrender letter to the Bhutan Telecom office before the next billing date.

Your account has been created with the following details:

Username: {{ username }}
Password: {{ password }}

URL: {{ web_url if web_url.endswith('/') else web_url + '/' }}
If you wish to use FTP, please use the following:

Host: sftp://{{ domain }}/
Port: {{ sftp_port }}

Important Maintenance Notice:

To ensure the ongoing security and stability of your website, we strongly recommend that you:

Keep your website software updated. This includes the core application (e.g., WordPress, Joomla), all themes, and all plugins/extensions. Running outdated software is the most common cause of security breaches and service disruptions.

Perform regular backups. We advise you to regularly back up your website files and database. This ensures you can quickly restore your site in case of any unforeseen issues.

Use strong, unique credentials. Please use the strong password provided and update it periodically. Avoid using the same password for your hosting account, CMS admin panel, and FTP/SFTP access.

Neglecting these practices may expose your site to security vulnerabilities, malware, and unexpected downtime. Please be advised that, in accordance with our Acceptable Use Policy and to protect our infrastructure and other customers, we reserve the right to suspend hosting services or remove compromised website content if a severe security breach is detected that poses an active threat.


Thank you for choosing our service to meet your web hosting needs. Please don't hesitate to contact us at systems@bt.bt if you have any questions.

Regards,
Bhutan Telecom Ltd.
"""


HANDOVER_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.65;
      color: #334155;
      background-color: #f1f5f9;
      margin: 0;
      padding: 24px 12px;
    }
    .email-container {
      max-width: 640px;
      margin: 0 auto;
      background: #ffffff;
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid #e2e8f0;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.05);
    }
    .header {
      background: linear-gradient(135deg, #0f4c81 0%, #1e3a8a 100%);
      color: #ffffff;
      padding: 28px 24px;
      text-align: center;
    }
    .header h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }
    .header p {
      margin: 6px 0 0;
      font-size: 14px;
      opacity: 0.9;
    }
    .content {
      padding: 28px 24px;
    }
    p {
      margin: 0 0 16px;
      font-size: 14px;
      color: #334155;
    }
    .credentials-box {
      background: #f8fafc;
      border: 1px solid #cbd5e1;
      border-left: 4px solid #0f4c81;
      border-radius: 6px;
      padding: 18px 20px;
      margin: 20px 0;
    }
    .cred-row {
      display: flex;
      margin-bottom: 10px;
      font-size: 14px;
    }
    .cred-row:last-child {
      margin-bottom: 0;
    }
    .cred-label {
      width: 110px;
      font-weight: 600;
      color: #475569;
      flex-shrink: 0;
    }
    .cred-value {
      color: #0f172a;
      word-break: break-all;
    }
    .cred-value code {
      background: #e2e8f0;
      padding: 2px 6px;
      border-radius: 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      font-weight: 600;
      color: #0f172a;
    }
    .cred-value a {
      color: #0284c7;
      text-decoration: none;
      font-weight: 600;
    }
    .cred-value a:hover {
      text-decoration: underline;
    }
    .section-subtitle {
      font-weight: 600;
      font-size: 14px;
      color: #0f172a;
      margin: 16px 0 10px;
    }
    .notice-box {
      background: #fffbeb;
      border: 1px solid #fef3c7;
      border-left: 4px solid #f59e0b;
      border-radius: 6px;
      padding: 16px 18px;
      margin: 24px 0;
    }
    .notice-title {
      font-weight: 700;
      font-size: 14px;
      color: #92400e;
      margin-bottom: 8px;
    }
    .notice-box p, .notice-box li {
      font-size: 13px;
      color: #78350f;
      line-height: 1.55;
    }
    .notice-box ul {
      margin: 8px 0 12px 18px;
      padding: 0;
    }
    .notice-box li {
      margin-bottom: 6px;
    }
    .notice-disclaimer {
      font-size: 12px;
      color: #92400e;
      margin-top: 10px;
      line-height: 1.5;
    }
    .footer {
      background: #f8fafc;
      border-top: 1px solid #e2e8f0;
      padding: 18px 24px;
      font-size: 13px;
      color: #64748b;
    }
    .footer strong {
      color: #1e293b;
    }
  </style>
</head>
<body>
  <div class="email-container">
    <div class="header">
      <h1>Domain Registration & Web Hosting</h1>
      <p>Domain: <strong>{{ domain }}</strong></p>
    </div>
    <div class="content">
      <p>Dear Customer,</p>
      
      <p>We are pleased to inform you that your domain is successfully registered. We have also successfully hosted your website. The credentials for the same are shared below. Please note that the domain and web hosting should be renewed every year from the date of registration. If you wish to discontinue the service, please submit a surrender letter to the Bhutan Telecom office before the next billing date.</p>

      <p><strong>Your account has been created with the following details:</strong></p>

      <div class="credentials-box">
        <div class="cred-row">
          <div class="cred-label">Username:</div>
          <div class="cred-value"><strong>{{ username }}</strong></div>
        </div>
        <div class="cred-row">
          <div class="cred-label">Password:</div>
          <div class="cred-value"><code>{{ password }}</code></div>
        </div>
        <div class="cred-row" style="margin-top: 12px;">
          <div class="cred-label">URL:</div>
          <div class="cred-value"><a href="{{ web_url if web_url.endswith('/') else web_url + '/' }}" target="_blank">{{ web_url if web_url.endswith('/') else web_url + '/' }}</a></div>
        </div>

        <div class="section-subtitle">If you wish to use FTP, please use the following:</div>
        
        <div class="cred-row">
          <div class="cred-label">Host:</div>
          <div class="cred-value"><code>sftp://{{ domain }}/</code></div>
        </div>
        <div class="cred-row">
          <div class="cred-label">Port:</div>
          <div class="cred-value"><code>{{ sftp_port }}</code></div>
        </div>
      </div>

      <div class="notice-box">
        <div class="notice-title">Important Maintenance Notice:</div>
        <p>To ensure the ongoing security and stability of your website, we strongly recommend that you:</p>
        <ul>
          <li><strong>Keep your website software updated.</strong> This includes the core application (e.g., WordPress, Joomla), all themes, and all plugins/extensions. Running outdated software is the most common cause of security breaches and service disruptions.</li>
          <li><strong>Perform regular backups.</strong> We advise you to regularly back up your website files and database. This ensures you can quickly restore your site in case of any unforeseen issues.</li>
          <li><strong>Use strong, unique credentials.</strong> Please use the strong password provided and update it periodically. Avoid using the same password for your hosting account, CMS admin panel, and FTP/SFTP access.</li>
        </ul>
        <div class="notice-disclaimer">
          Neglecting these practices may expose your site to security vulnerabilities, malware, and unexpected downtime. Please be advised that, in accordance with our Acceptable Use Policy and to protect our infrastructure and other customers, we reserve the right to suspend hosting services or remove compromised website content if a severe security breach is detected that poses an active threat.
        </div>
      </div>

      <p>Thank you for choosing our service to meet your web hosting needs. Please don't hesitate to contact us at <a href="mailto:systems@bt.bt" style="color: #0284c7; text-decoration: none;">systems@bt.bt</a> if you have any questions.</p>
    </div>

    <div class="footer">
      Regards,<br>
      <strong>Bhutan Telecom Ltd.</strong>
    </div>
  </div>
</body>
</html>
"""


class BaseProvisioner:
    """Base class for hosting control panel provisioners."""
    
    def test_connection(self) -> Dict[str, Any]:
        raise NotImplementedError

    def create_account(
        self,
        domain: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        email: Optional[str] = None,
        package: Optional[str] = None,
        quota_mb: Optional[int] = None
    ) -> ProvisionerResult:
        raise NotImplementedError

    def format_handover(
        self,
        panel: str,
        domain: str,
        username: str,
        password: str,
        web_url: str,
        sftp_host: str,
        sftp_port: int,
        doc_root: str,
        nameservers: str
    ) -> str:
        template = Template(HANDOVER_TEXT_TEMPLATE)
        return template.render(
            panel=panel,
            domain=domain,
            username=username,
            password=password,
            web_url=web_url,
            sftp_host=sftp_host,
            sftp_port=sftp_port,
            doc_root=doc_root,
            nameservers=nameservers
        )

    def format_handover_html(
        self,
        panel: str,
        domain: str,
        username: str,
        password: str,
        web_url: str,
        sftp_host: str,
        sftp_port: int,
        doc_root: str,
        nameservers: str
    ) -> str:
        template = Template(HANDOVER_HTML_TEMPLATE)
        return template.render(
            panel=panel,
            domain=domain,
            username=username,
            password=password,
            web_url=web_url,
            sftp_host=sftp_host,
            sftp_port=sftp_port,
            doc_root=doc_root,
            nameservers=nameservers
        )
