# Web Hosting Account Provisioning Automation (`automation_webservice_bt`)

Automated user account provisioning system for **cPanel / WHM** and **DirectAdmin** shared web hosting servers.

Instead of manually navigating through cPanel or DirectAdmin web interfaces, this service automates the entire provisioning lifecycle:
1. Creates the hosting user account on the remote server via **SSH** (`whmapi1` / `createacct` on cPanel, or DirectAdmin CLI/API).
2. Sets up **Customer Web UI** login credentials and **SFTP** (SSH File Transfer Protocol) credentials.
3. Configures document root directory (`public_html/`) for the customer's website files.
4. Generates a **Customer Handover Kit** (credentials, URLs, nameservers, SFTP settings) formatted and ready to send directly to your customer via copy-paste or automated email.

---

## 🚀 Key Features

- **Multi-Control Panel Support**:
  - **cPanel / WHM**: Automated creation via SSH (`whmapi1` / `/scripts/createacct`) or WHM REST API 1.
  - **DirectAdmin**: Automated creation via DirectAdmin API (`CMD_API_ACCOUNT_USER`) or SSH CLI scripts.
- **Customer Handover Kit**:
  - Web UI login URL & credentials.
  - SFTP host, port, username, password, and target directory (`public_html/`).
  - DNS / Nameserver configuration instructions.
- **Multiple Interfaces**:
  - **Web Dashboard**: Modern dark-mode web application running at `http://localhost:8000`.
  - **CLI Terminal Tool**: Fast command-line interface (`python cli.py create ...`).
  - **REST API**: Webhook-ready JSON API (`POST /api/v1/accounts/create`) for billing or CRM integration.
- **Dry-Run Simulation**: Test account generation and credential preview without touching live production servers.
- **Optional Automated Emailing**: Send the welcome email with credentials directly to the customer via SMTP.
- **Service Surrender (Termination)**: Retire a hosting account, a domain registration, or both,
  with a scanned surrender letter attached as evidence and every action written to an audit log.

---

## 📁 Project Architecture

```
automation_webservice_bt/
├── app.py                     # FastAPI web service & webhook endpoints
├── cli.py                     # Command-line interface for provisioning
├── config.py                  # Environment & server settings loader
├── tls_config.py              # Shared TLS verification settings
├── notifier.py                # Email dispatcher for customer welcome letters
├── nic_client.py              # nic.bt.bt domain registry client
├── surrender.py               # Surrender service: evidence, audit log, orchestration
├── requirements.txt           # Python dependencies (exact pins)
├── Dockerfile                 # Multi-stage container build
├── docker-compose.yml         # Container orchestration (Compose v2: `docker compose`)
├── .dockerignore              # Keeps secrets & host artifacts out of image
├── .env.example               # Server credentials & host configuration template
├── provisioners/
│   ├── base.py                # Base provisioner class & handover templates
│   ├── ssh_client.py          # Paramiko SSH remote command executor
│   ├── cpanel.py              # cPanel / WHM automation engine
│   └── directadmin.py         # DirectAdmin automation engine
├── templates/
│   └── index.html             # Sleek dark-mode dashboard
├── tests/
│   ├── test_surrender.py      # Evidence validation, ordering, audit trail
│   └── test_surrender_api.py  # HTTP endpoints, auth gate (servers stubbed)
└── static/
    ├── css/style.css          # Modern styling & animations
    └── js/app.js              # Interactive UI & clipboard copy helpers
```

Run the tests with:

```bash
./venv/bin/python -m unittest discover -s tests -v
```

They never contact a live server — the panel and registry steps are injected as
fakes, so the whole suite is safe to run anywhere.

---

## 🐳 Docker Deployment (Recommended)

The service is stateless — no database, no volumes, no persistent state — so it
containerizes cleanly. Reproducible builds come from the exact pins in
`requirements.txt`.

> **Exception:** surrender evidence and the audit log are written to
> `./data/surrenders` inside the container. Mount that path to a volume if you
> use surrender, or the letters and the audit trail are lost when the container
> is replaced.

```bash
cp .env.example .env      # then fill in real server credentials
docker compose up -d --build
docker compose logs -f
```

The service is then on **http://127.0.0.1:8000**.

