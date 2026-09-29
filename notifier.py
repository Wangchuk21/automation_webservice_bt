import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
from typing import Tuple, Optional, List
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


def _deliver(msg, recipients: List[str]) -> None:
    """
    Hand a prepared message to the configured SMTP server.

    Extracted rather than inlined because a third kind of message was needed and
    copying the SSL/TLS/authentication dance again would have given three places
    to fix the same problem, one of which nobody would remember.
    """
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
            server.sendmail(settings.SMTP_FROM_EMAIL, recipients, msg.as_string())
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
            server.sendmail(settings.SMTP_FROM_EMAIL, recipients, msg.as_string())


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
        msg["Subject"] = f"Domain Registration & Web Hosting Credentials - {result.domain}"
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = result.email

        # Envelope recipients (To + CC)
        recipients = [result.email]
        if settings.SMTP_CC_EMAIL:
            msg["Cc"] = settings.SMTP_CC_EMAIL
            for cc_addr in [x.strip() for x in settings.SMTP_CC_EMAIL.split(",") if x.strip()]:
                if cc_addr not in recipients:
                    recipients.append(cc_addr)

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
        _deliver(msg, recipients)

        cc_info = f" (CC: {settings.SMTP_CC_EMAIL})" if settings.SMTP_CC_EMAIL else ""
        return True, f"Welcome email successfully sent to {result.email}{cc_info}."
    except Exception as e:
        logger.error(f"Failed to send email to {result.email}: {e}")
        return False, f"Failed to send email: {str(e)}"


def forwarding_text(domain: str, kind: str, target: str, observed) -> str:
    """
    The body BT already sends for a forwarded domain.

    It shows the operator's own check output rather than a paraphrase, so the
    customer is looking at the same evidence the page showed before the button
    was pressed. Kept in the existing house wording so a customer who has had
    two domains forwarded sees one consistent message from Bhutan Telecom.
    """
    lines = [f"Dear Customer,", "", f"Your domain {domain} has been successfully "
             f"forwarded as follows", ""]
    if kind == "nameserver":
        lines.append(f"host -t ns {domain}")
        for ns in (observed or []):
            lines.append(f"{domain} name server {ns}.")
    else:
        lines.append(f"host {domain}")
        for ip in (observed or []):
            lines.append(f"{domain} has address {ip}.")
    lines += ["", "Regards", ""]
    return "\n".join(lines)


def forwarding_subject(domain: str) -> str:
    return f"Your domain {domain} is now forwarded - {settings.SMTP_FROM_NAME}"


def send_forwarding_confirmation(
    domain: str,
    email: str,
    kind: str,
    target: str,
    observed=None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Tell a customer their domain has been forwarded.

    Only ever call this once the DNS check has confirmed it, because the email
    asserts something factual about the public internet. Sending it early tells a
    customer their domain is live when it is not, which is worse than telling
    them nothing. The caller is responsible for the gate; the wording here only
    states what has been verified.
    """
    if not settings.SMTP_ENABLED or not settings.SMTP_HOST:
        return False, "SMTP is not enabled in settings, so the customer was not notified."
    if not email or "@" not in email:
        return False, f"Invalid customer email address: '{email}'."
    if not domain:
        return False, "No domain given."

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = (subject or forwarding_subject(domain)).strip()
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = email
        recipients = [email]
        if settings.SMTP_CC_EMAIL:
            msg["Cc"] = settings.SMTP_CC_EMAIL
            for cc_addr in [x.strip() for x in settings.SMTP_CC_EMAIL.split(",") if x.strip()]:
                if cc_addr not in recipients:
                    recipients.append(cc_addr)

        # The operator's own wording if they supplied any, else the house text.
        custom = (body or "").strip()
        text = custom or forwarding_text(domain, kind, target, observed)
        msg.attach(MIMEText(text, "plain", "utf-8"))
        if custom:
            # Plain text only. Pairing a hand-written message with a generated
            # HTML alternative meant the customer read "it is now live and
            # resolves to ..." in one part of the email and whatever the
            # operator actually wrote in the other. Two versions of the same
            # message, saying different things.
            _deliver(msg, recipients)
            cc_info = f" (CC: {settings.SMTP_CC_EMAIL})" if settings.SMTP_CC_EMAIL else ""
            return True, f"Forwarding confirmation sent to {email}{cc_info}."
        html = (
            f"<p>Dear Customer,</p>"
            f"<p>Your domain <strong>{domain}</strong> has been registered with Bhutan "
            f"Telecom and the forwarding you requested is now complete.</p>"
            f"<p>"
            + (f"It is now delegated to <code>{', '.join(str(o) for o in (observed or []))}</code>. "
               f"Changes you make at <code>{target}</code> will now apply to this domain."
               if kind == "nameserver" else
               f"It is now live and resolves to "
               f"<code>{', '.join(str(o) for o in (observed or []))}</code>. Visitors who type "
               f"your domain will reach the site at that address.")
            + "</p>"
            f"<p>If you have any questions, please reply to this email.</p>"
            f"<p>Kind regards,<br>{settings.SMTP_FROM_NAME}<br>Bhutan Telecom</p>"
        )
        msg.attach(MIMEText(html, "html", "utf-8"))

        _deliver(msg, recipients)
        cc_info = f" (CC: {settings.SMTP_CC_EMAIL})" if settings.SMTP_CC_EMAIL else ""
        return True, f"Forwarding confirmation sent to {email}{cc_info}."
    except Exception as e:
        logger.error("Failed to send the forwarding confirmation for %s: %s", domain, e)
        return False, f"Failed to send email: {e}"
