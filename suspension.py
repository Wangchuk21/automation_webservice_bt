"""
Automated suspension of hosting accounts whose billing has lapsed.

Replaces a manual pass over the hosting panels. The billing system (BSCS) says
which web-hosting contracts are no longer active; this turns that into
suspension decisions on cPanel and DirectAdmin.

WHAT THIS IS ALLOWED TO DO
-------------------------
It may only ever move an account from NOT-suspended to suspended, and only when
billing has lapsed. It never unsuspends, and it never writes a reason onto an
account that is already suspended. An account shut off for abuse, spam or
bandwidth keeps that reason, because overwriting it with "billing" would
destroy the only record of why the customer was disconnected.

The decision matrix, in full:

    current state            reason              action
    -----------------------  ------------------  ------------------------------
    not suspended            -                   suspend as "billing" IF lapsed,
                                                 otherwise do nothing
    suspended                billing             skip (already correct; idempotent)
    suspended                abuse/spam/         skip, never rewrite the reason
                             user_bandwidth/
                             compromised/
                             forwarding/
                             Surrendered
    suspended                blank / unknown     skip, fail safe
    could not determine      -                   skip, never infer non-payment
    billing state

Incomplete BSCS data aborts the whole run. A partial page of results looks
exactly like a complete one unless the counter is compared, and acting on a
partial set would suspend some customers while silently ignoring others.
"""
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from config import settings

logger = logging.getLogger(__name__)

# Reasons that mean "suspended because the customer has not paid". Anything not
# listed here is treated as a non-billing suspension and left alone.
#
# cPanel stores a free-text reason typed by whoever suspended the account, so
# the vocabulary is inconsistent and only known billing phrases are matched.
# "not suspended" is a sentinel the panel reports for active accounts, not a
# reason. Anything unrecognised is treated as NOT billing, so an unfamiliar
# reason can never cause a suspension.
BILLING_REASONS: Dict[str, Set[str]] = {
    "cpanel": {
        "pending bills",
        "1 year pending bill",
        "billing issue",
        "payment due",
        "billing",
    },
    "directadmin": {
        "billing",
    },
}

# Sentinel cPanel reports for an active account.
NOT_SUSPENDED_SENTINELS = {"", "not suspended", "none", "null"}

# Action values
SUSPEND = "suspend"
SKIP_ALREADY_BILLING = "skip_already_suspended_billing"
SKIP_OTHER_REASON = "skip_suspended_other_reason"
SKIP_ACTIVE = "skip_billing_active"
SKIP_UNKNOWN_BILLING = "skip_billing_unknown"
SKIP_NO_MATCH = "skip_no_bscs_match"

# Reason written when this module suspends an account. Consistent on purpose, so
# a later run can recognise its own work.
SUSPEND_REASON = "billing"

# Token shapes that look like a domain in a BSCS name field.
_DOMAIN_TOKEN = re.compile(r"^[a-z0-9]([a-z0-9\-]*\.)+[a-z]{2,}$", re.I)
_KNOWN_TLD = re.compile(r"\.(bt|com\.bt|org\.bt|net\.bt|gov\.bt|edu\.bt)$", re.I)


class SuspensionError(RuntimeError):
    """Raised when a run cannot proceed safely."""


@dataclass
class Decision:
    """One account, and what was decided about it."""
    panel: str
    username: str
    domain: str
    action: str
    reason: str = ""
    current_state: str = ""
    contract: str = ""
    bscs_customer: str = ""

    @property
    def will_suspend(self) -> bool:
        return self.action == SUSPEND


@dataclass
class RunReport:
    decisions: List[Decision] = field(default_factory=list)
    skipped_errors: List[str] = field(default_factory=list)
    # Lapsed BSCS contracts that matched no account we manage. Kept so the
    # blind spot in the name-based join is visible to an operator instead of
    # silently dropping customers who are genuinely lapsed.
    unmatched_contracts: List[Dict[str, Any]] = field(default_factory=list)
    bscs_complete: bool = True
    bscs_note: str = ""
    total_accounts: int = 0

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self.decisions:
            out[d.action] = out.get(d.action, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "total_accounts": self.total_accounts,
            "bscs_complete": self.bscs_complete,
            "bscs_note": self.bscs_note,
            "counts": self.counts(),
            "skipped_errors": self.skipped_errors,
            "unmatched_contracts": self.unmatched_contracts,
            "decisions": [asdict(d) for d in self.decisions],
        }


