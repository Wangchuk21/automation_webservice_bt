"""
Turn on a real certificate for a newly hosted domain, and keep it renewed.

Why this is its own module and not another post-create step
----------------------------------------------------------

IPv6 and SFTP are safe to attempt unconditionally: both either work or they do
not, and a failure leaves an account that is otherwise fine. A certificate is
not like that. Let's Encrypt will only issue for a domain that resolves to the
server issuing it, with port 80 answering. Attempting it anyway produces a
failed validation, and repeated failures burn the issuance rate limit -- five
failed authorisations per hostname per account, per week. That limit is slow to
recover from and it is shared, so a bad guess here can lock out legitimate
customers for days.

So the certificate is gated on a DNS check that has actually passed. The domain
pointing somewhere else is not a failure, it is simply "not yet", and the
account is still created.

Renewal
-------

Neither panel wants renewal written by hand.

DirectAdmin's Let's Encrypt integration renews on its own schedule, so issuing is
all that is needed. cPanel's AutoSSL plugin does the same once installed: it
queues a domain, validates it and installs the certificate, then renews it
before expiry. Neither needs a cron job from us, and adding one would be a
second, worse mechanism that silently stops working when nobody is watching.

Platform state, as found
------------------------

Verified against the live servers, not assumed:

- yongnay (DirectAdmin 1.711): works, with no licence. I first read
  ``acmeEnabled: false`` as a licence gate and was wrong. It is a per-domain
  setting -- ``acme_enabled`` in the domain's own .conf, settable through
  ``PUT /api/domain-tls/{domain}/acme-config``. Domains with it on hold live
  Let's Encrypt certificates, with rotated ACME logs showing renewals. The code
  now enables the domain before asking for a certificate.

- thimpchu (cPanel 138.0): the AutoSSL plugin is not installed and no
  Let's Encrypt provider RPM is present, so there is nothing to call. Until
  AutoSSL is installed, this reports why and stops.

Neither is a code defect, and neither is worked around. A self-signed
certificate, or a certificate from a CA that does not need the domain to
resolve, would be worse than no certificate: it would look secure in a browser
and would not be, or it would break on a domain whose DNS is still being set up.
"""
import logging
import shlex
from typing import Any, Dict, List, Optional

import requests

from config import settings
from dns_check import check_domain
from tls_config import api_base_url, resolve_verify

logger = logging.getLogger(__name__)

# Outcomes. Distinguishing "we chose not to" from "we tried and it failed"
# matters, because only one of them needs a human and the other does not.
ISSUED = "issued"
SKIPPED = "skipped"
FAILED = "failed"
UNSUPPORTED = "unsupported"

# DirectAdmin error codes, from the server's own swagger spec rather than from
# documentation that may be a version behind. Each one needs a different answer.
DA_ACME_DISABLED = "DOMAIN_ACME_IS_DISABLED"
DA_LICENSE_OVERUSED = "LICENSE_OVERUSED"
DA_RATE_LIMIT = "RATELIMIT_REACHED"
DA_IN_PROGRESS = "DOMAIN_ACME_ALREADY_IN_PROGRESS"
DA_UNAUTHORIZED = "UNAUTHORIZED"

# Codes that mean something is wrong, as opposed to the platform declining.
ssl_failed_codes = (DA_UNAUTHORIZED,)

_REASONS = {
    DA_ACME_DISABLED: (
        "DirectAdmin reports Let's Encrypt is switched off for this domain. "
        "This is a per-domain setting in DirectAdmin, not a server-wide or "
        "licence problem: the server has letsencrypt=1 and domains that have it "
        "enabled receive certificates without any licence configured."
    ),
    DA_LICENSE_OVERUSED: (
        "The DirectAdmin licence is overused, so certificate provisioning is "
        "refused until it is renewed."
    ),
    DA_RATE_LIMIT: (
        "Let's Encrypt's rate limit for this domain has been reached. This is "
        "shared across the server, so do not retry repeatedly."
    ),
    DA_IN_PROGRESS: (
        "A certificate request for this domain is already running. Wait for it to "
        "finish before trying again."
    ),
    # Not the same class of problem as the rest. The others are about the
    # platform; this one means the account being impersonated does not exist, or
    # the API credentials are wrong -- so it needs saying plainly rather than
    # being folded into "not supported".
    DA_UNAUTHORIZED: (
        "DirectAdmin rejected the request as unauthorised. The hosting account "
        "most likely does not exist on this server under that name, or the API "
        "credentials are wrong."
    ),
}


