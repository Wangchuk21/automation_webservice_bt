#!/usr/bin/env python3
"""
Pre-flight check for a fresh production host.

Run this BEFORE `docker compose up`. It answers the one question that matters at
this point: will this machine be able to do the job?

    python3 scripts/preflight.py

Standard library only, so it runs before anything is built or installed. It
reads .env from the repository root. It never prints a secret value, only
whether one is present.

Exit status is 0 when nothing blocking was found and 1 otherwise, so it can
gate a deployment script.
"""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

FAIL, WARN, OK = "FAIL", "WARN", "OK"
results = []


def record(level, area, message):
    results.append((level, area, message))
    print(f"  [{level:4}] {area:22} {message}")


def section(title):
    print(f"\n{title}")
    print("-" * (len(title) + 2))


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

def read_env():
    values = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


REQUIRED = [
    "API_AUTH_TOKEN",
    "CPANEL_SSH_PASSWORD",
    "DIRECTADMIN_SSH_PASSWORD",
    "DIRECTADMIN_API_PASSWORD",
    "NIC_PASSWORD",
    "BSCS_PASSWORD",
]

# SSH password alone is enough for the two panels; the key is an alternative.
OPTIONAL_IF_KEY = ["CPANEL_SSH_KEY_PATH", "DIRECTADMIN_SSH_KEY_PATH"]


def check_env(env):
    section("Configuration (.env)")
    if not ENV_FILE.exists():
        record(FAIL, ".env", "Not found. It is gitignored, so it did not come "
                              "with the clone. Copy .env.example and fill it in.")
        return
    record(OK, ".env", "Present")

    missing = [k for k in REQUIRED if not env.get(k)]
    has_key = any(env.get(k) for k in OPTIONAL_IF_KEY)
    if missing and not has_key:
        record(FAIL, "credentials", f"Missing or empty: {', '.join(missing)}")
    elif missing:
        record(WARN, "credentials",
               f"Missing: {', '.join(missing)} -- acceptable only if an SSH key is set")
    else:
        record(OK, "credentials", "All required credentials present")

    secret = env.get("SECRET_KEY", "")
    if not secret:
        record(FAIL, "SECRET_KEY", "Not set. Sessions are signed with it.")
    elif secret.startswith("change-this") or len(secret) < 32:
        record(FAIL, "SECRET_KEY", "Still the placeholder or too short. "
                                   "Generate one: python3 -c \"import secrets;"
                                   "print(secrets.token_urlsafe(48))\"")
    else:
        record(OK, "SECRET_KEY", f"Set, {len(secret)} characters")

    # Credentials that were pasted into a chat are compromised regardless of
    # how strong they are, so this cannot be checked by value.
    record(WARN, "credentials", "These were exposed in a chat transcript and "
                                "should be rotated, whatever this host is for.")


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def check_docker():
    section("Docker")
    if not subprocess.run(["which", "docker"], capture_output=True).returncode == 0:
        record(FAIL, "docker", "Not installed.")
        return
    record(OK, "docker", "Installed")

    # Compose v1 is retired and this project's file needs v2.
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if r.returncode != 0:
        record(FAIL, "compose", "`docker compose` (the v2 plugin) is not "
                               "available. Install docker-compose-plugin.")
    else:
        record(OK, "compose", " ".join(r.stdout.split())[:60])

    for svc in ("bt_provisioner", "bt_suspender"):
        r = subprocess.run(["docker", "inspect", svc], capture_output=True)
        if r.returncode == 0:
            record(WARN, "already running", f"{svc} is already up on this host. "
                                             "Two hosts running the nightly "
                                             "job means two audit trails.")


# ---------------------------------------------------------------------------
# Network -- the part that fails silently
# ---------------------------------------------------------------------------

def check_port(label, host, port, timeout=8.0, critical=True):
    if not host:
        record(FAIL, label, "No host configured.")
        return
    started = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            record(OK, label, f"{host}:{port} reachable "
                              f"({time.time() - started:.2f}s)")
    except Exception as e:
        record(FAIL if critical else WARN, label,
               f"{host}:{port} unreachable ({type(e).__name__}). "
               + ("The nightly job cannot work without this." if critical else ""))


