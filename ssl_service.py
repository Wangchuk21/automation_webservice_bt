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

- thimpchu (cPanel 138.0): AutoSSL **is** installed and already holds a Let's
  Encrypt EAB account with its terms accepted, so two of the three preconditions
  are met. The third is not: a package does not list features itself, it names a
  feature list, and AutoSSL is not ticked in the "default" feature list that every
  package points at. So the certificate step declines, naming that one change
  rather than a missing plugin.

  I first reported this server as having no AutoSSL at all, on the strength of an
  empty plugins directory and no matching RPM. Both were the wrong way to ask:
  AutoSSL is not a plugin in current cPanel and does not ship as a package with
  that name. `get_autossl_providers` is the authority and answered immediately,
  which is the same lesson as ipv6_enable_account and the DirectAdmin dry run.

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


# cPanel's AutoSSL calls, read out of the server's own API definition rather
# than guessed. The same file gave us ipv6_enable_account, and the same reasoning
# applies: /usr/local/cpanel/Whostmgr/API/1/SSL.pm is the authority.
#
#   get_autossl_providers            -> is AutoSSL usable here, and with whom
#   set_autossl_provider provider=X  -> turn it on server-wide
#   start_autossl_check_for_one_user user=X -> issue and renew for one account
#
# start_autossl_check_for_one_user is the one that matters. It runs
# `autossl --user <name>`, which is exactly the action that is otherwise done by
# logging in as the customer -- but as root over WHM, without their password.
#
# It also dies with "The user [x] does not have the [AutoSSL] feature enabled"
# when the account's package does not include it. On thimpchu none of the eight
# packages list AutoSSL, so that is a second thing to fix, and it is reported by
# name rather than left to be discovered per account.
CPANEL_AUTOSSL_PROVIDERS = "get_autossl_providers"
CPANEL_AUTOSSL_SET_PROVIDER = "set_autossl_provider"
CPANEL_AUTOSSL_RUN = "start_autossl_check_for_one_user"
CPANEL_LETSENCRYPT = "LetsEncrypt"


def _cpanel_ssh():
    """
    An SSH executor for thimpchu, built from settings.

    Not CPanelProvisioner(host): its signature defaults ssh_port to 22, and
    thimpchu does not listen there, so the connection hangs until it times out
    instead of failing at once. Every other panel call in this project passes the
    configured port explicitly, and this does too.
    """
    from provisioners.ssh_client import SSHExecutor

    c = settings.CPANEL
    return SSHExecutor(c.host, port=c.ssh_port, user=c.ssh_user,
                       password=c.ssh_password, key_path=c.ssh_key_path)


def _whm(ssh, function: str, **params) -> tuple:
    """Call a WHM API function over SSH with sudo.

    The API token returns "Access denied" for every function tried, including
    ones known to work, so the SSH route is the one that functions.
    """
    import shlex
    args = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in params.items())
    cmd = f"whmapi1 --output=json {function}" + (f" {args}" if args else "")
    return ssh.execute(f"sudo -S -p '' {cmd}",
                       stdin_data=f"{settings.CPANEL.sudo_password}\n")


def _json_from(body: str) -> dict:
    import json as _json
    body = (body or "").strip()
    start = body.find("{")
    if start < 0:
        return {}
    try:
        return _json.loads(body[start:])
    except ValueError:
        return {}


def autossl_providers() -> Dict[str, Any]:
    """
    Whether AutoSSL is usable on this server, and which certificate authorities
    it can get certificates from.

    Doubles as the availability check: the function returns nothing at all when
    the AutoSSL plugin is not installed, which is the case on thimpchu and is the
    reason the certificate step declines rather than silently doing nothing.
    """
    rc, out, err = _whm(_cpanel_ssh(), CPANEL_AUTOSSL_PROVIDERS)
    body = _json_from(out or err)
    providers = ((body.get("data") or {}).get("payload")) or []
    # The name is module_name. Read off the server's own answer rather than
    # guessed: I first looked for "name" or "id" and got an empty string, then
    # concluded no provider existed -- which was wrong, and would have sent
    # someone hunting for a problem that was not there.
    names = [str(p.get("module_name") or "") for p in providers if isinstance(p, dict)]
    enabled = [str(p.get("module_name") or "") for p in providers
               if isinstance(p, dict) and p.get("enabled")]
    return {"available": bool(providers), "providers": [n for n in names if n],
            "enabled": [n for n in enabled if n], "raw": body, "rc": rc}


def package_has_autossl(package: str = "") -> Optional[bool]:
    """
    Whether an account on this package would be allowed to use AutoSSL.

    The check is two hops, because cPanel keeps it two hops. A package does not
    list features itself; it names a feature list (`FEATURELIST = default`), and
    AutoSSL has to be ticked in *that*. So: read the package for the list it
    points at, then read that list for autossl.

    `start_autossl_check_for_one_user` refuses by name when this is false, so
    without it every account fails individually and none of the failures point at
    the thing to change.

    Returns None when the package or its feature list cannot be read, which is
    different from "no" -- one is unknown and the other is a definite refusal.
    """
    if _whm is None:  # pragma: no cover
        return None
    ssh = _cpanel_ssh()

    rc, out, err = _whm(ssh, "listpkgs")
    pkgs = ((_json_from(out or err).get("data") or {}).get("pkg")) or []
    target = package or settings.CPANEL.default_plan or "default"
    chosen = next((p for p in pkgs if p.get("name") == target), None)
    if chosen is None:
        logger.warning("Package '%s' is not on this server; cannot check its "
                       "AutoSSL feature", target)
        return None
    featurelist = chosen.get("FEATURELIST") or "default"

    rc, out, err = _whm(ssh, "read_featurelist",
                        featurelist=featurelist)
    feats = ((_json_from(out or err).get("data") or {}).get("features"))
    if feats is None:
        logger.warning("Could not read the '%s' feature list; cannot tell whether "
                       "AutoSSL is on it", featurelist)
        return None
    return any(str(k).lower() == "autossl" and str(v) in ("1", "y", "true")
               for k, v in feats.items())


