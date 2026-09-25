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
├── requirements.txt           # Python dependencies
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

## ⚙️ Quick Setup

### 1. Configure Server Credentials
Copy [.env.example](file:///Users/tandingyeltshen/Documents/Automation/.env.example) to `.env`:
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
