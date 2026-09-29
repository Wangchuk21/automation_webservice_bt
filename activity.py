"""
A single record of what has been done, so it can be reviewed later.

The handover kit answers "what just happened" and the domain queue answers "what
is still outstanding". Neither answers "what have we done to this account", and
a reload loses both. A provisioning in particular left no durable trace at all:
the result was shown once on the page and then existed only in the container's
stdout, which rotates.

The other flows already keep their own audit trails -- surrenders with their
evidence, the nightly suspension run, domain services -- and those stay exactly
as they are, because they are the formal record and have their own integrity
requirements. This is a cross-cutting feed for the question "what has been
done", and it never replaces them.

Design notes:

Every event is appended, including refusals. A refused activation is exactly the
kind of thing an operator needs to see later: it says somebody tried to bring
back an account suspended for abuse, and the rule stopped them. A log that only
records successes hides that.

Writes never raise. If the activity log cannot be written, the action that was
already taken has still been taken, and failing the request because the diary
is full would be a bad trade.

Storage is JSONL on the same data volume as the surrender audit trail, newest
last, read back newest first.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

# The kinds of thing worth recording. Fixed set, because the dashboard colours
# and groups by them and an unlisted kind would fall through every filter.
PROVISIONED = "provisioned"
SUSPENDED = "suspended"
ACTIVATED = "activated"
DOMAIN_REGISTERED = "domain_registered"
SUSPENSION_RUN = "suspension_run"

KINDS = (PROVISIONED, SUSPENDED, ACTIVATED, DOMAIN_REGISTERED, SUSPENSION_RUN)

OK = "ok"
REFUSED = "refused"
FAILED = "failed"

# Panel-agnostic so the feed reads the same whatever ran.
_CPANEL_LABEL = {"cpanel": "cPanel", "whm": "cPanel", "directadmin": "DirectAdmin", "da": "DirectAdmin"}


def panel_label(panel: str) -> str:
    return _CPANEL_LABEL.get((panel or "").strip().lower(), (panel or "").strip())


def log_path() -> Path:
    return Path(settings.ACTIVITY_LOG).expanduser()


def record_event(kind: str, summary: str, outcome: str = OK, **fields: Any) -> Optional[Dict[str, Any]]:
    """
    Append one event. Never raises: the action has already happened, and a full
    disk must not turn a successful provisioning into a failed request.
    """
    event = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "outcome": outcome,
        "panel": (fields.pop("panel", "") or ""),
        "username": (fields.pop("username", "") or ""),
        "domain": (fields.pop("domain", "") or ""),
        "summary": summary,
        "detail": fields,
    }
    target = log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.error("Could not write the activity record to %s: %s (the action "
                     "itself still happened)", target, e)
        return None
    return event


def read_events() -> List[Dict[str, Any]]:
    """Every event, oldest first. A torn final line is skipped, not fatal."""
    target = log_path()
    if not target.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping an unreadable line in %s", target)
    except OSError as e:
        logger.error("Could not read the activity log %s: %s", target, e)
    return events


def recent(limit: int = 50, kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The most recent events, newest first."""
    events = read_events()
    if kinds:
        wanted = set(kinds)
        events = [e for e in events if e.get("kind") in wanted]
    return list(reversed(events))[: max(1, min(limit, 500))]


def counts() -> Dict[str, int]:
    """How many of each kind, for the summary line."""
    out: Dict[str, int] = {}
    for e in read_events():
        out[e.get("kind", "?")] = out.get(e.get("kind", "?"), 0) + 1
    return out