def enable_cpanel(username: str, domain: str) -> Dict[str, Any]:
    """
    Turn on a Let's Encrypt certificate for one cPanel account.

    AutoSSL validates the domain, installs the certificate and renews it before
    expiry, so this is the whole job. Run as root over WHM, so the customer's
    password is not needed even though the same action is what they would do by
    logging in.

    Each precondition is checked and reported by name, because they are three
    separate things someone has to fix and "no certificate" explains none of them.
    """
    username = (username or "").strip().lower()
    # Checked before the provisioner is built, so a bad request cannot fail on an
    # unrelated construction error and report the wrong thing.
    if not username:
        return _result("ssl", FAILED, "No username given.")

    if not settings.CPANEL_AUTOSSL_VERIFIED:
        # The function names are now read off this server's own API definition,
        # so this guard is off by default. It stays for a deployment that has not
        # confirmed its own copy of SSL.pm matches.
        return _result("ssl", UNSUPPORTED,
                       "cPanel AutoSSL has not been confirmed on this server, so "
                       "the certificate step is not attempted. Nothing is wrong "
                       "with this account; it simply has no certificate.")

    try:
        probe = autossl_providers()
    except Exception as e:  # noqa: BLE001
        return _result("ssl", FAILED, f"Could not reach cPanel: {e}")

    if not probe["available"]:
        return _result(
            "ssl", UNSUPPORTED,
            "cPanel AutoSSL is not installed on this server, so no certificate "
            "can be issued or renewed here. Install AutoSSL from WHM; the "
            "account itself is fine and simply has no certificate.")

    # AutoSSL present but never switched on server-wide is a different fix from
    # not installed at all, and "install it" would be the wrong advice.
    if probe.get("enabled") is not None and not probe["enabled"]:
        return _result(
            "ssl", UNSUPPORTED,
            f"AutoSSL is installed on this server but not switched on "
            f"(providers: {', '.join(probe.get('providers') or ['none'])}). "
            f"Enable it in WHM under SSL/TLS Status, then this account can be "
            f"queued for a certificate.")

    names = probe["providers"]
    if names and not any(CPANEL_LETSENCRYPT.lower() in n.lower() for n in names):
        return _result("ssl", UNSUPPORTED,
                       f"AutoSSL is installed but offers no Let's Encrypt "
                       f"provider (available: {', '.join(names) or 'none listed'}).")

    package = settings.CPANEL.default_plan or "default"
    has_feature = package_has_autossl(package)
    if has_feature is None:
        # Unknown, not a refusal. Proceeding risks the per-account error; stopping
        # risks skipping a certificate that could have been issued. Say so rather
        # than guessing either way.
        return _result("ssl", FAILED,
                       f"Could not read the feature list for package '{package}', "
                       f"so whether AutoSSL is enabled for it is unknown. Nothing "
                       f"was suspended and no account was changed.")
    if not has_feature:
        return _result(
            "ssl", UNSUPPORTED,
            f"AutoSSL is installed, but the package this account would use "
            f"('{package}') does not include the AutoSSL feature, so cPanel "
            f"refuses to issue for it. Add AutoSSL to that package in WHM, or "
            f"move the account to one that has it.")

    ssh = _cpanel_ssh()

    # Server-wide on/off switch first. Idempotent, and needed before the
    # per-account call will do anything.
    rc, out, err = _whm(ssh, CPANEL_AUTOSSL_SET_PROVIDER,
                        provider=CPANEL_LETSENCRYPT)
    body = _json_from(out or err)
    meta = body.get("metadata") or {}
    if meta.get("result") == 0:
        errors = "; ".join(str(e) for e in (meta.get("errors") or []))
        return _result("ssl", FAILED,
                       f"cPanel would not enable AutoSSL: {errors or body or out}")

    rc, out, err = _whm(ssh, CPANEL_AUTOSSL_RUN, user=username)
    raw = out or err
    meta = (_json_from(raw).get("metadata") or {})
    if meta.get("result") == 0:
        joined = "; ".join(str(e) for e in (meta.get("errors") or []))
        if "does not have" in joined or "AutoSSL" in joined:
            return _result("ssl", UNSUPPORTED,
                           f"cPanel refused: {joined}. The package must include "
                           f"the AutoSSL feature, not just have AutoSSL installed "
                           f"on the server.")
        return _result("ssl", FAILED, f"cPanel would not start AutoSSL: {joined}")

    return _result("ssl", ISSUED,
                   f"AutoSSL started for {username}. It validates {domain}, "
                   f"installs the certificate and renews it automatically. "
                   f"Certificates appear in cPanel under SSL/TLS Status.",
                   provider=CPANEL_LETSENCRYPT, user=username, domain=domain)


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
