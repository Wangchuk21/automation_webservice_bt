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

---

## 📁 Project Architecture

```
automation_webservice_bt/
├── app.py                     # FastAPI web service & webhook endpoints
├── cli.py                     # Command-line interface for provisioning
├── config.py                  # Environment & server settings loader
├── notifier.py                # Email dispatcher for customer welcome letters
├── nic_client.py              # nic.bt.bt domain registry client
├── requirements.txt           # Python dependencies (exact pins)
├── Dockerfile                 # Multi-stage container build
├── docker-compose.yml         # Container orchestration
├── .dockerignore              # Keeps secrets & host artifacts out of image
├── .env.example               # Server credentials & host configuration template
├── provisioners/
│   ├── base.py                # Base provisioner class & handover templates
│   ├── ssh_client.py          # Paramiko SSH remote command executor
│   ├── cpanel.py              # cPanel / WHM automation engine
│   └── directadmin.py         # DirectAdmin automation engine
├── templates/
│   └── index.html             # Sleek dark-mode dashboard
└── static/
    ├── css/style.css          # Modern styling & animations
    └── js/app.js              # Interactive UI & clipboard copy helpers
```

---

## 🐳 Docker Deployment (Recommended)

The service is stateless — no database, no volumes, no persistent state — so it
containerizes cleanly. Reproducible builds come from the exact pins in
`requirements.txt`.

```bash
cp .env.example .env      # then fill in real server credentials
docker-compose up -d --build
docker-compose logs -f
```

The service is then on **http://127.0.0.1:8000**.

```bash
docker-compose ps                 # health status
docker-compose restart            # survives restarts
docker-compose down               # stop
docker-compose down --rmi local   # stop and remove the image
```

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
docker-compose run --rm provisioner python cli.py test --panel cpanel
docker-compose run --rm provisioner python cli.py create \
  --panel cpanel --domain client.bt --email client@client.bt --dry-run
```

### Image hardening

- Two-stage build: build tooling stays in the builder, runtime is `python:3.13-slim` (~284 MB)
- Runs as non-root `provisioner` (uid 1000)
- `read_only: true` root filesystem with a `tmpfs` `/tmp`
- `no-new-privileges:true`
- `.dockerignore` excludes `.env`, `venv/`, `.git/`, and `__pycache__`, so no credentials or host artifacts enter the image
- `HEALTHCHECK` hits `/api/v1/health`, which touches no external server

> **Note:** `docker-compose.yml` uses v1 syntax (`version: "3.8"`) for the legacy
> `docker-compose` binary. The v2 `docker compose` plugin also works and will
> warn that `version` is obsolete.

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
