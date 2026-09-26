import secrets
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any
import os
from pathlib import Path

from config import settings
from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner
from provisioners.base import (
    generate_secure_password,
    sanitize_username,
    validate_domain,
    validate_username,
    validate_email,
    validate_package,
    validate_postal_code,
    validate_country,
    validate_renewal_date,
    ValidationError,
)
from notifier import send_customer_welcome_email, test_smtp_connection
from nic_client import NICClient

app = FastAPI(
    title="Automation WebService BT",
    description="Automated Shared Hosting Provisioning for cPanel and DirectAdmin",
    version="1.0.0"
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Ensure directories exist
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# API authentication
#
# Every /api/v1 endpoint below is capable of creating hosting accounts or
# writing to the national domain registry, so they are gated by an optional
# shared token sent as `X-API-Token`.
#
# When API_AUTH_TOKEN is unset the service stays open, which keeps local
# development and the single-operator dashboard working with no extra setup.
# Set it in .env to lock the API down. / and /api/v1/health are intentionally
# left open: the dashboard must load before a token can be entered, and the
# Docker healthcheck has no way to send headers.
# ---------------------------------------------------------------------------
def require_api_token(x_api_token: Optional[str] = Header(None, alias="X-API-Token")) -> None:
    expected = settings.API_AUTH_TOKEN
    if not expected:
        return  # No token configured -> open, for local use
    if not x_api_token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Token header. Send the API_AUTH_TOKEN value from .env.",
        )
    # Constant-time comparison to avoid leaking the token through timing.
    if not secrets.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=401, detail="Invalid API token.")

def get_cpanel_provisioner():
    return CPanelProvisioner(
        host=settings.CPANEL.host,
        ssh_port=settings.CPANEL.ssh_port,
        ssh_user=settings.CPANEL.ssh_user,
        ssh_password=settings.CPANEL.ssh_password,
        ssh_key_path=settings.CPANEL.ssh_key_path,
        whm_api_token=settings.CPANEL.whm_api_token,
        whm_password=settings.CPANEL.whm_password,
        whm_user=settings.CPANEL.whm_user,
        web_url=settings.CPANEL.web_url,
        tls_hostname=settings.CPANEL.tls_hostname,
        sftp_port=settings.CPANEL.sftp_port,
        default_plan=settings.CPANEL.default_plan,
        nameservers=settings.CPANEL.nameservers
    )

def get_da_provisioner():
    return DirectAdminProvisioner(
        host=settings.DIRECTADMIN.host,
        ssh_port=settings.DIRECTADMIN.ssh_port,
        ssh_user=settings.DIRECTADMIN.ssh_user,
        ssh_password=settings.DIRECTADMIN.ssh_password,
        ssh_key_path=settings.DIRECTADMIN.ssh_key_path,
        api_user=settings.DIRECTADMIN.api_user,
        api_password=settings.DIRECTADMIN.api_password,
        web_url=settings.DIRECTADMIN.web_url,
        tls_hostname=settings.DIRECTADMIN.tls_hostname,
        server_ip=settings.DIRECTADMIN.server_ip,
        sftp_port=settings.DIRECTADMIN.sftp_port,
        default_package=settings.DIRECTADMIN.default_package,
        nameservers=settings.DIRECTADMIN.nameservers
    )


