"""Shared TLS settings for outbound HTTPS calls.

All hosting and registry endpoints this service talks to (WHM on 2087,
DirectAdmin on 2222, nic.bt.bt) present publicly-trusted Let's Encrypt or
Sectigo certificates, so verification can simply be switched on. The one
complication is hostname matching: the cPanel and DirectAdmin certificates
carry DNS SANs only, so a request opened to the bare server IP fails
verification even though the chain is trusted. For those, set
CPANEL_TLS_HOSTNAME / DIRECTADMIN_TLS_HOSTNAME to the certificate's DNS name.
"""

from typing import Optional, Union

import requests
import urllib3

from config import settings


def resolve_verify() -> Union[bool, str]:
    """
    Value to pass as requests' `verify=` argument.

    Returns the CA bundle path when one is configured (requests accepts a path
    and still verifies the chain), otherwise the TLS_VERIFY boolean.
    """
    bundle = (settings.TLS_CA_BUNDLE or "").strip()
    if bundle:
        return bundle
    return bool(settings.TLS_VERIFY)


def api_base_url(host: str, tls_hostname: Optional[str] = None, port: Optional[int] = None) -> str:
    """
    Base URL for an API call, preferring the TLS hostname over a bare IP.

    Using the certificate's DNS name keeps hostname verification working when
    the server is addressed by IP.
    """
    hostname = (tls_hostname or "").strip() or host
    if port:
        return f"https://{hostname}:{port}"
    return f"https://{hostname}"


def warn_if_unverified(where: str) -> None:
    """
    Log a clear warning when certificate checks are disabled.

    urllib3's own InsecureRequestWarning is deliberately NOT suppressed here,
    so disabling verification stays visible in the logs.
    """
    if not resolve_verify():
        import logging
        logging.getLogger(__name__).warning(
            "TLS certificate verification is DISABLED for %s. Set TLS_VERIFY=true "
            "or provide TLS_CA_BUNDLE.", where
        )


__all__ = ["resolve_verify", "api_base_url", "warn_if_unverified", "requests"]
