"""
Check whether a domain's DNS actually points at the server we host it on.

Why DNS and not a ping: ICMP is commonly filtered between customers and the
hosting network, so a failed ping says nothing about whether the domain is
mapped. What matters after provisioning is whether the A record resolves to an
address this server answers on.

Three outcomes, deliberately distinguished:

  mapped      -- resolves, and at least one address is one of ours
  not_mapped  -- resolves, but to addresses that are not ours
  unresolved  -- no address record at all

"not_mapped" is the interesting one after a fresh provisioning: the account
exists but nothing points at it yet, which is worth saying plainly rather than
leaving the operator to assume the customer can reach the site.

Resolution runs in a worker thread with a deadline. A nameserver that accepts a
query and then never answers would otherwise hold the request open until the
client gives up, and this is called from the page right after a provisioning.
"""
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

RESOLVE_TIMEOUT = 8.0
CONNECT_TIMEOUT = 4.0
# Margin over RESOLVE_TIMEOUT for the whole check, which may resolve the domain
# and the server's own hostname. Kept short: this is called from the page
# straight after a provisioning, so the operator is waiting on it.
CHECK_DEADLINE = RESOLVE_TIMEOUT + 2.0

MAPPED = "mapped"
NOT_MAPPED = "not_mapped"
UNRESOLVED = "unresolved"


def resolve_ips(domain: str, timeout: float = RESOLVE_TIMEOUT) -> List[str]:
    """
    The addresses a domain resolves to, IPv4 and IPv6.

    Returns an empty list when it does not resolve, which includes both "no such
    domain" and "the nameserver did not answer": from the hosting side those are
    the same problem, and neither is worth a stack trace.
    """
    # DNS is case-insensitive, so normalise here rather than relying on every
    # caller to have done it.
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain:
        return []
    try:
        # getaddrinfo returns a list, not a context manager.
        found = [entry[4][0]
                 for entry in socket.getaddrinfo(domain, None, proto=socket.IPPROTO_TCP)]
    except (socket.gaierror, socket.herror, UnicodeError, OSError) as e:
        logger.info("Could not resolve %s: %s", domain, e)
        return []
    # Order, and drop duplicates: a round-robin record should not print the same
    # address three times, and the comparison below only needs the set.
    seen, out = set(), []
    for ip in found:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def hosting_addresses(panel: str) -> List[str]:
    """
    Addresses that count as "ours" for a panel.

    The configured SERVER_IP is authoritative when set. The panel's own host is
    added too, because it is frequently the address directly (both servers here
    are configured by IP) and it is what a freshly created account gets.

    A shared-hosting IP is not always the control panel's address, so
    HOSTING_SERVER_IPS can add more without changing either server's settings.
    """
    server = (settings.CPANEL if panel == "cpanel" else settings.DIRECTADMIN) \
        if panel in ("cpanel", "directadmin") else None

    addresses: List[str] = []
    for value in (server.server_ip if server else "", server.host if server else ""):
        for part in (value or "").replace(";", ",").split(","):
            part = part.strip()
            if _is_ip(part):
                addresses.append(part)
            elif part:
                # A hostname in SERVER_IP: resolve it rather than ignore it.
                addresses.extend(resolve_ips(part))
    extras = getattr(settings, "HOSTING_SERVER_IPS", "") or ""
    for part in extras.replace(";", ",").split(","):
        part = part.strip()
        if _is_ip(part):
            addresses.append(part)
        elif part:
            # A hostname: resolve it rather than silently ignoring it.
            addresses.extend(resolve_ips(part))

    seen, out = set(), []
    for ip in addresses:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _classify(resolved: List[str], ours: List[str]) -> str:
    if not resolved:
        return UNRESOLVED
    if set(resolved) & set(ours):
        return MAPPED
    return NOT_MAPPED