class AccountCreateRequest(BaseModel):
    panel: str = Field(..., description="'cpanel' or 'directadmin'")
    domain: str = Field(..., description="Customer domain (e.g., client.bt)")
    username: Optional[str] = Field(None, description="Hosting account username (auto-generated if empty)")
    password: Optional[str] = Field(None, description="Hosting account password (auto-generated if empty)")
    email: Optional[str] = Field(None, description="Customer notification email")
    package: Optional[str] = Field(None, description="Hosting package/plan")
    send_email: bool = Field(False, description="Send credentials directly to customer email via SMTP")
    register_nic: bool = Field(False, description="Register or update domain on nic.bt.bt")
    customer_name: Optional[str] = Field(None, description="Customer or organization name for nic.bt.bt")
    phone: Optional[str] = Field(None, description="Customer telephone for nic.bt.bt")
    address: Optional[str] = Field(None, description="Customer address for nic.bt.bt")
    postal_code: Optional[str] = Field(None, description="Customer postal code for nic.bt.bt (required by the registry form)")
    country: Optional[str] = Field(None, description="Customer country code for nic.bt.bt")
    renewal_date: Optional[str] = Field(None, description="Domain renewal date for nic.bt.bt, YYYY-MM-DD")
    dry_run: bool = Field(False, description="Simulate account creation without modifying remote server")

    # Defence in depth against shell injection. The provisioners shlex.quote()
    # everything they interpolate into a remote command; these reject malformed
    # input before it ever reaches a provisioner or a live server.
    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str) -> str:
        return validate_domain(v)

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: Optional[str]) -> Optional[str]:
        return validate_username(v) if v else v

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: Optional[str]) -> Optional[str]:
        return validate_email(v) if v else v

    @field_validator("package")
    @classmethod
    def _check_package(cls, v: Optional[str]) -> Optional[str]:
        return validate_package(v) if v else v

    @field_validator("postal_code")
    @classmethod
    def _check_postal_code(cls, v: Optional[str]) -> Optional[str]:
        return validate_postal_code(v) if v else v

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: Optional[str]) -> Optional[str]:
        return validate_country(v) if v else v

    @field_validator("renewal_date")
    @classmethod
    def _check_renewal_date(cls, v: Optional[str]) -> Optional[str]:
        return validate_renewal_date(v) if v else v


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "cpanel_host": settings.CPANEL.host,
            "cpanel_web": settings.CPANEL.web_url,
            "da_host": settings.DIRECTADMIN.host,
            "da_web": settings.DIRECTADMIN.web_url,
            "smtp_enabled": settings.SMTP_ENABLED
        }
    )


@app.get("/api/v1/health")
async def health_check():
    return {"status": "ok", "service": "automation_webservice_bt"}


@app.get("/api/v1/generate-credentials", dependencies=[Depends(require_api_token)])
async def generate_credentials(domain: Optional[str] = None):
    pwd = generate_secure_password(16)
    user = sanitize_username(domain) if domain else ""
    return {"suggested_username": user, "suggested_password": pwd}


@app.get("/api/v1/packages", dependencies=[Depends(require_api_token)])
async def get_packages(panel: str = "cpanel"):
    if panel in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
        return {"panel": "cpanel", "packages": prov.list_packages()}
    elif panel in ("directadmin", "da"):
        prov = get_da_provisioner()
        return {"panel": "directadmin", "packages": prov.list_packages()}
    return {"panel": panel, "packages": ["default"]}


@app.get("/api/v1/servers/status", dependencies=[Depends(require_api_token)])
async def check_servers():
    """Test connection to both configured hosting servers."""
    cp_prov = get_cpanel_provisioner()
    da_prov = get_da_provisioner()
    
    cp_status = cp_prov.test_connection()
    da_status = da_prov.test_connection()
    
    return {
        "cpanel": {
            "host": settings.CPANEL.host,
            "web_url": settings.CPANEL.web_url,
            "status": cp_status
        },
        "directadmin": {
            "host": settings.DIRECTADMIN.host,
            "web_url": settings.DIRECTADMIN.web_url,
            "status": da_status
        }
    }


@app.post("/api/v1/smtp/test", dependencies=[Depends(require_api_token)])
async def test_smtp(recipient: Optional[str] = None):
    """Test SMTP connection to Zimbra/mail server, optionally sending a test email."""
    ok, msg = test_smtp_connection(recipient=recipient)
    return {"success": ok, "message": msg, "host": settings.SMTP_HOST, "port": settings.SMTP_PORT}