def check_network(env):
    section("Network reachability")
    print("  Every one of these is an outbound call the service makes.")

    # The important one. A private address: if this host has no route to it the
    # suspension job finds nothing and reports nothing, which looks identical to
    # a quiet night.
    bscs = env.get("BSCS_BASE_URL", "")
    m = re.search(r"https?://([^:/]+)(?::(\d+))?", bscs)
    if m:
        check_port("BSCS (billing)", m.group(1), m.group(2) or "80")
    else:
        record(FAIL, "BSCS (billing)", f"BSCS_BASE_URL not understood: {bscs!r}")

    check_port("cPanel SSH", env.get("CPANEL_SERVER_HOST"),
               env.get("CPANEL_SSH_PORT", "22"))
    check_port("cPanel WHM", env.get("CPANEL_SERVER_HOST"),
               env.get("CPANEL_WEB_PORT", "2083"), critical=False)
    check_port("DirectAdmin SSH", env.get("DIRECTADMIN_SERVER_HOST"),
               env.get("DIRECTADMIN_SSH_PORT", "22"))
    check_port("DirectAdmin API", env.get("DIRECTADMIN_SERVER_HOST"),
               env.get("DIRECTADMIN_WEB_PORT", "2222"))
    check_port("nic.bt.bt", "nic.bt.bt", 443, critical=False)
    if env.get("SMTP_ENABLED", "").lower() in ("true", "1", "yes"):
        check_port("SMTP", env.get("SMTP_HOST"), env.get("SMTP_PORT", "587"),
                   critical=False)


# ---------------------------------------------------------------------------
# Sudo
# ---------------------------------------------------------------------------

def check_sudo(env):
    section("Sudo (needed for the post-create steps)")
    for prefix, host_key, port_key, user_key in (
        ("CPANEL", "CPANEL_SERVER_HOST", "CPANEL_SSH_PORT", "CPANEL_SSH_USER"),
        ("DIRECTADMIN", "DIRECTADMIN_SERVER_HOST", "DIRECTADMIN_SSH_PORT",
         "DIRECTADMIN_SSH_USER"),
    ):
        host, port, user = env.get(host_key), env.get(port_key, "22"), env.get(user_key)
        if not host:
            record(FAIL, prefix, "No host configured.")
            continue
        # cPanel IPv6 and the DirectAdmin AllowUsers edit both run as root over
        # SSH. Without it those steps fail on every provisioning.
        record(WARN, prefix, f"Cannot test sudo without credentials. {user}@{host}:{port} "
                             f"is expected to have full root (that is how it is set up "
                             f"on the current host). Verify manually: "
                             f"ssh {user}@{host} -p {port} 'sudo -n true'")


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------

def check_host(env):
    section("Host")
    tz = env.get("TZ") or os.environ.get("TZ") or "(unset)"
    if "Thimphu" in tz:
        record(OK, "timezone", f"{tz}")
    else:
        record(WARN, "timezone",
               f"{tz}. The containers set TZ themselves, but the host clock being "
               f"elsewhere makes cron logs and file times confusing to read.")

    try:
        st = os.statvfs(ROOT)
        free_gb = (st.f_bavail * st.f_frsize) / 1024**3
        if free_gb < 2:
            record(FAIL, "disk", f"Only {free_gb:.1f} GB free. The image is ~300 MB.")
        else:
            record(OK, "disk", f"{free_gb:.1f} GB free")
    except OSError as e:
        record(WARN, "disk", f"Could not read free space: {e}")

    try:
        with open("/proc/cpuinfo") as f:
            cpus = sum(1 for ln in f if ln.startswith("processor"))
        record(OK, "cpu", f"{cpus} core(s)")
    except OSError:
        pass

    section("History")
    record(WARN, "audit data", "The surrender audit log, the scanned surrender "
                               "letters and the suspension trail live in Docker "
                               "volumes on the machine that was used before. They "
                               "do not travel with the code, and a new host starts "
                               "with empty volumes.")


def main():
    print("Pre-flight check for the hosting automation service")
    print(f"Repository: {ROOT}")
    env = read_env()
    check_env(env)
    check_docker()
    check_network(env)
    check_sudo(env)
    check_host(env)

    section("Summary")
    blocking = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]
    for level, area, message in blocking:
        print(f"  BLOCKING  {area}: {message}")
    print(f"\n  {len(blocking)} blocking, {len(warnings)} warning(s), "
          f"{len(results) - len(blocking) - len(warnings)} passed")

    if blocking:
        print("\n  Do not start the service yet. Fix the above first.")
        return 1
    print("\n  Ready. Next:\n"
          "    docker compose up -d --build\n"
          "    docker compose ps          # both should report healthy/up\n"
          "    curl -s localhost:8000/api/v1/health")
    return 0


if __name__ == "__main__":
    sys.exit(main())