```bash
docker compose ps                 # health status
docker compose restart            # survives restarts
docker compose down               # stop
docker compose down --rmi local   # stop and remove the image
```

### TLS certificate verification

All HTTPS calls (WHM, DirectAdmin, nic.bt.bt) verify server certificates.
Controlled by `TLS_VERIFY` in `.env`, which defaults to `true`. For a private
CA, point `TLS_CA_BUNDLE` at the CA file instead of disabling verification.

**If a server is addressed by IP, you must also set its `TLS_HOSTNAME`.** The
cPanel and DirectAdmin certificates carry DNS SANs only — `CN=thimpchu.druknet.bt`
and `CN=yongnay.druknet.bt` — so verifying a connection opened to the bare IP
fails hostname matching even though the certificate chain is trusted:

```ini
CPANEL_SERVER_HOST=202.144.128.216        # used for SSH
CPANEL_TLS_HOSTNAME=thimpchu.druknet.bt   # used for HTTPS/SNI
DIRECTADMIN_SERVER_HOST=202.144.128.131
DIRECTADMIN_TLS_HOSTNAME=yongnay.druknet.bt
```

Leaving `TLS_HOSTNAME` blank works when `SERVER_HOST` is already a hostname.

`DIRECTADMIN_SERVER_IP` sets the address assigned to newly created accounts. It
is required — account creation fails with a clear message rather than guessing
an IP.

### Why the port binds to `127.0.0.1`

`docker-compose.yml` publishes to loopback only, so the service is not reachable
from the network. Loopback alone is the entire access boundary, which is why the
API token below matters if you ever change that.

To use the dashboard from another machine, use an SSH tunnel rather than opening
the port:

```bash
ssh -L 8000:127.0.0.1:8000 user@this-host
```

### API authentication

Every `/api/v1` endpoint is gated by an optional shared token, sent as the
`X-API-Token` header. This includes `POST /api/v1/accounts/create` and
`POST /api/v1/nic/register`, which create hosting accounts and write to the
national domain registry.

**With `API_AUTH_TOKEN` empty (the default) the API is open** — suitable for
local use on `127.0.0.1`. To lock it down, set a token in `.env`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then restart the container and paste the value into the **API Token** box on the
dashboard. It is kept in browser `localStorage` and is never rendered into the
served HTML.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accounts/create \
  -H "X-API-Token: <token>" -H "Content-Type: application/json" \
  -d '{"panel":"cpanel","domain":"customer.bt","dry_run":true}'
```

`/` and `/api/v1/health` are intentionally left open: the dashboard must load
before a token can be entered, and the Docker healthcheck cannot send headers.

> If you expose this beyond loopback, use HTTPS as well — the token would
> otherwise travel in cleartext.

### SSH keys in the container

Key-based SSH auth works via a read-only bind mount of `~/.ssh`:

```yaml
volumes:
  - ~/.ssh:/home/provisioner/.ssh:ro
```

`config.py` calls `os.path.expanduser()` on `CPANEL_SSH_KEY_PATH`, and the image
sets `HOME=/home/provisioner`, so in-container paths resolve under
`/home/provisioner/.ssh/`. If you authenticate by password instead, comment out
the `volumes:` block and set `CPANEL_SSH_PASSWORD` / `DIRECTADMIN_SSH_PASSWORD`
in `.env`.

### Running the CLI in a container

```bash
docker compose run --rm provisioner python cli.py test --panel cpanel
docker compose run --rm provisioner python cli.py create \
  --panel cpanel --domain client.bt --email client@client.bt --dry-run
