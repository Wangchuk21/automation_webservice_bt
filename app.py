import secrets
from fastapi import FastAPI, HTTPException, Request, Depends, Header, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
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
from nic_client import NICClient, split_domain_ext
from bscs_client import BSCSClient, BSCSError
from suspension import (
    SKIP_ALREADY_BILLING, SKIP_NO_MATCH, SKIP_OTHER_REASON, SUSPEND, latest_report,
)
from surrender import (
    SurrenderError,
    perform_surrender,
    preview_surrender,
    store_evidence,
    list_audits,
    get_audit,
    evidence_path,
)

app = FastAPI(
    title="Automation WebService BT",
    description="Automated Shared Hosting Provisioning for cPanel and DirectAdmin",
    version="1.0.0"
)


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    """
    Force the browser to revalidate the dashboard and its assets.

    Without an explicit Cache-Control, browsers apply heuristic freshness from
    Last-Modified and can serve a stale app.js after a redeploy. The symptom is
    a dashboard that renders new markup but whose JavaScript never runs the
    matching code, leaving panels stuck on "Loading..." with no error anywhere.
    Cheap to revalidate, and this is an internal ops tool where showing stale
    state would be actively misleading.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static") or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
def _verify_api_token(x_api_token: Optional[str]) -> None:
    """
    Compare a presented token against the configured one.

    Kept separate from the FastAPI dependency wrappers below: calling a
    dependency function directly would pass its `Header(...)` default object
    rather than a string, which blows up inside compare_digest.
    """
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


def require_api_token(x_api_token: Optional[str] = Header(None, alias="X-API-Token")) -> None:
    _verify_api_token(x_api_token)

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

def require_token_for_destructive(x_api_token: Optional[str] = Header(None, alias="X-API-Token")) -> None:
    """
    Fail closed for any operation that destroys customer data.

    The rest of the API stays open when API_AUTH_TOKEN is unset, which is
    convenient for local work but unacceptable for surrender: an instance
    reachable on the network would let anyone destroy live hosting accounts and
    domain registrations. So this requires the token to be configured AND
    correctly presented, regardless of the open-API development default.
    """
    if not settings.API_AUTH_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "Surrender is disabled because API_AUTH_TOKEN is not set. "
                "Set API_AUTH_TOKEN in .env and restart; destroying customer "
                "services must never run unauthenticated."
            ),
        )
    _verify_api_token(x_api_token)


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


# ---------------------------------------------------------------------------
# Service surrender (termination)
#
# A surrender removes a hosting account and/or a domain registration at the
# customer's request. Unlike the provisioning endpoints these are gated by
# require_token_for_destructive(), so they cannot run unauthenticated even when
# the rest of the API is open for development.
# ---------------------------------------------------------------------------
class SurrenderPreviewRequest(BaseModel):
    domain: str
    username: Optional[str] = None
    panel: str = "cpanel"
    scope: str = "both"

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str) -> str:
        return validate_domain(v)

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: Optional[str]) -> Optional[str]:
        return validate_username(v) if v else v


@app.post("/api/v1/surrenders/preview", dependencies=[Depends(require_api_token)])
async def preview_surrender_endpoint(payload: SurrenderPreviewRequest):
    """
    Report what a surrender would remove, without changing anything.

    Read-only, so it uses the normal token gate: an operator should be able to
    inspect a customer's services before deciding, including during setup.
    """
    panel = payload.panel.lower().strip()

    def hosting_exists(username: str) -> bool:
        if panel in ("cpanel", "whm"):
            return get_cpanel_provisioner().account_exists(username)
        if panel in ("directadmin", "da"):
            return get_da_provisioner().account_exists(username)
        return False

    def domain_exists(domain: str) -> bool:
        return NICClient().find_domain_id(*split_domain_ext(domain)) is not None

    try:
        return preview_surrender(
            domain=payload.domain,
            scope=payload.scope,
            username=payload.username,
            hosting_exists=hosting_exists,
            domain_exists=domain_exists,
        )
    except SurrenderError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/surrenders", dependencies=[Depends(require_token_for_destructive)])
async def create_surrender(
    request: Request,
    domain: str = Form(...),
    scope: str = Form("both"),
    panel: str = Form("cpanel"),
    username: Optional[str] = Form(None),
    reason: str = Form(""),
    confirm: bool = Form(False),
    evidence: Optional[UploadFile] = File(None),
):
    """
    Surrender the hosting account, the domain registration, or both.

    Requires an explicit confirm=true and, unless disabled in configuration, a
    scanned surrender letter as evidence. The evidence is stored and hashed, and
    the whole operation is written to the audit log.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Surrender is destructive and must be confirmed: send confirm=true.",
        )

    try:
        domain = validate_domain(domain)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        if username:
            username = validate_username(username)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    panel_norm = panel.lower().strip()
    if panel_norm not in ("cpanel", "whm", "directadmin", "da"):
        raise HTTPException(status_code=400, detail=f"Invalid panel '{panel}'.")

    prov = get_cpanel_provisioner() if panel_norm in ("cpanel", "whm") else get_da_provisioner()

    # Validate and persist the evidence before anything is destroyed, so a
    # rejected upload cannot leave a half-completed surrender behind.
    stored_evidence = None
    if evidence is not None and evidence.filename:
        try:
            stored_evidence = store_evidence(evidence.file, evidence.filename)
        except SurrenderError as e:
            raise HTTPException(status_code=422, detail=str(e))
    elif settings.SURRENDER_REQUIRE_EVIDENCE:
        raise HTTPException(
            status_code=422,
            detail="A scanned surrender letter (PDF or JPEG) is required as evidence.",
        )

    reason_text = (reason or "").strip() or "Service surrender"

    def hosting_delete(user: str) -> Dict[str, Any]:
        return prov.delete_account(user, reason=reason_text, confirm=True)

    def domain_delete(dom: str) -> Dict[str, Any]:
        return NICClient().delete_domain(dom, confirm=True)

    try:
        record = perform_surrender(
            domain=domain,
            scope=scope,
            username=username,
            reason=reason_text,
            evidence=stored_evidence,
            panel=panel_norm,
            operator=request.headers.get("X-Operator", "api"),
            client_ip=request.client.host if request.client else None,
            hosting_delete=hosting_delete,
            domain_delete=domain_delete,
        )
    except SurrenderError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # A partial or failed surrender is reported as an error status so a caller
    # scripting against this cannot mistake it for a clean completion.
    status_code = 200 if record.get("status") == "completed" else 409
    return JSONResponse(status_code=status_code, content=record)