# ---------------------------------------------------------------------------
# Reading billing state
# ---------------------------------------------------------------------------
def normalise_domain(value: str) -> str:
    """
    Lowercase, strip a leading "www." and surrounding punctuation.

    Applied to BOTH sides of the comparison. BSCS stores names inconsistently
    ("www.test.bt" vs "test.bt") while the panels report the bare domain, so
    comparing raw strings would silently fail to match and quietly skip real
    customers.
    """
    d = (value or "").strip().lower().strip('."\'')
    if d.startswith("www."):
        d = d[4:]
    return d


def extract_domains(name_field: str) -> List[str]:
    """
    Pull domain-looking tokens out of a BSCS name field.

    The field is multi-valued and inconsistent: it may be
    "www.test.bt, www.test.bt, www.test.bt" or "Dorji Bhutan, --, www.test.bt"
    or a bare person's name. Only tokens that look like domains are returned,
    lowercased and without a leading "www.", so a name never becomes a domain.
    """
    out: List[str] = []
    for token in re.split(r"[,\n;]+", name_field or ""):
        t = token.strip().lower().strip('."\'')
        if not t or not _DOMAIN_TOKEN.match(t):
            continue
        if not _KNOWN_TLD.search(t):
            continue
        if t.startswith("www."):
            t = t[4:]
        if t and t not in out:
            out.append(t)
    return out


def lapsed_hosting_domains(bscs_client) -> Dict[str, Any]:
    """
    Ask BSCS which web-hosting contracts are not active, in one query.

    Returns {"domains", "complete", "note", "rows"}. "complete" is False when
    the portal returned a partial page; the caller must then abort rather than
    act on an incomplete picture.
    """
    from bscs_client import (
        CONTRACT_SEARCH_ALIAS, CONTRACT_SEARCH_STEP, CONTRACT_SEARCH_SU,
    )

    # Rate plan 157 = "Web Hosting Prepaid Plan"; status 4 = "Deactivated".
    # Both were read from the live portal. Using the option VALUE, not the
    # label, is essential: sending the label matches nothing and returns a
    # plausible-looking empty result.
    RPCODE_WEB_HOSTING = "157"
    CO_STATUS_DEACTIVATED = "4"

    ok, msg = bscs_client.login()
    if not ok:
        raise SuspensionError(f"BSCS login failed: {msg}")

    _, form_html = bscs_client._start_su(CONTRACT_SEARCH_SU, CONTRACT_SEARCH_ALIAS)
    data = bscs_client._collect_form_fields(form_html)
    data.update({
        "RPCODE": RPCODE_WEB_HOSTING,
        "CO_STATUS": CO_STATUS_DEACTIVATED,
        "FW_SubmittedFormPath": "form",
        "SuSubmitButton": "Search",
        "SRCH_COUNT": "50",
    })

    import time as _time
    step = (f"SolutionUnitServlet?SuStepName={CONTRACT_SEARCH_STEP}"
            f"&SuToken={bscs_client._su_token}"
            f"&RequestTimeStamp={int(_time.time() * 1000)}")
    resp = bscs_client._request("POST", f"{bscs_client.base_url}/{step}",
                               data=data, allow_redirects=True)
    text = resp.text

    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    if not re.search(r"search\s*results", plain, re.I):
        raise SuspensionError(
            "BSCS did not execute the search (the portal re-rendered the form). "
            "The query fields are no longer what it expects, so the billing state "
            "is unknown and nothing will be suspended."
        )

    counter = re.search(r"\b(\d+)\s*/\s*(\d+)\b", plain)
    complete = True
    note = ""
    if counter:
        shown, total = (counter.group(1).strip(), counter.group(2).strip())
        if shown.isdigit() and total.isdigit() and int(shown) < int(total):
            complete = False
            note = (f"BSCS returned only {shown} of {total} rows. The portal caps a "
                    f"page at 25, so this list is incomplete.")
    else:
        note = "BSCS returned no result counter; completeness could not be confirmed."

    domains: Set[str] = set()
    rows: List[Dict[str, str]] = []
    for rec in bscs_client._parse_customer_rows(text):
        name_field = rec.get("Customer") or rec.get("Customers") or ""
        found = extract_domains(name_field)
        row = {
            "contract": rec.get("Contract code", ""),
            "customer_code": rec.get("Customer code", ""),
            "public_key": rec.get("Public key", ""),
            "name_field": name_field,
            "domains": found,
        }
        rows.append(row)
        domains.update(found)

    return {"domains": domains, "complete": complete, "note": note, "rows": rows}


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def is_billing_reason(panel: str, reason: str) -> bool:
    """True only for a recognised billing reason. Unknown reasons are False."""
    r = (reason or "").strip().lower()
    if r in NOT_SUSPENDED_SENTINELS:
        return False
    return r in BILLING_REASONS.get(panel, set())