```

### Image hardening

- Two-stage build: build tooling stays in the builder, runtime is `python:3.13-slim` (~284 MB)
- Runs as non-root `provisioner` (uid 1000)
- `read_only: true` root filesystem with a `tmpfs` `/tmp`
- `no-new-privileges:true`
- `.dockerignore` excludes `.env`, `venv/`, `.git/`, and `__pycache__`, so no credentials or host artifacts enter the image
- `HEALTHCHECK` hits `/api/v1/health`, which touches no external server

> **Use Compose v2** (`docker compose`). The legacy `docker-compose` v1 binary
> cannot talk to Docker Engine 25+; it fails with `KeyError: 'ContainerConfig'`
> *after* it has already stopped the running container, which takes the service
> down. It has been removed from this host.
>
> ### Secret values containing `$`
>
> Compose treats `$NAME` inside an env value as a variable reference and
> silently replaces it with an empty string, so a password containing `$` would
> reach the container truncated with no error. Store such values with Compose's
> `$$` escape (`$$` -> a literal `$`); `config.py` unescapes them for the host
> so the CLI and the container see identical values. This currently affects
> `NIC_PASSWORD`. Related: do **not** quote values in `.env` -- python-dotenv
> strips quotes but Docker's `env_file` does not, which silently corrupted
> `NIC_PASSWORD` and `SMTP_PASSWORD` before.

---

## ⚙️ Quick Setup (Local / Virtualenv)

Prefer Docker? Use the section above. For local development without containers:

### 1. Configure Server Credentials
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Open `.env` and fill in your server details:

```ini
# cPanel Server
CPANEL_SERVER_HOST=cpanel.yourdomain.bt
CPANEL_WEB_URL=https://cpanel.yourdomain.bt:2083
CPANEL_SSH_PORT=22
CPANEL_SSH_USER=root
CPANEL_SSH_KEY_PATH=~/.ssh/id_rsa
# or CPANEL_SSH_PASSWORD=your_password

# DirectAdmin Server
DIRECTADMIN_SERVER_HOST=da.yourdomain.bt
DIRECTADMIN_WEB_URL=https://da.yourdomain.bt:2222
DIRECTADMIN_SSH_PORT=22
DIRECTADMIN_SSH_USER=root
DIRECTADMIN_SSH_KEY_PATH=~/.ssh/id_rsa
# or DIRECTADMIN_SSH_PASSWORD=your_password
```

---

## 💻 Usage

### Option 1: Web Dashboard UI
The web server runs locally:
```bash
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser:
1. Choose **cPanel** or **DirectAdmin**.
2. Enter the customer's domain (e.g. `clientdomain.bt`).
3. Click **Provision Hosting Account** (or check *Dry-Run Simulation* to test).
4. The system provisions the account and displays the **Customer Handover Kit** with one-click copy buttons for Web UI and SFTP credentials!

---

### Option 2: Command Line (CLI)

#### 1. Test Server Connectivity
```bash
# Test cPanel
./venv/bin/python3 cli.py test --panel cpanel

# Test DirectAdmin
./venv/bin/python3 cli.py test --panel directadmin
```

#### 2. Provision a User Account
```bash
# Provision on cPanel
./venv/bin/python3 cli.py create \
  --panel cpanel \
  --domain mycompany.bt \
  --email contact@mycompany.bt \
  --save

# Provision on DirectAdmin
./venv/bin/python3 cli.py create \
  --panel directadmin \
  --domain clientportal.bt \
  --email admin@clientportal.bt \
  --save

# Test without executing on remote server (Dry Run)
./venv/bin/python3 cli.py create \
  --panel cpanel \
  --domain testdemo.bt \
  --dry-run
```

---

### Option 3: REST API Integration

Create accounts programmatically by sending a POST request:

```bash
curl -X POST http://localhost:8000/api/v1/accounts/create \
  -H "Content-Type: application/json" \
  -d '{
    "panel": "cpanel",
    "domain": "customer.bt",
    "email": "client@customer.bt",
    "dry_run": false
  }'
```

#### Response Example:
```json
{
  "success": true,
  "message": "cPanel account created successfully via SSH (whmapi1).",
  "data": {
    "panel": "cpanel",
    "domain": "customer.bt",
    "username": "customer",
    "password": "...",
    "web_url": "https://cpanel.yourdomain.bt:2083",
    "sftp_host": "cpanel.yourdomain.bt",
    "sftp_port": 22,
    "doc_root": "public_html/",
    "nameservers": "ns1.yourdomain.bt, ns2.yourdomain.bt",
    "handover_text": "..."
  }
}
```

---

## 🛑 Service Surrender (Termination)

Retire a service at the customer's request. Available from the dashboard, the
CLI, and the REST API. You choose the scope:

| Scope | Removes |
|---|---|
| `hosting` | The hosting account: files, mail, databases. Leaves the domain registered. |
| `domain` | The domain registration at nic.bt.bt. Leaves the hosting account in place. |
| `both` | Both of the above. |

