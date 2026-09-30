"""
Records domain services: a domain registered on nic.bt.bt, and what was then
asked of it -- hosting, or forwarding somewhere else.

Why this exists as a record rather than a field on the account: the customer
chooses after the registration, and the choice is often made days later, by
somebody else. The registration is the durable part; what follows is a
sequence of small facts gathered over time -- did the forwarding get done, who
checked, was the customer told.

The lifecycle, and why each state is worth distinguishing:

    registered    written to nic.bt.bt, nothing decided yet
    awaiting_dns  forwarding requested, waiting for BT to do it by hand
    verified      DNS checked and correct -- but the customer has NOT been told
    notified      the customer has been emailed

`verified` and `notified` are kept apart on purpose. The notification is the
only outward-facing step, and an operator who has verified five domains should
not find out that all five were emailed to customers in the meantime.

Storage is JSONL, one line per change, latest line per domain wins. That matches
the surrender and suspension logs, and it means the history of what happened to
a domain is kept rather than overwritten.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

REGISTERED = "registered"
AWAITING_DNS = "awaiting_dns"
VERIFIED = "verified"
NOTIFIED = "notified"
HOSTING = "hosting"
FORWARDING = "forwarding"

VALID_STATUS = (REGISTERED, AWAITING_DNS, VERIFIED, NOTIFIED)
VALID_SERVICE = (HOSTING, FORWARDING)

# One process writes this file, but the API is served by a threadpool, so two
# requests can append at once. Without this, a domain registered twice in quick
# succession can interleave and lose a line.
_write_lock = threading.Lock()

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_path() -> Path:
    return Path(settings.DOMAIN_SERVICE_LOG).expanduser()


def normalise_domain(domain: str) -> str:
    value = (domain or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(value):
        raise ValueError(f"'{domain}' is not a valid domain name.")
    return value


def append(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Add one line to the log and return what was written."""
    target = log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        # Never raise into the caller. A domain is registered on the registry
        # whether or not this log line lands, and a failure here must not make
        # the operator think the registration itself failed.
        logger.error("Could not write the domain service record to %s: %s "
                     "(the registry write still happened)", target, e)
    return entry


def _read_all() -> List[Dict[str, Any]]:
    target = log_path()
    if not target.exists():
        return []
    entries = []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn final line is expected if the process died mid-write.
                    logger.warning("Skipping an unreadable line in %s", target)
    except OSError as e:
        logger.error("Could not read the domain service log %s: %s", target, e)
    return entries


def current_states() -> List[Dict[str, Any]]:
    """The latest record for every domain, newest change first."""
    latest: Dict[str, Dict[str, Any]] = {}
    for entry in _read_all():
        domain = (entry.get("domain") or "").lower()
        if not domain:
            continue
        latest[domain] = entry          # later lines win
    return sorted(latest.values(), key=lambda e: e.get("updated_at", ""), reverse=True)


def get_state(domain: str) -> Optional[Dict[str, Any]]:
    domain = (domain or "").strip().lower()
    for entry in current_states():
        if (entry.get("domain") or "").lower() == domain:
            return entry
    return None


def record_registration(
    domain: str,
    customer_name: str,
    email: str,
    service: str,
    forwarding_kind: Optional[str] = None,
    forwarding_target: Optional[str] = None,
    registry_action: str = "",
    registry_message: str = "",
) -> Dict[str, Any]:
    """
    Record a domain that has just been written to nic.bt.bt, and what was asked
    of it. Called after the registry write, not before, so the log never claims
    a registration that did not happen.
    """
    domain = normalise_domain(domain)
    if service not in VALID_SERVICE:
        raise ValueError(f"service must be one of: {', '.join(VALID_SERVICE)}")
    status = AWAITING_DNS if service == FORWARDING else REGISTERED
    entry = {
        "domain": domain,
        "customer_name": (customer_name or "").strip(),
        "email": (email or "").strip(),
        "service": service,
        "forwarding_kind": (forwarding_kind or "").strip().lower(),
        "forwarding_target": (forwarding_target or "").strip(),
        "registry_action": registry_action,
        "registry_message": registry_message,
        "status": status,
        "created_at": _now(),
        "updated_at": _now(),
        "notified_at": "",
    }
    append(entry)
    return entry


def record_verification(domain: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Record a DNS check. Only a successful check advances the domain to
    `verified`; a failed one records what was seen and leaves the status alone,
    so a domain that was verified and has since been changed does not silently
    keep its verified status.
    """
    domain = normalise_domain(domain)
    previous = get_state(domain) or {}
    status = VERIFIED if result.get("status") == "forwarded" else previous.get("status", AWAITING_DNS)
    entry = {
        **previous,
        "domain": domain,
        "forwarding_kind": result.get("kind") or previous.get("forwarding_kind", ""),
        "forwarding_target": result.get("target") or previous.get("forwarding_target", ""),
        "observed": result.get("observed", []),
        "last_check_status": result.get("status", ""),
        "last_check_message": result.get("message", ""),
        "last_checked_at": _now(),
        "status": status,
        "updated_at": _now(),
    }
    append(entry)
    return entry


def record_notification(domain: str, sent: bool, message: str,
                        subject: str = "", body: str = "", recipient: str = "",
                        kind: str = "", edited: bool = False,
                        correction: bool = False, reason: str = "") -> Dict[str, Any]:
    """
    Record that the customer was told -- or that the attempt failed.

    The email itself is kept, not just the fact of it. An operator can now edit
    the wording before it goes out, and afterwards there is no copy anywhere
    else: the message left the building, and a customer who later disputes what
    they were told has to be answered from the record. "Notified at 14:02" says
    that something was sent; it does not say what, or to whom, or whether the
    address was one the customer gave us.
    """
    domain = normalise_domain(domain)
    previous = get_state(domain) or {}
    entry = {
        **previous,
        "domain": domain,
        "status": NOTIFIED if sent else previous.get("status", AWAITING_DNS),
        "notified_at": _now() if sent else previous.get("notified_at", ""),
        "notification_message": message,
        "updated_at": _now(),
    }
    if sent:
        # Every send is kept, not just the last. A correction that overwrote the
        # original would leave the record agreeing with the customer's inbox
        # about the wrong thing, which is the one outcome the record exists to
        # prevent. "notification" points at the most recent send so existing
        # readers keep working.
        sends = list(previous.get("sends") or [])
        sends.append({
            "at": entry["notified_at"],
            "to": recipient,
            "subject": subject,
            "body": body,
            # Whether the operator wrote the wording themselves. Passed in rather
            # than worked out by regenerating the default here: that depended on
            # the stored observed values, so a standard email would be mislabelled
            # as edited whenever they were absent or had changed.
            "edited": bool(edited),
            "kind": kind or previous.get("forwarding_kind", ""),
            "correction": bool(correction),
            "correction_of": previous.get("notified_at", "") if correction else "",
            "reason": (reason or "").strip(),
        })
        entry["sends"] = sends
        entry["notification"] = sends[-1]
    append(entry)
    return entry


def list_by_status(*statuses: str) -> List[Dict[str, Any]]:
    wanted = set(statuses)
    return [e for e in current_states() if e.get("status") in wanted]