def _result(step: str, status: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"step": step, "success": status == ISSUED, "status": status,
            "message": message, **extra}


def dns_gate(panel: str, domain: str) -> Dict[str, Any]:
    """
    Whether a certificate can even be attempted for this domain right now.

    Returns a decision with a reason, never a bare yes or no, because "no" with
    no explanation is what makes an operator press a button repeatedly.
    """
    if not domain:
        return {"allowed": False, "reason": "No domain was given, so there is "
                                           "nothing a certificate could cover."}
    if not settings.SSL_REQUIRE_DNS_MAPPING:
        return {"allowed": True, "reason": "The DNS check is switched off, so a "
                                           "certificate will be attempted "
                                           "regardless of where the domain points."}
    result = check_domain(domain, panel)
    if result.get("status") == "mapped":
        return {"allowed": True, "reason": "", "check": result}
    return {
        "allowed": False,
        "reason": (f"{domain} does not point at this server yet, so Let's Encrypt "
                   f"would refuse to issue. {result.get('message', '')}".strip()),
        "check": result,
    }


def _da_reason(detail: Any) -> Optional[str]:
    """Pull the documented code out of a DirectAdmin error body."""
    if isinstance(detail, dict):
        for value in detail.values():
            if isinstance(value, str) and value in _REASONS:
                return value
        if detail.get("type") in _REASONS:
            return detail["type"]
    if isinstance(detail, str) and detail in _REASONS:
        return detail
    return None


# The hostnames DirectAdmin offers in a certificate. It includes a standard set
# of subdomains whether or not they exist, and every one of them has to pass
# validation -- so ftp.dash.bt, which resolves nowhere, fails the whole order.
# This matches the skip list samchar.bt already carries on this server.
ACME_CANDIDATE_NAMES = ("www", "mail", "ftp", "pop", "smtp", "webmail", "autodiscover")

# The fields DirectAdmin's acme-config requires in full. A partial PUT is
# refused with "unknown acme key type", so this has to be sent whole.
_ACME_DEFAULTS = {
    "provider": "letsencrypt",
    "externalAccountKeyID": "",
    "externalAccountHMAC": "",
    "keyType": "ec256",
    "preferWildcard": True,
    "dnsProvider": "",
    "dnsEnvironment": {},
    "skipDNSNames": [],
}


def unresolved_names(domain: str) -> List[str]:
    """
    Which of the names DirectAdmin would ask for do not actually resolve.

    Returned as a skip list so they are left out of the order. A name that
    resolves nowhere fails validation, and one failed name fails the whole
    certificate rather than just its own part -- so a customer with a perfectly
    good domain and no ftp subdomain could get no certificate at all.
    """
    from dns_check import resolve_ips

    missing: List[str] = []
    for sub in ACME_CANDIDATE_NAMES:
        name = f"{sub}.{domain}"
        try:
            if not resolve_ips(name):
                missing.append(name)
        except Exception:  # noqa: BLE001 - a lookup failure counts as unresolved
            missing.append(name)
    return missing


