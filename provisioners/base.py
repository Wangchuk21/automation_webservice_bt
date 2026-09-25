import secrets
import string
import re
from typing import Dict, Any, Optional
from dataclasses import dataclass
from jinja2 import Template

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


HANDOVER_TEXT_TEMPLATE = """
================================================================================
                    WEBSITE HOSTING ACCOUNT CREDENTIALS
================================================================================
Dear Customer,

Your shared web hosting account for '{{ domain }}' has been successfully provisioned.
Below are your access credentials to manage your website and upload your web files.

--------------------------------------------------------------------------------
1. WEB CONTROL PANEL ACCESS (Browser Web UI)
--------------------------------------------------------------------------------
Control Panel URL : {{ web_url }}
Username          : {{ username }}
Password          : {{ password }}

Use the Web UI to manage your domains, databases (MySQL), email accounts, 
SSL certificates, and file manager directly in your browser.

--------------------------------------------------------------------------------
2. SFTP FILE UPLOAD ACCESS (Secure FTP / FileZilla / WinSCP / Cyberduck)
--------------------------------------------------------------------------------
Protocol          : SFTP (SSH File Transfer Protocol)
SFTP Host/Server  : {{ sftp_host }}
SFTP Port         : {{ sftp_port }}
Username          : {{ username }}
Password          : {{ password }}
Web Document Root : {{ doc_root }}

* Upload your website files (HTML, PHP, assets) into the '{{ doc_root }}' directory.
* Files placed outside this directory will not be visible on the web.

--------------------------------------------------------------------------------
3. DOMAIN STATUS
--------------------------------------------------------------------------------
Domain Name       : {{ domain }}
DNS Management    : Managed by Technical Support (No customer action required).
Routing Status    : Active and connected to your hosting root directory.

--------------------------------------------------------------------------------
Need help or support?
Contact: support@druknet.bt
================================================================================
"""


HANDOVER_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #2d3748; background-color: #f7fafc; padding: 20px; }
    .card { max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; border: 1px solid #e2e8f0; }
    .header { background: linear-gradient(135deg, #1e3a8a, #2563eb); color: white; padding: 24px; text-align: center; }
    .header h2 { margin: 0; font-size: 24px; font-weight: 700; }
    .header p { margin: 6px 0 0; opacity: 0.9; font-size: 14px; }
    .content { padding: 28px; }
    .section { margin-bottom: 24px; }
    .section-title { font-size: 15px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #4b5563; margin-bottom: 12px; border-bottom: 2px solid #edf2f7; padding-bottom: 6px; }
    .grid { display: grid; grid-template-columns: 140px 1fr; gap: 8px 12px; font-size: 14px; }
    .label { font-weight: 600; color: #64748b; }
    .value { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #0f172a; word-break: break-all; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; background: #e0f2fe; color: #0369a1; font-weight: 600; font-size: 12px; }
    .highlight { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; }
    .footer { background: #f8fafc; padding: 16px; text-align: center; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h2>Web Hosting Provisioned</h2>
      <p>Domain: <strong>{{ domain }}</strong> ({{ panel|upper }})</p>
    </div>
    <div class="content">
      <div class="section">
        <div class="section-title">1. Web UI Control Panel</div>
        <div class="highlight grid">
          <div class="label">Panel URL:</div>
          <div class="value"><a href="{{ web_url }}" target="_blank" style="color: #2563eb; text-decoration: none;">{{ web_url }}</a></div>
          <div class="label">Username:</div>
          <div class="value"><strong>{{ username }}</strong></div>
          <div class="label">Password:</div>
          <div class="value"><code>{{ password }}</code></div>
        </div>
      </div>

      <div class="section">
        <div class="section-title">2. SFTP Upload Access</div>
        <div class="highlight grid">
          <div class="label">Protocol:</div>
          <div class="value"><span class="badge">SFTP (SSH File Transfer)</span></div>
          <div class="label">Server Host:</div>
          <div class="value">{{ sftp_host }}</div>
          <div class="label">Port:</div>
          <div class="value">{{ sftp_port }}</div>
          <div class="label">Username:</div>
          <div class="value">{{ username }}</div>
          <div class="label">Password:</div>
          <div class="value"><code>{{ password }}</code></div>
          <div class="label">Doc Root:</div>
          <div class="value"><strong>{{ doc_root }}</strong></div>
        </div>
        <p style="font-size: 13px; color: #64748b; margin-top: 8px;">Upload all public HTML, PHP, and asset files into the <strong>{{ doc_root }}</strong> directory.</p>
      </div>

      <div class="section">
        <div class="section-title">3. Domain & DNS Status</div>
        <div class="highlight grid">
          <div class="label">Domain:</div>
          <div class="value"><strong>{{ domain }}</strong></div>
          <div class="label">DNS Routing:</div>
          <div class="value"><span class="badge" style="background: #ecfdf5; color: #047857;">Managed by Technical Support</span></div>
        </div>
      </div>
    </div>
    <div class="footer">
      Shared Web Hosting Automation &bull; Delivered for {{ domain }}
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