@app.post("/api/v1/accounts/create", dependencies=[Depends(require_api_token)])
async def create_account(payload: AccountCreateRequest):
    panel = payload.panel.lower().strip()
    if panel in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
    elif panel in ("directadmin", "da"):
        prov = get_da_provisioner()
    else:
        raise HTTPException(status_code=400, detail=f"Invalid panel '{payload.panel}'. Must be 'cpanel' or 'directadmin'.")

    result = prov.create_account(
        domain=payload.domain,
        username=payload.username,
        password=payload.password,
        email=payload.email,
        package=payload.package,
        dry_run=payload.dry_run
    )

    if not result.success:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": result.message,
                "domain": result.domain,
                "panel": result.panel,
                "raw_response": result.raw_response
            }
        )

    email_status = None
    if payload.send_email and payload.email:
        sent, err = send_customer_welcome_email(result)
        email_status = {"sent": sent, "message": err}

    nic_status = None
    if payload.register_nic:
        if not payload.postal_code:
            # nic.bt.bt marks postal code as required on the domain form. Fail
            # here with a clear message rather than submitting "-" and getting an
            # opaque rejection from the registry after the account is created.
            raise HTTPException(
                status_code=422,
                detail=(
                    "postal_code is required to register a domain on nic.bt.bt. "
                    "Supply the customer's postal code, or untick the registry option."
                ),
            )
        if payload.dry_run:
            # Report the skip explicitly; returning None makes the dashboard
            # show nothing at all, which reads as "silently ignored".
            nic_status = {
                "success": True,
                "action": "skipped",
                "domain": result.domain,
                "message": (
                    "nic.bt.bt update skipped because this was a dry-run. "
                    "Tick the box and run without dry-run to push the domain."
                ),
            }
        else:
            nic = NICClient()
            nic_status = nic.register_or_update_domain(
                domain=result.domain,
                customer_name=payload.customer_name or result.username,
                email=payload.email or settings.SMTP_FROM_EMAIL,
                phone=payload.phone or "+975",
                address=payload.address or "Thimphu, Bhutan",
                postalcode=payload.postal_code,
                country=payload.country or "BT",
                reg_date=payload.renewal_date,
            )

    return {
        "success": True,
        "message": result.message,
        "data": {
            "panel": result.panel,
            "domain": result.domain,
            "username": result.username,
            "password": result.password,
            "email": result.email,
            "web_url": result.web_url,
            "sftp_host": result.sftp_host,
            "sftp_port": result.sftp_port,
            "doc_root": result.doc_root,
            "nameservers": result.nameservers,
            "handover_text": result.handover_text,
            "email_status": email_status,
            "nic_status": nic_status
        }
    }


@app.post("/api/v1/nic/test", dependencies=[Depends(require_api_token)])
async def test_nic_portal():
    """Test login & access to nic.bt.bt registry portal."""
    nic = NICClient()
    ok, msg = nic.login()
    return {"success": ok, "message": msg, "portal_url": settings.NIC_URL}


class DomainRegisterRequest(BaseModel):
    domain: str
    customer_name: str
    email: str
    phone: Optional[str] = "+975"
    address: Optional[str] = "Thimphu, Bhutan"
    postal_code: Optional[str] = None
    country: Optional[str] = "BT"
    renewal_date: Optional[str] = None

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str) -> str:
        return validate_domain(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return validate_email(v)

    @field_validator("postal_code")
    @classmethod
    def _check_postal_code(cls, v: Optional[str]) -> Optional[str]:
        return validate_postal_code(v) if v else v

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: Optional[str]) -> Optional[str]:
        return validate_country(v) if v else v

    @field_validator("renewal_date")
    @classmethod
    def _check_renewal_date(cls, v: Optional[str]) -> Optional[str]:
        return validate_renewal_date(v) if v else v


@app.post("/api/v1/nic/register", dependencies=[Depends(require_api_token)])
async def register_nic_domain(payload: DomainRegisterRequest):
    """Directly register or update a domain on nic.bt.bt."""
    nic = NICClient()
    res = nic.register_or_update_domain(
        domain=payload.domain,
        customer_name=payload.customer_name,
        email=payload.email,
        phone=payload.phone or "+975",
        address=payload.address or "Thimphu, Bhutan",
        postalcode=payload.postal_code or "-",
        country=payload.country or "BT",
        reg_date=payload.renewal_date,
    )
    return res