def _port_answers(ip: str, port: int = 80, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Whether a TCP connection to this address is accepted."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_domain(domain: str, panel: str = "cpanel",
                 probe: bool = True) -> Dict[str, object]:
    """
    Report whether a domain's DNS points at the server hosting it.

    `probe` opens a TCP connection to port 80 on an address that is already known
    to be ours, to say whether the web server is answering. It is never used
    against an address that is not ours: that would mean this host reaching out
    to a third party's server on a customer's say-so, which is not something to
    do quietly as part of a provisioning check.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"domain": "", "status": UNRESOLVED, "resolved": [], "ours": [],
                "message": "No domain given."}

    def work() -> Tuple[List[str], List[str], str]:
        resolved = resolve_ips(domain)
        ours = hosting_addresses(panel)
        return resolved, ours, _classify(resolved, ours)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        resolved, ours, status = pool.submit(work).result(timeout=CHECK_DEADLINE)
    except FutureTimeout:
        logger.warning("DNS check for %s did not finish in time", domain)
        return {"domain": domain, "status": UNRESOLVED, "resolved": [], "ours": [],
                "message": "The nameserver did not answer in time. Check again shortly."}
    finally:
        # wait=False, and not a context manager: leaving via "with" would join
        # the stuck worker and block exactly as long as the deadline was meant
        # to prevent. The abandoned thread finishes on its own and is discarded.
        pool.shutdown(wait=False, cancel_futures=True)

    ours_set = set(ours)
    # The addresses that are both resolved and ours. Probing only ever uses one
    # of these, never an address that merely appeared in the domain's DNS.
    match = [a for a in resolved if a in ours_set]
    web_answers: Optional[bool] = _port_answers(match[0]) if (probe and match) else None

    if status == MAPPED:
        message = f"Points to this server ({', '.join(match)})."
        if web_answers:
            message += " The web server is answering on port 80."
        else:
            message += (" The web server did not answer on port 80, which is normal "
                        "for an account with no content yet.")
    elif status == NOT_MAPPED:
        message = (f"Resolves to {', '.join(resolved)}, which is not this server "
                   f"({', '.join(ours) or 'no address configured'}). The hosting "
                   f"account exists but the domain does not point at it.")
    else:
        message = ("The domain has no address record, so nothing can reach this "
                   "hosting account yet. A DNS A record is needed.")

    return {
        "domain": domain,
        "status": status,
        "resolved": resolved,
        "ours": ours,
        "web_answers": web_answers,
        "message": message,
    }

# ---------------------------------------------------------------------------
# Forwarding verification
#
# Forwarding is done by BT staff, by hand, in systems this service does not
# touch: nic.bt.bt holds no DNS or nameserver records (its domain form is the
# 23 required fields plus notes, and the portal has no such page), and .bt
# delegation belongs to ns1/ns2.druknet.bt. So the system's part is to verify,
# and the verification differs by kind:
#
#   a           the domain should resolve to the target address
#   nameserver  the domain should be delegated to the target nameservers
#
# Asking the wrong question is the risk here. For a nameserver delegation the
# A record is still whatever it always was, so an address check would pass for
# a domain that was never forwarded. Hence the kind decides the question.
# ---------------------------------------------------------------------------

FORWARD_A = "a"
FORWARD_NAMESERVER = "nameserver"
FORWARD_KINDS = (FORWARD_A, FORWARD_NAMESERVER)

# "not yet" covers the three ways a delegation can be incomplete or absent, all
# of which mean the same thing to an operator: nobody has finished the job.
# Distinct from an error, which is handled separately.
NOT_FORWARDED = "not_forwarded"
FORWARDED = "forwarded"
MISMATCH = "mismatch"


def resolve_ns(domain: str, lifetime: float = RESOLVE_TIMEOUT) -> List[str]:
    """
    The nameservers a domain is delegated to.

    Needs a real DNS query: socket.getaddrinfo answers addresses and nothing
    else, so the delegation is invisible to it.

    Returns an empty list when there is no delegation record -- which includes
    the name not existing at all, and the nameserver not answering. For an
    operator those are the same situation, and neither is worth a stack trace.
    """
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain:
        return []
    try:
        import dns.resolver
    except ImportError:  # pragma: no cover - dependency is in requirements
        logger.warning("dnspython is not installed, so nameserver delegation "
                       "cannot be verified")
        return []
    try:
        answers = dns.resolver.resolve(domain, "NS", lifetime=lifetime)
        names = sorted({str(r).rstrip(".").lower() for r in answers})
        return names
    except Exception as e:
        # NoAnswer, NXDOMAIN, LifetimeTimeout and NoNameservers all mean the
        # same thing here: there is no delegation to check against.
        logger.info("No NS record for %s: %s", domain, type(e).__name__)
        return []


def _normalise_target(kind: str, target: str) -> List[str]:
    """Comparable forms of what the operator said they pointed the domain at."""
    values = [v.strip().lower().rstrip(".") for v in (target or "").replace(";", ",").split(",")]
    values = [v for v in values if v]
    if kind == FORWARD_A:
        return sorted({v for v in values if _is_ip(v)})
    return sorted({v for v in values})


def check_forwarding(domain: str, kind: str, target: str) -> Dict[str, object]:
    """
    Whether a domain has actually been forwarded as requested.

    `kind` is "a" or "nameserver"; `target` is the address or the nameserver
    list BT said they set it to. Returns one of forwarded, not_forwarded or
    mismatch, with the observed values so an operator can see what is actually
    there rather than being told only yes or no.
    """
    domain = (domain or "").strip().lower()
    kind = (kind or "").strip().lower()
    result: Dict[str, object] = {
        "domain": domain, "kind": kind, "target": target,
        "observed": [], "expected": _normalise_target(kind, target),
    }

    if kind not in FORWARD_KINDS:
        result["status"] = MISMATCH
        result["message"] = (f"Unknown forwarding kind '{kind}'. "
                             f"Use one of: {', '.join(FORWARD_KINDS)}.")
        return result
    if not result["expected"]:
        result["status"] = MISMATCH
        result["message"] = (f"No usable target was given for this {kind} "
                             f"forwarding, so it cannot be verified.")
        return result
    if not domain:
        result["status"] = MISMATCH
        result["message"] = "No domain given."
        return result

    if kind == FORWARD_A:
        observed = sorted(set(resolve_ips(domain)))
        noun = "address"
    else:
        observed = resolve_ns(domain)
        noun = "nameserver"
    result["observed"] = observed

    if not observed:
        result["status"] = NOT_FORWARDED
        result["message"] = (f"{domain} has no {noun} record yet, so the "
                             f"forwarding has not been done.")
        return result

    if sorted(observed) == result["expected"] or set(observed) & set(result["expected"]):
        result["status"] = FORWARDED
        result["message"] = (f"{domain} is delegated to {', '.join(observed)}, "
                             f"as requested.")
        return result

    result["status"] = MISMATCH
    result["message"] = (f"{domain} points to {', '.join(observed)}, not the "
                         f"expected {', '.join(result['expected'])}. It may have "
                         f"been forwarded somewhere else, or the change has not "
                         f"propagated yet.")
    return result


def lookup_records(domain: str, kind: str = FORWARD_A) -> Dict[str, object]:
    """
    What the domain's DNS actually says right now, for the given kind.

    Separate from check_forwarding because asking "what is it now" is a
    different question from "is it what you wanted". The operator needs the first
    one while they are filling the form in -- to read the current nameservers off
    the screen rather than typing them from memory, where a single wrong
    character means the check never matches and the forwarding looks like it was
    never done.
    """
    domain = (domain or "").strip().lower()
    kind = (kind or FORWARD_A).strip().lower()
    if kind not in FORWARD_KINDS:
        return {"domain": domain, "kind": kind, "observed": [],
                "message": f"Unknown kind '{kind}'. Use one of: {', '.join(FORWARD_KINDS)}."}
    if kind == FORWARD_A:
        observed = sorted(set(resolve_ips(domain)))
        noun = "address"
    else:
        observed = resolve_ns(domain)
        noun = "name server"
    return {
        "domain": domain, "kind": kind, "observed": observed,
        "message": (f"{domain} currently resolves to {', '.join(observed)}."
                    if observed and kind == FORWARD_A else
                    (f"{domain} is currently delegated to {', '.join(observed)}."
                     if observed else
                     f"{domain} has no {noun} record yet.")),
    }