### How it is guarded

- **The scanned surrender letter is required** (PDF or JPEG), matching the
  process your customer email describes — a letter submitted to the office
  before the next billing date. Set `SURRENDER_REQUIRE_EVIDENCE=false` to
  waive it.
- **Uploads are validated by content, not by filename.** A PHP payload renamed
  to `letter.pdf` is rejected. The stored filename is generated server-side, so
  a hostile name cannot escape the upload directory. Size is capped while
  streaming, so an oversized file is never fully written.
- **Every action is audited.** A `started` record is written *before* anything
  is destroyed, so an interrupted run still leaves a trace. Each record carries
  the reference id, operator, client IP, reason, and the evidence SHA-256.
- **`POST /api/v1/surrenders` refuses to run at all unless `API_AUTH_TOKEN` is
  set**, answering `503`. The rest of the API tolerates an unset token for local
  development; surrender never does, because it destroys customer data.
- **Partial failures are visible.** The response is `409` with
  `"status": "partial"` and the per-step results, rather than a cheerful
  success. A failed registry step never masks a successful hosting step.
- **Hosting is always surrendered before the domain.** The registration is the
  harder asset to restore, so it goes last — a mid-way failure leaves the
  customer still holding the domain.

### Dashboard

The **Service Surrender** card on the dashboard has a *Preview Impact* button
that reports what exists and what would be removed, without changing anything.
Surrendering then requires typing the domain exactly, attaching the letter, and
confirming a browser dialog.

### CLI

```bash
# Both services, with evidence
python3 cli.py surrender \
  --domain customer.bt \
  --scope both \
  --panel cpanel \
  --username customer \
  --reason "Surrender letter BT/2026/114" \
  --evidence ./surrender-letter.pdf \
  --yes

# Domain registration only
python3 cli.py surrender --domain customer.bt --scope domain --evidence ./letter.pdf --yes
```

Omit `--yes` and it prints the target and aborts. A `.php` file renamed to
`.pdf` is refused before any server is contacted.

### REST API

```bash
curl -X POST http://localhost:8000/api/v1/surrenders \
  -H "X-API-Token: $API_AUTH_TOKEN" \
  -F domain=customer.bt \
  -F scope=both \
  -F panel=cpanel \
  -F username=customer \
  -F reason="Surrender letter BT/2026/114" \
  -F confirm=true \
  -F evidence=@./surrender-letter.pdf
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/surrenders/preview` | What would be removed. Read-only. |
| `POST /api/v1/surrenders` | Surrender. Requires `confirm=true` and evidence. |
| `GET /api/v1/surrenders` | Audit history, newest first. |
| `GET /api/v1/surrenders/{id}/evidence` | Download the attached letter. |

### ⚠️ DirectAdmin is recorded, not deleted

DirectAdmin exposes no supported way to remove an account from a script — there
is no delete-user CLI and no `CMD_API_*` call for it; removal is only available
through the panel GUI. On this server that would mean hand-removing the system
user, the `/usr/local/directadmin/data/users/<name>` record, the home directory,
mail stores and databases across 222 live accounts, which risks silently
corrupting DirectAdmin's internal state.

So a DirectAdmin surrender is **recorded with its evidence and flagged
`manual_action_required`**, and the response tells the operator to delete the
account via the DirectAdmin panel (User Level → Delete User). Nothing is
destroyed automatically.

cPanel is fully automated via `whmapi1 removeacct`, and nic.bt.bt via the
portal's own `_method=DELETE` route.

---

## ⏰ Automated Suspension (nightly job)

Replaces the manual pass over the hosting panels. A second container runs
`cron`, which invokes `scripts/suspend_expired.py` each night.

```bash
# report only -- changes nothing (this is what cron runs by default)
./venv/bin/python scripts/suspend_expired.py

# actually suspend
./venv/bin/python scripts/suspend_expired.py --live
```

The schedule lives in [`deploy/crontab`](deploy/crontab) and is **baked into the
image**, not bind-mounted: Debian's cron refuses to read an `/etc/cron.d` file
that isn't owned by root. Edit that file and rebuild to change the schedule.

### What it is allowed to do

