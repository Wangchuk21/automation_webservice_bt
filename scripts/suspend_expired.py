#!/usr/bin/env python3
"""
Suspend hosting accounts whose web-hosting billing has lapsed.

Replaces the manual pass over the hosting panels. Run it from cron.

    # report only -- changes nothing (default)
    ./venv/bin/python scripts/suspend_expired.py

    # actually suspend
    ./venv/bin/python scripts/suspend_expired.py --live

Requires BSCS_ENABLED and DIRECTADMIN_API_PASSWORD in .env. Exits non-zero if
the run could not be completed safely, so a scheduled failure is visible rather
than silent.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bscs_client import BSCSClient
from config import settings
from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner
from suspension import (
    SUSPEND, SuspensionError, decide, execute, lapsed_hosting_domains, write_audit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("suspend")
# paramiko narrates every authentication attempt at INFO, which buries the run
# output. It is not useful here; failures still surface as exceptions.
logging.getLogger("paramiko").setLevel(logging.WARNING)


def build_provisioners():
    cp = CPanelProvisioner(
        host=settings.CPANEL.host, ssh_port=settings.CPANEL.ssh_port,
        ssh_user=settings.CPANEL.ssh_user, ssh_password=settings.CPANEL.ssh_password,
        ssh_key_path=settings.CPANEL.ssh_key_path,
        whm_api_token=settings.CPANEL.whm_api_token, whm_password=settings.CPANEL.whm_password,
        whm_user=settings.CPANEL.whm_user, web_url=settings.CPANEL.web_url,
        tls_hostname=settings.CPANEL.tls_hostname, sftp_port=settings.CPANEL.sftp_port,
        default_plan=settings.CPANEL.default_plan, nameservers=settings.CPANEL.nameservers,
    )
    da = DirectAdminProvisioner(
        host=settings.DIRECTADMIN.host, ssh_port=settings.DIRECTADMIN.ssh_port,
        ssh_user=settings.DIRECTADMIN.ssh_user, ssh_password=settings.DIRECTADMIN.ssh_password,
        ssh_key_path=settings.DIRECTADMIN.ssh_key_path,
        api_user=settings.DIRECTADMIN.api_user, api_password=settings.DIRECTADMIN.api_password,
        web_url=settings.DIRECTADMIN.web_url, tls_hostname=settings.DIRECTADMIN.tls_hostname,
        sftp_port=settings.DIRECTADMIN.sftp_port,
        default_package=settings.DIRECTADMIN.default_package,
        nameservers=settings.DIRECTADMIN.nameservers,
    )
    return cp, da


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="Actually suspend. Without this, only report.")
    ap.add_argument("--only-panel", choices=["cpanel", "directadmin"],
                    help="Limit enumeration to one panel (diagnostics).")
    args = ap.parse_args()
    dry_run = not args.live

    if not settings.BSCS_ENABLED:
        logger.error("BSCS_ENABLED is false. Set it in .env to run the suspension job.")
        return 2
    if not settings.BSCS_BASE_URL or not settings.BSCS_USERNAME:
        logger.error("BSCS is not configured. Set BSCS_BASE_URL and BSCS_USERNAME in .env.")
        return 2

    logger.info("mode: %s", "DRY RUN (nothing will change)" if dry_run else "LIVE SUSPENSION")

    cp, da = build_provisioners()
    accounts = []
    try:
        if args.only_panel != "directadmin":
            cp_accounts = cp.list_accounts()
            logger.info("cPanel: %d accounts", len(cp_accounts))
            accounts += cp_accounts
        if args.only_panel != "cpanel":
            if not settings.DIRECTADMIN.api_password:
                logger.error("DIRECTADMIN_API_PASSWORD is not set; cannot read or "
                             "suspend DirectAdmin accounts. Refusing to run a partial job.")
                return 2
            da_accounts = da.list_accounts()
            logger.info("DirectAdmin: %d accounts", len(da_accounts))
            accounts += da_accounts
    except SuspensionError as e:
        logger.error("%s", e)
        return 2

    if not accounts:
        logger.error("No accounts enumerated. Refusing to run: an empty work list "
                     "must never be treated as 'nothing to do'.")
        return 2

    try:
        lapsed = lapsed_hosting_domains(BSCSClient())
    except SuspensionError as e:
        logger.error("Could not read billing state: %s", e)
        logger.error("Nothing was suspended. This is the safe outcome when the "
                     "billing system cannot be read.")
        return 2

    logger.info("BSCS: %d lapsed web-hosting contract(s); domains matched: %s%s",
                len(lapsed["rows"]),
                ", ".join(sorted(lapsed["domains"])) or "(none)",
                "  [INCOMPLETE]" if not lapsed["complete"] else "")
    if lapsed.get("note"):
        logger.warning("%s", lapsed["note"])

    report = decide(accounts, lapsed)
    counts = report.counts()
    logger.info("decisions over %d accounts: %s", report.total_accounts,
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    handlers = {
        "cpanel": lambda u, reason: cp.suspend_account(u, reason=reason, confirm=True),
        "directadmin": lambda u, reason: da.suspend_account(u, reason=reason, confirm=True),
    }
    if args.only_panel:
        handlers = {k: v for k, v in handlers.items() if k == args.only_panel}

    try:
        execution = execute(report, handlers, dry_run=dry_run)
    except SuspensionError as e:
        logger.error("%s", e)
        logger.error("Nothing was suspended.")
        return 2

    candidates = [d for d in report.decisions if d.will_suspend]
    if not candidates:
        logger.info("No accounts need suspending. %d lapsed contract(s) could not be "
                    "matched to a domain we manage -- see the audit log for those rows.",
                    len(lapsed["rows"]))
    for d in candidates:
        logger.info("%-9s %-16s %-24s %s", d.panel, d.username, d.domain, d.contract)

    audit_path, audit_written = write_audit(report, execution)
    if audit_written:
        logger.info("audit written to %s", audit_path)
    else:
        # write_audit has already logged the cause. Say plainly what it means,
        # because "audit written" printed next to a permission error is how a
        # broken audit trail looks like a working one.
        logger.error("NO AUDIT RECORD WAS WRITTEN to %s. The dashboard will show "
                     "'no run recorded' and nobody can see what this run decided. "
                     "The decisions above are only in this log.", audit_path)

    acted = [r for r in execution["results"] if r.get("action") == "suspended"]
    failed = [r for r in execution["results"] if not r.get("success")]
    logger.info("summary: %d would suspend, %d suspended, %d failed, %d skipped",
                len([r for r in execution["results"] if r.get("action") == "would_suspend"]),
                len(acted), len(failed),
                report.total_accounts - len(candidates))
    for r in failed:
        logger.error("  FAILED %s/%s: %s", r["panel"], r["username"], r["message"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