def decide(accounts: Sequence[Dict[str, Any]], lapsed: Dict[str, Any]) -> RunReport:
    """
    Produce a decision for every account. Changes nothing.

    This is the whole safety policy in one pure function: no I/O, no side
    effects, fully testable.
    """
    report = RunReport(total_accounts=len(accounts), bscs_complete=lapsed.get("complete", True))
    if lapsed.get("note"):
        report.bscs_note = lapsed["note"]

    by_domain: Dict[str, List[Dict[str, str]]] = {}
    for row in lapsed.get("rows", []):
        for d in row.get("domains", []):
            nd = normalise_domain(d)
            if nd:
                by_domain.setdefault(nd, []).append(row)

    lapsed_domains: Set[str] = {normalise_domain(d) for d in lapsed.get("domains", set())}
    lapsed_domains.discard("")

    for acct in accounts:
        panel = acct.get("panel", "")
        username = acct.get("username", "")
        domain = normalise_domain(acct.get("domain", ""))
        suspended = bool(acct.get("suspended"))
        reason = (acct.get("reason") or "").strip()

        # Rule 1: never touch an account that is already suspended. This runs
        # before any billing lookup, so an abuse or spam case is never even
        # considered, let alone relabelled.
        if suspended:
            if is_billing_reason(panel, reason):
                action, why = SKIP_ALREADY_BILLING, "already suspended for billing"
            else:
                action = SKIP_OTHER_REASON
                why = f"already suspended (reason: {reason or 'not recorded'}) - not billing, left untouched"
            report.decisions.append(Decision(
                panel=panel, username=username, domain=domain, action=action,
                reason=why, current_state=f"suspended: {reason or 'not recorded'}",
            ))
            continue

        # Rule 2: a domain we could not match to BSCS is not evidence of
        # anything. Absence of a match must never become a suspension.
        if not domain or domain not in lapsed_domains:
            report.decisions.append(Decision(
                panel=panel, username=username, domain=domain, action=SKIP_NO_MATCH,
                reason="no matching lapsed contract in BSCS",
                current_state="not suspended",
            ))
            continue

        rows = by_domain.get(domain, [])
        contracts = ", ".join(r.get("contract", "") for r in rows if r.get("contract"))
        customers = ", ".join(sorted({r.get("name_field", "")[:60] for r in rows if r.get("name_field")}))

        # Rule 3: positive confirmation of non-payment.
        report.decisions.append(Decision(
            panel=panel, username=username, domain=domain, action=SUSPEND,
            reason="web hosting billing lapsed in BSCS",
            current_state="not suspended",
            contract=contracts, bscs_customer=customers,
        ))

    # Anything BSCS says is lapsed that we could not line up with an account.
    # Recorded rather than dropped: a contract here may well be a real customer
    # whose record simply has no domain in it.
    #
    # One account can match several contracts, and Decision.contract holds them
    # comma-joined for display, so the set is built by splitting on commas --
    # otherwise a matched contract would also be listed as unmatched.
    matched_contracts = set()
    for d in report.decisions:
        if d.contract:
            matched_contracts.update(c.strip() for c in d.contract.split(",") if c.strip())
    for row in lapsed.get("rows", []):
        contract = row.get("contract", "")
        if contract and contract in matched_contracts:
            continue
        report.unmatched_contracts.append({
            "contract": contract,
            "customer_code": row.get("customer_code", ""),
            "public_key": row.get("public_key", ""),
            "name_field": row.get("name_field", ""),
            "domains_found": row.get("domains", []),
        })

    return report


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute(
    report: RunReport,
    suspend_handlers: Dict[str, Callable[[str, str], Dict[str, Any]]],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Act on the decisions, or report what would happen.

    A run is refused outright when the billing picture is incomplete, or when
    there is no handler for a panel. Those are the two ways this could quietly
    suspend the wrong accounts.
    """
    if not report.bscs_complete:
        raise SuspensionError(
            "Refusing to run: the BSCS billing list is incomplete. "
            + (report.bscs_note or "")
        )

    results: List[Dict[str, Any]] = []
    for d in report.decisions:
        if not d.will_suspend:
            continue
        handler = suspend_handlers.get(d.panel)
        if handler is None:
            raise SuspensionError(
                f"Refusing to run: no suspension handler for panel '{d.panel}'. "
                "A panel with no handler must never be silently skipped."
            )
        if dry_run:
            results.append({
                "panel": d.panel, "username": d.username, "domain": d.domain,
                "action": "would_suspend", "success": True,
                "message": f"[DRY RUN] would suspend '{d.username}' as '{SUSPEND_REASON}'",
                "contract": d.contract,
            })
            continue
        res = handler(d.username, SUSPEND_REASON)
        results.append({
            "panel": d.panel, "username": d.username, "domain": d.domain,
            "action": "suspended" if res.get("success") else "suspend_failed",
            "success": bool(res.get("success")),
            "already_suspended": bool(res.get("already_suspended")),
            "message": res.get("message", ""),
            "contract": d.contract,
        })
    return {"dry_run": dry_run, "results": results}


def latest_report(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Most recent completed run from the audit log, or None.

    Reads the log backwards for the newest record that has decisions, so the
    dashboard shows the last real run rather than a partial line.
    """
    target = Path(path or settings.SUSPENSION_AUDIT_LOG).expanduser()
    if not target.is_file():
        return None
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.error("Failed to read suspension audit log: %s", e)
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("decisions"):
            return rec
    return None


def unmatched_lapsed(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lapsed BSCS contracts that did NOT resolve to an account we manage.

    This is the blind spot in the join, surfaced rather than hidden. A contract
    whose customer record holds only a person's name yields no domain, so the
    account can never be matched automatically -- but an operator reading the
    dashboard can recognise the name and act, or chase it with billing.
    """
    rec = latest_report(path)
    if not rec:
        return []
    return rec.get("unmatched_contracts", []) or []


def write_audit(report: RunReport, execution: Dict[str, Any],
                path: Optional[str] = None) -> Tuple[str, bool]:
    """
    Append one JSON line recording the whole run. Returns (path, written).

    Written after execution and never raises into the caller: losing the audit
    trail must not mask the outcome, but it is logged loudly.

    The caller is told whether the write happened. It previously returned only
    the path, so the caller logged "audit written" after logging the failure --
    an ERROR and an INFO on the same run, and a dashboard that reported no run
    recorded with nothing on the console to explain why.
    """
    target = Path(path or settings.SUSPENSION_AUDIT_LOG).expanduser()
    record = report.to_dict()
    record["execution"] = execution
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return str(target), True
    except OSError as e:
        logger.error("Failed to write suspension audit record to %s: %s "
                     "(the dashboard will report no run recorded until this "
                     "is fixed)", target, e)
        return str(target), False