It may only ever move an account from **not suspended** to **suspended**, and
only on positive confirmation from BSCS that web-hosting billing has lapsed.

| Current state | Reason | Action |
|---|---|---|
| not suspended | — | suspend as `billing` if lapsed, else nothing |
| suspended | `billing` | skip — already correct, so the job is idempotent |
| suspended | `abuse`, `spam`, `user_bandwidth`, `compromised`, `forwarding`, `Surrendered` | **skip, never rewrite the reason** |
| suspended | blank / unrecognised | skip, fail safe |
| no BSCS match | — | skip — absence of data is never read as non-payment |
| BSCS unreachable or list incomplete | — | **abort the whole run** |

Overwriting a non-billing reason with `billing` would destroy the only record of
why a customer was disconnected, so the job never writes a reason onto an
account that is already suspended. It also **never unsuspends** — restoring
service to someone who has not paid is the more dangerous direction.

cPanel stores suspension reasons as free text, so billing reasons are matched
against an explicit per-panel allowlist. Anything unrecognised is treated as
*not* billing.

### Requirements

- `BSCS_ENABLED=true` and `BSCS_BASE_URL` / `BSCS_USERNAME` / `BSCS_PASSWORD`
  in `.env`
- `DIRECTADMIN_API_PASSWORD` set, without which the job **refuses to run**
  rather than quietly skipping the DirectAdmin panel
- Network reachability to the BSCS portal, the cPanel/DirectAdmin servers and
  nic.bt.bt. On this deployment that is the VPN tunnel on the host; if the
  tunnel is down the job fails closed and suspends nothing.

### Going live

The crontab runs in **dry-run** mode. Watch it for a few days:

```bash
docker compose logs -f suspender
docker compose exec suspender cat /app/data/suspension_audit.jsonl
```

When the list looks right, add `--live` to `deploy/crontab` and rebuild.

### Container notes

`cron` must start as root so it can drop privileges to run the job as
`provisioner`; running it unprivileged fails with `seteuid: Operation not
permitted` and the container crash-loops. The job itself still runs
unprivileged. This container is not `read_only` (unlike the web service) because
cron's writes are spread across several paths, and it never listens on a port.

### Known limits

- The BSCS domain join is **name-based and incomplete**. A lapsed contract whose
  customer record holds only a person's name yields no domain, so that customer
  is never matched. This is the main gap.
- The BSCS portal caps a result page at 25 rows with no working paging control.
  A partial list is detected and aborts the run, but it does not continue.
- Neither panel's suspension **write** has been executed against a real account
  yet; both are gated behind `--live` and should be proven one account at a time.

---

## 🔒 Customer Handover Output Example

When an account is provisioned, the following handover card is automatically generated and ready to send to your customer:

```text
================================================================================
                    WEBSITE HOSTING ACCOUNT CREDENTIALS
================================================================================
Dear Customer,

Your shared web hosting account for 'customer.bt' has been successfully provisioned.
Below are your access credentials to manage your website and upload your web files.

--------------------------------------------------------------------------------
1. WEB CONTROL PANEL ACCESS (Browser Web UI)
--------------------------------------------------------------------------------
Control Panel URL : https://cpanel.yourdomain.bt:2083
Username          : customer
Password          : [Auto-Generated-Secure-Password]

Use the Web UI to manage your domains, databases (MySQL), email accounts, 
SSL certificates, and file manager directly in your browser.

--------------------------------------------------------------------------------
2. SFTP FILE UPLOAD ACCESS (Secure FTP / FileZilla / WinSCP / Cyberduck)
--------------------------------------------------------------------------------
Protocol          : SFTP (SSH File Transfer Protocol)
SFTP Host/Server  : cpanel.yourdomain.bt
SFTP Port         : 22
Username          : customer
Password          : [Auto-Generated-Secure-Password]
Web Document Root : public_html/

* Upload your website files (HTML, PHP, assets) into the 'public_html/' directory.
* Files placed outside this directory will not be visible on the web.

--------------------------------------------------------------------------------
3. DOMAIN DNS & NAMESERVERS
--------------------------------------------------------------------------------
Point your domain nameservers to:
ns1.yourdomain.bt, ns2.yourdomain.bt
================================================================================
```