@app.get("/api/v1/surrenders", dependencies=[Depends(require_api_token)])
async def list_surrenders(limit: int = 100):
    """List recent surrender records, newest first."""
    return {"surrenders": list_audits(limit=max(1, min(limit, 500)))}


@app.get("/api/v1/surrenders/{surrender_id}/evidence", dependencies=[Depends(require_api_token)])
async def download_surrender_evidence(surrender_id: str):
    """Download the surrender letter attached to a record."""
    record = get_audit(surrender_id)
    if not record or not record.get("evidence"):
        raise HTTPException(status_code=404, detail="No evidence found for that surrender.")

    try:
        path = evidence_path(record["evidence"]["stored_name"])
    except SurrenderError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return FileResponse(
        path,
        media_type=record["evidence"].get("content_type", "application/octet-stream"),
        filename=record["evidence"].get("original_name") or path.name,
    )


# ---------------------------------------------------------------------------
# Ericsson BSCS / CBiO CX (READ-ONLY reference)
#
# Lookup only: confirm a customer exists and read their contract/billing state
# as reference. There is deliberately no endpoint here that writes to BSCS --
# we hold no rights to change contracts or VAS packages, so the capability is
# not offered at all rather than offered and refused.
#
# These use POST even for searches, because a customer name or ID in a query
# string is captured by proxy and access logs; a request body is not.
# ---------------------------------------------------------------------------
def get_bscs_client():
    """Build a BSCS client, or explain why it is unavailable."""
    if not settings.BSCS_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="BSCS integration is disabled. Set BSCS_ENABLED=true in .env to enable it.",
        )
    if not settings.BSCS_BASE_URL or not settings.BSCS_USERNAME:
        raise HTTPException(
            status_code=503,
            detail="BSCS is not configured. Set BSCS_BASE_URL and BSCS_USERNAME in .env.",
        )
    try:
        return BSCSClient()
    except BSCSError as e:
        raise HTTPException(status_code=503, detail=str(e))


class BscsSearchRequest(BaseModel):
    """Search criteria. Any one field is required; an empty search is refused
    because it would pull the entire customer index out of the billing system.
    """
    customer_id: Optional[str] = None
    customer_code: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[str] = None
    document_id: Optional[str] = None

    def criteria(self) -> Dict[str, Any]:
        """
        Non-blank criteria only.

        Values are stripped, because a whitespace-only string is truthy: without
        this, a search of {"full_name": "   "} would pass the "at least one
        criterion" check and be sent to the billing system as a broad query.
        """
        out: Dict[str, Any] = {}
        for key, value in self.model_dump().items():
            if isinstance(value, str):
                value = value.strip()
            if value:
                out[key] = value
        return out


@app.post("/api/v1/bscs/test", dependencies=[Depends(require_api_token)])
async def bscs_test_connection():
    """Verify BSCS portal reachability and authentication. Read-only."""
    client = get_bscs_client()
    return client.test_connection()


@app.post("/api/v1/bscs/customers/search", dependencies=[Depends(require_api_token)])
async def bscs_search_customers(payload: BscsSearchRequest):
    """Search the BSCS customer index. Read-only; nothing is modified."""
    criteria = payload.criteria()
    if not criteria:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one criterion: customer_id, customer_code, "
                   "full_name, first_name, last_name, status or document_id.",
        )
    return get_bscs_client().search_customers(**criteria)


