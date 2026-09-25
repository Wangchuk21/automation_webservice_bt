import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
from typing import Tuple, Optional
from config import settings
from provisioners.base import ProvisionerResult

logger = logging.getLogger(__name__)


def test_smtp_connection(recipient: Optional[str] = None) -> Tuple[bool, str]:
    """
    Tests connectivity and authentication with the configured SMTP server.
    Optionally sends a test email to the recipient.
    """
    if not settings.SMTP_ENABLED:
        return False, "SMTP is disabled (SMTP_ENABLED=false)."
    if not settings.SMTP_HOST:
        return False, "SMTP_HOST is not configured."

    use_ssl = settings.SMTP_SSL or settings.SMTP_PORT == 465

    try:
        if use_ssl:
            try:
                ctx = ssl.create_default_context()
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=ctx, timeout=15)
            except Exception:
                ctx = ssl._create_unverified_context()
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=ctx, timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                pass

        with server:
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

            if recipient:
                msg = MIMEText("This is a test email from the Bhutan Telecom Web Hosting Automation system.", "plain", "utf-8")
                msg["Subject"] = "Test Email - Web Hosting Automation"
                msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
                msg["To"] = recipient
                server.sendmail(settings.SMTP_FROM_EMAIL, [recipient], msg.as_string())
                return True, f"SMTP connection and login successful. Test email sent to {recipient}."

        return True, "SMTP connection and authentication successful."
    except Exception as e:
        logger.error(f"SMTP test failed: {e}")
        return False, f"SMTP test failed: {str(e)}"


def send_customer_welcome_email(result: ProvisionerResult) -> Tuple[bool, str]:
    """
    Sends customer onboarding email containing Web UI & SFTP credentials.
    """
    if not settings.SMTP_ENABLED or not settings.SMTP_HOST:
        return False, "SMTP is not enabled in settings. Handover text generated for manual copy-paste."

    if not result.email or "@" not in result.email:
        return False, f"Invalid customer email address: '{result.email}'."

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Your Web Hosting Credentials - {result.domain}"
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = result.email

        # Attach Plain Text and HTML
        text_part = MIMEText(result.handover_text, "plain", "utf-8")
        msg.attach(text_part)

        # Generate HTML from provisioner template
        from provisioners.base import BaseProvisioner
        base_prov = BaseProvisioner()
        html_body = base_prov.format_handover_html(
            panel=result.panel,
            domain=result.domain,
            username=result.username,
            password=result.password,
            web_url=result.web_url,
            sftp_host=result.sftp_host,
            sftp_port=result.sftp_port,
            doc_root=result.doc_root,
            nameservers=result.nameservers
        )
        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

        # Send via SMTP
        use_ssl = settings.SMTP_SSL or settings.SMTP_PORT == 465
        if use_ssl:
            try:
                ctx = ssl.create_default_context()
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=ctx, timeout=15)
            except Exception:
                ctx = ssl._create_unverified_context()
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=ctx, timeout=15)
            with server:
                server.ehlo()
                if settings.SMTP_USER and settings.SMTP_PASSWORD:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM_EMAIL, [result.email], msg.as_string())
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except Exception:
                    pass
                if settings.SMTP_USER and settings.SMTP_PASSWORD:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM_EMAIL, [result.email], msg.as_string())

        return True, f"Welcome email successfully sent to {result.email}."
    except Exception as e:
        logger.error(f"Failed to send email to {result.email}: {e}")
        return False, f"Failed to send email: {str(e)}"