def enable_da_acme(domain: str, username: str) -> Optional[str]:
    """
    Turn Let's Encrypt on for one DirectAdmin domain, if it is not already.

    This is a per-domain switch in DirectAdmin, not a server-wide one, and a
    domain created by the provisioning flow will not have it. It is read first
    so a domain that is already enabled is never written to.

    Returns an error message, or None when the domain is enabled.
    """
    base = api_base_url(settings.DIRECTADMIN.host,
                        settings.DIRECTADMIN.tls_hostname, 2222)
    who = (f"{settings.DIRECTADMIN.api_user}|{username}" if username
           else settings.DIRECTADMIN.api_user)
    auth = (who, settings.DIRECTADMIN.api_password)
    url = f"{base}/api/domain-tls/{domain}/acme-config"

    try:
        current = requests.get(url, auth=auth, verify=resolve_verify(), timeout=45)
        if current.status_code == 200 and current.json().get("enabled"):
            return None
        # Preserve whatever the domain already had, and only change `enabled`
        # and the skip list. An operator who has deliberately added names there
        # keeps them.
        existing = current.json() if current.status_code == 200 else {}
        wanted = unresolved_names(domain)
        skip = list(existing.get("skipDNSNames") or [])
        for name in wanted:
            if name not in skip:
                skip.append(name)
        body = {**_ACME_DEFAULTS, **existing, "enabled": True, "skipDNSNames": skip}
        resp = requests.put(url, auth=auth, json=body, verify=resolve_verify(),
                            timeout=45)
    except Exception as e:  # noqa: BLE001
        return f"Could not reach DirectAdmin to enable Let's Encrypt: {e}"

    if resp.status_code in (200, 204):
        return None
    return (f"DirectAdmin refused to enable Let's Encrypt for {domain} "
            f"(HTTP {resp.status_code}): {(resp.text or '')[:160]}")