@app.post("/api/v1/bscs/contracts/search", dependencies=[Depends(require_api_token)])
async def bscs_search_contracts(payload: BscsSearchRequest):
    """Search contracts for a customer. Read-only; nothing is modified."""
    criteria = payload.criteria()
    customer_id = criteria.pop("customer_id", None)
    if not customer_id:
        raise HTTPException(status_code=422, detail="customer_id is required to search contracts.")
    return get_bscs_client().search_contracts(customer_id, **criteria)


# ---------------------------------------------------------------------------
# Suspension review
#
# The nightly job detects and records; an operator acts from the dashboard.
# That split is deliberate. The BSCS domain join is name-based and only about
# half of lapsed contracts resolve to an account, so acting automatically
# would sometimes be wrong. Detection is cheap and safe to automate; the
# decision is not.
#
# The safety rules are enforced HERE, server-side, not in the browser. A crafted
# request cannot suspend an account that is already suspended, and can never
# overwrite a non-billing reason.
# ---------------------------------------------------------------------------
class SuspendRequest(BaseModel):
    """Declared for documentation and for the CLI; the HTTP endpoint takes form
    fields so the dashboard can post multipart without constructing JSON."""

    panel: str = Field(..., description="'cpanel' or 'directadmin'")
    username: str = Field(..., description="Hosting account name")
    confirm: bool = Field(False, description="Must be true to act")
    reason: str = Field("billing", description="Reason recorded on the panel")

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        return validate_username(v)


@app.get("/api/v1/suspension/report", dependencies=[Depends(require_api_token)])
async def suspension_report():
    """
    The most recent detection run: candidates, already-suspended counts, and
    the lapsed contracts that could NOT be matched to an account.
    """
    rec = latest_report()
    if not rec:
        return {
            "available": False,
            "message": "No suspension run has been recorded yet. The scheduled job "
                       "has not completed, or BSCS was not reachable.",
        }
    counts = rec.get("counts", {})
    # Age the report. A run that failed (BSCS unreachable, VPN down) writes no
    # record at all, so without this the dashboard would happily show a
    # week-old list as if it were current -- and someone could act on it.
    generated = rec.get("generated_at")
    age_hours = None
    if generated:
        try:
            from datetime import datetime as _dt
            gen = _dt.fromisoformat(generated)
            if gen.tzinfo is None:
                gen = gen.astimezone()
            age_hours = round((_dt.now(gen.tzinfo) - gen).total_seconds() / 3600.0, 1)
        except (ValueError, TypeError):
            age_hours = None
    return {
        "available": True,
        "generated_at": generated,
        "age_hours": age_hours,
        "stale": bool(age_hours is not None and age_hours > 26),
        "total_accounts": rec.get("total_accounts", 0),
        "bscs_complete": rec.get("bscs_complete", True),
        "bscs_note": rec.get("bscs_note", ""),
        "candidates": [d for d in rec.get("decisions", []) if d.get("action") == SUSPEND],
        "already_suspended_billing": counts.get(SKIP_ALREADY_BILLING, 0),
        "suspended_other_reason": counts.get(SKIP_OTHER_REASON, 0),
        "no_match": counts.get(SKIP_NO_MATCH, 0),
        "unmatched_contracts": rec.get("unmatched_contracts", []),
    }


@app.post("/api/v1/suspension/suspend", dependencies=[Depends(require_token_for_destructive)])
async def suspend_account_now(
    panel: str = Form(...),
    username: str = Form(...),
    confirm: bool = Form(False),
    reason: str = Form("billing"),
):
    """
    Suspend one account, after re-checking its state on the panel.

    The state is re-read here rather than trusted from the report, so an
    account that was suspended manually since the last run -- for abuse, spam
    or anything else -- is refused rather than relabelled as billing.
    """
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Suspension is destructive: send confirm=true.")

    try:
        username = validate_username(username)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    panel_norm = panel.lower().strip()
    if panel_norm in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
        state = prov.account_state(username)
    elif panel_norm in ("directadmin", "da"):
        prov = get_da_provisioner()
        state = prov.account_state(username)
    else:
        raise HTTPException(status_code=400, detail="Invalid panel.")

    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"Account '{username}' was not found on {panel_norm}.")

    if state["suspended"]:
        # This is the safety rule that protects the abuse cases: an account
        # already suspended keeps its reason and is never relabelled.
        raise HTTPException(
            status_code=409,
            detail=(f"'{username}' is already suspended "
                    f"(reason: {state['reason'] or 'not recorded'}). Left untouched."),
        )

    result = prov.suspend_account(username, reason=reason, confirm=True)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("message", "Suspension failed."))

    return {"success": True, "message": result.get("message", ""),
            "panel": panel_norm, "username": username, "domain": state.get("domain", "")}


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