def enable_directadmin(domain: str, username: str = "") -> Dict[str, Any]:
    """
    Ask DirectAdmin to issue a Let's Encrypt certificate for a domain.

    Endpoint taken from the server's own /static/swagger.json, not from
    documentation that may be a version behind: POST
    /api/domain-tls/{domain}/provision-certs, returning certsFulfilled,
    dnsNamesFailedChallenge, dnsNamesFailedCAA and dnsNamesSkipped.

    The dry-run endpoint is deliberately not used here. It exists to preview, and
    this path is only reached once the DNS check has established that the domain
    already resolves to this server.
    """
    # Enabled first: a domain the provisioning flow just created will not have
    # Let's Encrypt turned on, and the panel answers DOMAIN_ACME_IS_DISABLED
    # rather than issuing. Read before writing, so a domain that already has it
    # is left alone.
    acme_error = enable_da_acme(domain, username)
    if acme_error:
        return _result("ssl", FAILED, acme_error)

    base = api_base_url(settings.DIRECTADMIN.host, settings.DIRECTADMIN.tls_hostname, 2222)
    # admin|user, DirectAdmin's documented impersonation form, so the request acts
    # on the account that owns the domain rather than on the admin.
    who = f"{settings.DIRECTADMIN.api_user}|{username}" if username else settings.DIRECTADMIN.api_user
    try:
        resp = requests.post(f"{base}/api/domain-tls/{domain}/provision-certs",
                             auth=(who, settings.DIRECTADMIN.api_password),
                             verify=resolve_verify(),
                             timeout=settings.SSL_PROVISION_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - reported, never raised into provisioning
        return _result("ssl", FAILED, f"Could not reach DirectAdmin: {e}")

    try:
        body = resp.json()
    except ValueError:
        return _result("ssl", FAILED,
                       f"DirectAdmin returned HTTP {resp.status_code} with a body "
                       f"that was not JSON: {(resp.text or '')[:180]}")

    if resp.status_code == 200:
        # acmeEnabled is the server stating its own capability. Believed over any
        # assumption, and reported as a reason rather than a failure, because
        # nothing is wrong and no amount of retrying will change it.
        if body.get("acmeEnabled") is False:
            return _result("ssl", UNSUPPORTED,
                           "DirectAdmin reports Let's Encrypt is switched off for "
                           "this domain. This is a per-domain setting in the panel "
                           "and can be turned on for it.",
                           acme_enabled=False)
        fulfilled = body.get("certsFulfilled") or []
        failed = list(body.get("dnsNamesFailedChallenge") or [])
        caa = list(body.get("dnsNamesFailedCAA") or [])
        skipped = list(body.get("dnsNamesSkipped") or [])
        if fulfilled:
            names = ", ".join(
                str(c.get("domain") or c.get("name") or c.get("id") or c)
                for c in fulfilled)
            return _result("ssl", ISSUED,
                           f"Let's Encrypt certificate installed for {names}. "
                           f"DirectAdmin renews it automatically.", certs=fulfilled)
        detail = ", ".join(failed or caa or skipped) or "the server gave no reason"
        return _result("ssl", FAILED, f"DirectAdmin issued nothing. {detail}",
                       failed_challenge=failed, failed_caa=caa, skipped=skipped)

    reason = _da_reason(body)
    if reason:
        # A tuple here would make `status` a tuple, and every caller compares it
        # to a string -- it would read as neither success nor failure.
        status = FAILED if reason in ssl_failed_codes else UNSUPPORTED
        return _result("ssl", status, _REASONS[reason], da_code=reason)
    return _result("ssl", FAILED, f"DirectAdmin refused (HTTP {resp.status_code}): {body}")


def enable_cpanel(username: str, domain: str) -> Dict[str, Any]:
    """
    Queue a domain for cPanel AutoSSL.

    AutoSSL validates the domain, installs the certificate and renews it before
    expiry, so queueing it is the whole job. Run over SSH with sudo for the same
    reason IPv6 is: the WHM API token returns "Access denied" for every function
    tried, including ones known to work.

    Not yet exercised against this server. AutoSSL is not installed on thimpchu,
    so the exact function name has not been confirmed against the installed
    plugin's own function list the way `ipv6_enable_account` was. It is written
    to fail visibly with that reason rather than to appear to succeed, and it
    must be verified once AutoSSL is in place before it is relied on.
    """
    username = (username or "").strip().lower()
    # Checked before the provisioner is built, so a bad request cannot fail on an
    # unrelated construction error and report the wrong thing.
    if not username:
        return _result("ssl", FAILED, "No username given.")

    if not settings.CPANEL_AUTOSSL_VERIFIED:
        # Deliberately not attempting a call whose function name is a guess.
        # AutoSSL is absent on this server, so the name could not be read off it,
        # and a wrong name returns a confident-looking error about AutoSSL not
        # being installed -- which would send whoever reads it chasing the wrong
        # problem. Saying "not verified yet" is accurate and short.
        return _result("ssl", UNSUPPORTED,
                       "cPanel AutoSSL has not been set up on this server yet, so "
                       "the certificate step is not attempted. Nothing is wrong "
                       "with this account; it simply has no certificate until "
                       "AutoSSL is installed.")

    from provisioners.cpanel import CPanelProvisioner

    provisioner = CPanelProvisioner(settings.CPANEL.host)

    cmd = (f"whmapi1 --output=json autossl_queue_run user={shlex.quote(username)} "
           f"domain={shlex.quote(domain)}")
    rc, out, err = provisioner.ssh.execute(
        f"sudo -S -p '' {cmd}",
        stdin_data=f"{settings.CPANEL.sudo_password}\n")
    body = " ".join((out or err or "").split())

    if "not installed" in body.lower() or "unknown" in body.lower() or rc != 0:
        return _result("ssl", UNSUPPORTED,
                       "cPanel AutoSSL is not available on this server, so a "
                       "certificate cannot be issued or renewed for it. Install "
                       "AutoSSL from WHM, then queue this domain again.",
                       output=body[:200])
    if '"result":1' in body.replace(" ", "").replace('"result": 1', '"result":1'):
        return _result("ssl", ISSUED,
                       f"Queued for cPanel AutoSSL. It validates the domain, "
                       f"installs the certificate and renews it automatically.",
                       queued=domain)
    return _result("ssl", FAILED,
                   f"cPanel did not queue the domain: {body[:220] or f'exit {rc}'}",
                   output=body[:220])


def enable_ssl(panel: str, username: str, domain: str) -> Dict[str, Any]:
    """
    The whole thing, in the order that matters: gate on DNS, then ask the panel.

    Returns a result for every call, including the ones that were skipped, so
    the handover kit can say what happened rather than showing a blank.
    """
    if not settings.SSL_ENABLED:
        return _result("ssl", SKIPPED,
                       "Automatic certificates are switched off in this "
                       "deployment's settings.")

    gate = dns_gate(panel, domain)
    if not gate["allowed"]:
        return _result("ssl", SKIPPED, gate["reason"], gated=True)

    if panel == "cpanel":
        outcome = enable_cpanel(username, domain)
    elif panel == "directadmin":
        outcome = enable_directadmin(domain, username)
    else:
        return _result("ssl", SKIPPED, f"Unknown panel '{panel}'.")

    if outcome["status"] == ISSUED:
        outcome["dns"] = gate.get("check")
    return outcome
