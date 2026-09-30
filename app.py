import secrets
import logging
from datetime import date
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
from notifier import send_customer_welcome_email, test_smtp_connection, send_forwarding_confirmation, forwarding_subject, forwarding_text, correction_subject, correction_body, send_forwarding_correction
from nic_client import (
    NICClient, REGISTRY_FIELDS, build_registry_payload, missing_registry_fields,
    registry_field_spec, split_domain_ext,
)
from bscs_client import BSCSClient, BSCSError
import activity
import domain_service
import ssl_service
from domain_service import AWAITING_DNS, NOTIFIED, VERIFIED
from dns_check import (
    FORWARD_KINDS, FORWARDED, check_domain, check_forwarding, lookup_records,
)
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

logger = logging.getLogger("app")


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
    ext: Optional[str] = Field(None, description="Registry extension (.bt, .com.bt, ...). Defaults to derived from the domain.")
    nic_fields: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The full set of values nic.bt.bt requires, keyed by the registry's "
            "own field names (customername, tech_fax, billing_email, ...). "
            "Blank or omitted entries fall back to the derivation the client "
            "applies. Unknown keys are rejected."
        ),
    )
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

    @field_validator("nic_fields")
    @classmethod
    def _check_nic_fields(cls, v: Dict[str, str]) -> Dict[str, str]:
        """
        Reject registry field names the registry does not have.

        The client drops unknown keys rather than forwarding them, which is
        right for safety but wrong for feedback: a mistyped name would simply
        vanish and the operator would believe a value had been sent. Failing
        here names the mistake instead.
        """
        known = {f["name"] for f in REGISTRY_FIELDS}
        unknown = sorted(set(v or {}) - known)
        if unknown:
            raise ValueError(
                f"Unknown nic.bt.bt field(s): {', '.join(unknown)}. "
                f"Valid names: {', '.join(sorted(known))}"
            )
        return {k: str(val).strip() for k, val in (v or {}).items()}


@app.get("/", response_class=HTMLResponse)
def serve_dashboard(request: Request):
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


@app.get("/registry-fields", response_class=HTMLResponse)
def serve_registry_fields(request: Request):
    """
    Reference page for the 23 fields nic.bt.bt requires.

    This was a read-only mirror of the provisioning form's own fields, sitting
    directly beneath them. It duplicated every value the operator had just
    typed, and it buried the one useful thing it offered -- the full list of
    registry requirements -- inside the middle of the form they were filling in.

    It is a separate page now. The provisioning form is the single place
    registry details are captured; this page only shows what is required and
    where each value comes from, and resolves the two computed fields against
    any domain typed into it.
    """
    return templates.TemplateResponse(request, "registry_fields.html", {})


@app.get("/api/v1/health")
def health_check():
    return {"status": "ok", "service": "automation_webservice_bt"}


@app.get("/api/v1/generate-credentials", dependencies=[Depends(require_api_token)])
def generate_credentials(domain: Optional[str] = None):
    pwd = generate_secure_password(16)
    user = sanitize_username(domain) if domain else ""
    return {"suggested_username": user, "suggested_password": pwd}


@app.get("/api/v1/packages", dependencies=[Depends(require_api_token)])
def get_packages(panel: str = "cpanel"):
    if panel in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
        return {"panel": "cpanel", "packages": prov.list_packages()}
    elif panel in ("directadmin", "da"):
        prov = get_da_provisioner()
        return {"panel": "directadmin", "packages": prov.list_packages()}
    return {"panel": panel, "packages": ["default"]}


@app.get("/api/v1/servers/status", dependencies=[Depends(require_api_token)])
def check_servers():
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
def test_smtp(recipient: Optional[str] = None):
    """Test SMTP connection to Zimbra/mail server, optionally sending a test email."""
    ok, msg = test_smtp_connection(recipient=recipient)
    return {"success": ok, "message": msg, "host": settings.SMTP_HOST, "port": settings.SMTP_PORT}


@app.post("/api/v1/accounts/create", dependencies=[Depends(require_api_token)])
def create_account(payload: AccountCreateRequest):
    panel = payload.panel.lower().strip()
    if panel in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
    elif panel in ("directadmin", "da"):
        prov = get_da_provisioner()
    else:
        raise HTTPException(status_code=400, detail=f"Invalid panel '{payload.panel}'. Must be 'cpanel' or 'directadmin'.")

    # Validate the registry fields before the account exists.
    #
    # This used to run after create_account, which was wrong twice over: it
    # reported a failure for an account that had already been created, inviting
    # a retry that would collide, and it read payload.postal_code, a field the
    # form stopped sending when the 23 registry fields replaced the old eight.
    # Ticking the registry option therefore always failed.
    if payload.register_nic and not payload.dry_run:
        base, derived_ext = split_domain_ext(payload.domain)
        resolved = build_registry_payload(
            payload.nic_fields, base, payload.ext or derived_ext,
            payload.renewal_date or date.today().strftime("%Y-%m-%d"),
        )
        missing = missing_registry_fields(resolved)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "nic.bt.bt requires these fields, which are empty: "
                    + ", ".join(missing)
                    + ". Fill them in on the form, or untick the registry option."
                ),
            )

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
                ext=payload.ext,
                fields=payload.nic_fields,
            )


    # The result was shown once on the page and then existed only in the
    # container's stdout, which rotates. This is what makes "what have we done"
    # answerable after a reload.
    activity.record_event(
        activity.PROVISIONED,
        f"Provisioned {result.domain} on {activity.panel_label(result.panel)} "
        f"as {result.username}",
        panel=result.panel, username=result.username, domain=result.domain,
        web_url=result.web_url, sftp_host=result.sftp_host,
        registry=("updated" if (nic_status or {}).get("action") == "updated"
                  else ("registered" if (nic_status or {}).get("action") == "created"
                        else "not requested")),
        dry_run=payload.dry_run,
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
            "nic_status": nic_status,
            "post_create": ([] if payload.dry_run
                            else run_post_create_steps(result)),
        }
    }


def run_post_create_steps(result) -> list:
    """
    The per-panel steps that must follow a successful account creation.

    Each is reported separately and none can fail the account. An account that
    exists but cannot SFTP is a problem an operator can see and fix; an account
    that exists while the API reports failure invites a retry, and a retry
    creates a second account for the same domain.

    Skipped on a dry run, which creates nothing.
    """
    steps = []
    if result.panel == "cpanel":
        prov = get_cpanel_provisioner()
        steps.append(prov.enable_ipv6(result.username))
    elif result.panel == "directadmin":
        prov = get_da_provisioner()
        steps.append(prov.allow_sftp_user(result.username))
    # Last, and only when the domain already points here. A certificate is the
    # one step that is actively harmful to attempt blindly: Let's Encrypt fails
    # for a domain that does not resolve to this server, and the failures count
    # against a rate limit shared by every customer on the box. See
    # ssl_service for the full reasoning.
    steps.append(ssl_service.enable_ssl(result.panel, result.username, result.domain))
    for step in steps:
        status = step.get("status") or ("ok" if step.get("success") else "failed")
        if step.get("success"):
            logger.info("Post-create %s for %s: %s",
                        step.get("step"), result.username, step.get("message"))
        elif status in (ssl_service.SKIPPED, ssl_service.UNSUPPORTED):
            # Not a failure and nobody's fault. A domain that does not point here
            # yet, or a panel without a licence, is a fact about the world rather
            # than a mistake, and warning about it trains people to ignore warnings.
            logger.info("Post-create %s not done for %s: %s",
                        step.get("step"), result.username, step.get("message"))
        else:
            # Deliberately a warning, not an exception: the account is created.
            logger.warning("Post-create %s FAILED for %s: %s",
                           step.get("step"), result.username, step.get("message"))
    return steps


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
def preview_surrender_endpoint(payload: SurrenderPreviewRequest):
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
def create_surrender(
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
def list_surrenders(limit: int = 100):
    """List recent surrender records, newest first."""
    return {"surrenders": list_audits(limit=max(1, min(limit, 500)))}


@app.get("/api/v1/surrenders/{surrender_id}/evidence", dependencies=[Depends(require_api_token)])
def download_surrender_evidence(surrender_id: str):
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
def bscs_test_connection():
    """Verify BSCS portal reachability and authentication. Read-only."""
    client = get_bscs_client()
    return client.test_connection()


@app.post("/api/v1/bscs/customers/search", dependencies=[Depends(require_api_token)])
def bscs_search_customers(payload: BscsSearchRequest):
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
def bscs_search_contracts(payload: BscsSearchRequest):
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
def suspension_report():
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
    # The record is a snapshot taken at the last run, so an account suspended
    # from the dashboard afterwards still shows as a candidate until the next
    # nightly run -- a finished job looking permanently outstanding. Re-read the
    # live state of each candidate (there are only ever a handful) so the
    # dashboard reflects what is true now, not what was true at 02:17.
    candidates = []
    for d in rec.get("decisions", []):
        if d.get("action") != SUSPEND:
            continue
        entry = dict(d)
        entry["live_suspended"] = None
        entry["live_reason"] = ""
        try:
            panel = (d.get("panel") or "").lower()
            if panel in ("cpanel", "whm"):
                prov = get_cpanel_provisioner()
            elif panel in ("directadmin", "da"):
                prov = get_da_provisioner()
            else:
                prov = None
            if prov is not None:
                state = prov.account_state(d.get("username", ""))
                if state is not None:
                    entry["live_suspended"] = bool(state.get("suspended"))
                    entry["live_reason"] = state.get("reason", "")
        except Exception as e:  # An unreachable panel must not break the report.
            logger.warning("Live state check failed for %s/%s: %s",
                           d.get("panel"), d.get("username"), e)
        candidates.append(entry)

    outstanding = [c for c in candidates if not c["live_suspended"]]

    return {
        "available": True,
        "generated_at": generated,
        "age_hours": age_hours,
        "stale": bool(age_hours is not None and age_hours > 26),
        "total_accounts": rec.get("total_accounts", 0),
        "bscs_complete": rec.get("bscs_complete", True),
        "bscs_note": rec.get("bscs_note", ""),
        # Outstanding right now, verified against the panel rather than the
        # snapshot.
        "candidates": outstanding,
        # Everything the run flagged, with its current state attached, so one
        # acted on since the run reads as done rather than silently vanishing.
        "candidates_all": candidates,
        # Accounts the last run found suspended for billing. These are the
        # candidates for reactivation once payment arrives. Not live-checked
        # here -- there are often over a hundred and each check is a round trip
        # to a panel; the activate endpoint re-reads the account before acting,
        # exactly as the suspend endpoint does, so a stale entry cannot cause a
        # wrong action.
        "activatable": [
            {"panel": d.get("panel", ""), "username": d.get("username", ""),
             "domain": d.get("domain", ""), "contract": d.get("contract", ""),
             "reason": d.get("reason", "")}
            for d in rec.get("decisions", [])
            if d.get("action") == SKIP_ALREADY_BILLING
        ],
        "already_suspended_billing": counts.get(SKIP_ALREADY_BILLING, 0),
        "suspended_other_reason": counts.get(SKIP_OTHER_REASON, 0),
        "no_match": counts.get(SKIP_NO_MATCH, 0),
        "unmatched_contracts": rec.get("unmatched_contracts", []),
    }


@app.post("/api/v1/suspension/suspend", dependencies=[Depends(require_token_for_destructive)])
def suspend_account_now(
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

    activity.record_event(
        activity.SUSPENDED,
        f"Suspended {username} on {activity.panel_label(panel_norm)} for {reason}",
        panel=panel_norm, username=username, domain=state.get("domain", ""),
        reason=reason,
    )
    return {"success": True, "message": result.get("message", ""),
            "panel": panel_norm, "username": username, "domain": state.get("domain", "")}


@app.post("/api/v1/suspension/activate", dependencies=[Depends(require_token_for_destructive)])
def activate_account_now(
    panel: str = Form(...),
    username: str = Form(...),
    confirm: bool = Form(False),
):
    """
    Re-activate one account suspended for billing, after re-checking its state.

    Refuses anything suspended for a different reason. This is the important
    part: an account taken down for abuse, spam or compromise must not come
    back because someone paid an invoice. Only a billing suspension is lifted,
    and the reason is re-read from the panel rather than taken from the
    nightly report, so an account suspended for something else since the report
    was written is still refused.
    """
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Activation is live: send confirm=true.")

    try:
        username = validate_username(username)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    panel_norm = panel.lower().strip()
    if panel_norm in ("cpanel", "whm"):
        prov = get_cpanel_provisioner()
    elif panel_norm in ("directadmin", "da"):
        prov = get_da_provisioner()
    else:
        raise HTTPException(status_code=400, detail="Invalid panel.")

    state = prov.account_state(username)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"Account '{username}' was not found on {panel_norm}.")
    if not state["suspended"]:
        # Already up. Not an error, so a double-click cannot raise an alarm.
        activity.record_event(
            activity.ACTIVATED,
            f"{username} on {activity.panel_label(panel_norm)} was already active",
            outcome=activity.OK, panel=panel_norm, username=username,
            domain=state.get("domain", ""), no_change=True,
        )
        return {"success": True, "already_active": True, "panel": panel_norm,
                "username": username, "domain": state.get("domain", ""),
                "message": f"'{username}' is not suspended. Nothing to do."}
    if (state.get("reason") or "").strip().lower() != "billing":
        # Recorded because it is the answer to "who tried to bring back an
        # account suspended for abuse". A feed that only kept successes would
        # hide exactly the event worth noticing.
        activity.record_event(
            activity.ACTIVATED,
            f"REFUSED activating {username} on {activity.panel_label(panel_norm)} "
            f"— suspended for '{state.get('reason') or 'unrecorded'}', not billing",
            outcome=activity.REFUSED, panel=panel_norm, username=username,
            domain=state.get("domain", ""),
            suspended_for=state.get("reason", ""),
        )
        raise HTTPException(
            status_code=409,
            detail=(f"Refusing to activate '{username}': it is suspended for "
                    f"'{state.get('reason') or 'an unrecorded reason'}', not billing. "
                    f"A payment does not clear that. Reactivation here is only for "
                    f"billing suspensions."),
        )

    result = prov.activate_account(username, confirm=True)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("message", "Activation failed."))

    activity.record_event(
        activity.ACTIVATED,
        f"Activated {username} on {activity.panel_label(panel_norm)} — billing paid",
        panel=panel_norm, username=username, domain=state.get("domain", ""),
        suspended_for=state.get("reason", ""),
    )
    return {"success": True, "already_active": bool(result.get("already_active")),
            "message": result.get("message", ""),
            "panel": panel_norm, "username": username, "domain": state.get("domain", "")}


@app.get("/api/v1/activity", dependencies=[Depends(require_api_token)])
def list_activity(limit: int = 50, kind: Optional[str] = None):
    """
    What has been done, newest first.

    The handover kit shows the result of the action you just took and the domain
    queue shows what is still outstanding; neither survives a reload, and a
    provisioning left no durable record at all. This is the cross-cutting feed
    for "what have we done", and it does not replace the surrender, suspension
    or domain-service trails, which remain the formal records.
    """
    kinds = [k.strip() for k in kind.split(",") if k.strip()] if kind else None
    return {"events": activity.recent(limit=limit, kinds=kinds),
            "counts": activity.counts()}


@app.get("/api/v1/dns/records", dependencies=[Depends(require_api_token)])
def dns_records(domain: str, kind: str = "a"):
    """
    What a domain's DNS says right now, for the given kind.

    Used while filling the forwarding form in, so the current nameservers or
    address are read off the screen rather than typed from memory. A mistyped
    nameserver would fail every later check and look exactly like the
    forwarding never having been done.
    """
    domain = (domain or "").strip()
    if not domain:
        raise HTTPException(status_code=422, detail="A domain is required.")
    return lookup_records(domain, kind=kind)


@app.get("/api/v1/nic/extensions", dependencies=[Depends(require_api_token)])
def nic_extensions():
    """
    The extensions nic.bt.bt accepts, read from the registry's own dropdown.

    The dashboard builds its extension selector from this, so the operator can
    only pick something the registry will take. Cached for a few minutes; an
    empty list means the portal could not be read, and the caller shows the
    field as unavailable rather than guessing.
    """
    opts = NICClient.list_extensions()
    return {
        "extensions": opts,
        "available": bool(opts),
        "message": "" if opts else
                   "Could not read the extension list from nic.bt.bt. "
                   "The extension will be derived from the domain name instead.",
    }


@app.get("/api/v1/dns/check", dependencies=[Depends(require_api_token)])
def dns_check(
    domain: str,
    panel: str = "cpanel",
    probe: bool = True,
):
    """
    Whether a domain's DNS points at the server that hosts it.

    Reported after a provisioning because a created account and a reachable
    domain are different things: the account can be correct while the A record
    still points nowhere, and the operator has no other way to see that.

    Resolves DNS rather than sending ICMP, which is commonly filtered and would
    report "unreachable" for a domain that is in fact mapped correctly.
    """
    domain = (domain or "").strip()
    if not domain:
        raise HTTPException(status_code=422, detail="A domain is required.")
    try:
        return check_domain(domain, panel=panel, probe=probe)
    except Exception as e:
        # Informational only. A failure to look up DNS must be reported as a
        # failed lookup, not as a failed request, so nothing that depends on
        # this endpoint can be broken by it.
        logger.warning("DNS check for %s could not be completed: %s", domain, e)
        return {
            "domain": domain, "status": "error", "resolved": [], "ours": [],
            "web_answers": None,
            "message": "The DNS check could not be completed. The hosting "
                       "account itself is unaffected.",
        }


@app.get("/api/v1/nic/field-spec", dependencies=[Depends(require_api_token)])
def nic_field_spec():
    """
    Every field nic.bt.bt requires, with where each value comes from.

    Lets the dashboard render the registry form from the same list the client
    submits, so the operator can see -- and override -- the Bhutan Telecom
    technical and billing details that used to be hardcoded and invisible.
    """
    return registry_field_spec()


@app.post("/api/v1/nic/test", dependencies=[Depends(require_api_token)])
def test_nic_portal():
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
    ext: Optional[str] = Field(None, description="Registry extension (.bt, .com.bt, ...). Defaults to derived from the domain.")
    nic_fields: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The full set of values nic.bt.bt requires, keyed by the registry's "
            "own field names (customername, tech_fax, billing_email, ...). "
            "Blank or omitted entries fall back to the derivation the client "
            "applies. Unknown keys are rejected."
        ),
    )

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

    @field_validator("nic_fields")
    @classmethod
    def _check_nic_fields(cls, v: Dict[str, str]) -> Dict[str, str]:
        """
        Reject registry field names the registry does not have.

        The client drops unknown keys rather than forwarding them, which is
        right for safety but wrong for feedback: a mistyped name would simply
        vanish and the operator would believe a value had been sent. Failing
        here names the mistake instead.
        """
        known = {f["name"] for f in REGISTRY_FIELDS}
        unknown = sorted(set(v or {}) - known)
        if unknown:
            raise ValueError(
                f"Unknown nic.bt.bt field(s): {', '.join(unknown)}. "
                f"Valid names: {', '.join(sorted(known))}"
            )
        return {k: str(val).strip() for k, val in (v or {}).items()}


@app.post("/api/v1/nic/register", dependencies=[Depends(require_api_token)])
def register_nic_domain(payload: DomainRegisterRequest):
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
        ext=payload.ext,
        fields=payload.nic_fields,
    )
    return res

# ---------------------------------------------------------------------------
# Domain services
#
# A domain is registered on nic.bt.bt first, and the customer then chooses
# hosting or forwarding. Forwarding is done by BT staff by hand, in systems this
# service does not touch: nic.bt.bt holds no DNS or nameserver records, and .bt
# delegation belongs to ns1/ns2.druknet.bt. So the service's part is to record
# what was asked for, verify it against real DNS, and only then let an operator
# tell the customer.
# ---------------------------------------------------------------------------

class DomainOnlyRequest(BaseModel):
    """Just a domain. Verify and notify read everything else from the record."""
    domain: str


class DomainServiceRequest(BaseModel):
    domain: str
    customer_name: str
    email: str
    # The full nic.bt.bt field set, keyed by the registry's own field names.
    nic_fields: Dict[str, str] = Field(default_factory=dict)
    service: str = Field("forwarding", description="hosting or forwarding")
    forwarding_kind: Optional[str] = Field(None, description="a or nameserver")
    forwarding_target: Optional[str] = Field(None, description="The address, or the comma-separated nameservers, it was pointed at.")


@app.get("/api/v1/domain-services", dependencies=[Depends(require_api_token)])
def list_domain_services(status: Optional[str] = None):
    """Every domain service, newest change first, optionally filtered by status."""
    entries = (domain_service.list_by_status(status) if status
               else domain_service.current_states())
    return {"services": entries, "count": len(entries)}


@app.post("/api/v1/domain-services/register", dependencies=[Depends(require_api_token)])
def register_domain_service(payload: DomainServiceRequest):
    """
    Register a domain on nic.bt.bt and record what the customer asked for.

    The registry write happens first. The record is written after it succeeds,
    so the log never claims a registration that did not happen, and a failure
    here is reported as a registry failure rather than a bookkeeping one.
    """
    try:
        domain = domain_service.normalise_domain(payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    service = (payload.service or "").strip().lower()
    if service not in domain_service.VALID_SERVICE:
        raise HTTPException(status_code=422,
                            detail=f"service must be one of: "
                                   f"{', '.join(domain_service.VALID_SERVICE)}")

    kind = (payload.forwarding_kind or "").strip().lower()
    target = (payload.forwarding_target or "").strip()
    if service == "forwarding":
        if kind not in FORWARD_KINDS:
            raise HTTPException(status_code=422,
                                detail=f"forwarding_kind must be one of: "
                                       f"{', '.join(FORWARD_KINDS)}")
        if not target:
            raise HTTPException(
                status_code=422,
                detail="forwarding_target is required: the address, or the "
                       "nameservers, the domain was pointed at. Without it the "
                       "forwarding cannot be verified later.")

    nic = NICClient()
    result = nic.register_or_update_domain(
        domain=domain,
        customer_name=payload.customer_name,
        email=payload.email,
        ext=(payload.nic_fields or {}).get("ext"),
        fields=payload.nic_fields or {},
    )
    if not result.get("success"):
        raise HTTPException(status_code=502,
                            detail=result.get("message", "nic.bt.bt rejected the registration."))

    activity.record_event(
        activity.DOMAIN_REGISTERED,
        f"Registered {domain} on nic.bt.bt for {payload.customer_name} "
        f"({'forwarding' if service == 'forwarding' else 'hosting'})",
        domain=domain, customer=payload.customer_name, service=service,
        forwarding_kind=kind, forwarding_target=target,
    )
    entry = domain_service.record_registration(
        domain=domain,
        customer_name=payload.customer_name,
        email=payload.email,
        service=service,
        forwarding_kind=kind,
        forwarding_target=target,
        registry_action=result.get("action", ""),
        registry_message=result.get("message", ""),
    )
    return {"success": True, "service": entry, "nic": result}


@app.post("/api/v1/domain-services/verify", dependencies=[Depends(require_api_token)])
def verify_domain_service(payload: DomainOnlyRequest):
    """
    Check whether a forwarding has actually been done, and record the answer.

    Separate from registering so it can be re-run as often as the operator
    likes: forwarding is done by hand and takes however long it takes.
    """
    try:
        domain = domain_service.normalise_domain(payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    state = domain_service.get_state(domain)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"No domain service is recorded for '{domain}'.")
    if state.get("service") != "forwarding":
        raise HTTPException(status_code=400,
                            detail=f"'{domain}' is recorded as {state.get('service')}, "
                                   f"which has no forwarding to verify.")
    if state.get("status") == NOTIFIED:
        # Still allow the check, but say clearly that the customer has been told
        # and a later mismatch is worth acting on.
        pass

    result = check_forwarding(domain, state.get("forwarding_kind", ""),
                              state.get("forwarding_target", ""))
    entry = domain_service.record_verification(domain, result)
    return {"success": True, "check": result, "service": entry}


class DomainNotifyRequest(BaseModel):
    domain: str
    # The operator's own wording, if they changed it. Empty means the house text.
    subject: Optional[str] = None
    body: Optional[str] = None


class DomainCorrectRequest(BaseModel):
    domain: str
    # What was wrong with the first email. Required: the customer is receiving a
    # second message about the same thing, and an unexplained one is worse than
    # a wrong one.
    reason: str
    # The address may be wrong too, not only the wording.
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


@app.post("/api/v1/domain-services/correct",
          dependencies=[Depends(require_token_for_destructive)])
def correct_domain_notification(payload: DomainCorrectRequest):
    """
    Send a second email about a domain the customer has already been told about.

    The notify endpoint refuses this with a 409, which is right: nobody should
    be able to double-send by accident. But an email that was genuinely wrong
    cannot be recalled, and this leaves no other route. So a correction is a
    separate, deliberate act with its own endpoint, and it must say what was
    wrong.

    The DNS check is repeated here rather than trusted from the first send. The
    original was justified by a check that passed then; a second claim that the
    domain is forwarded is a new claim about the present, and forwarding can
    lapse. Correcting a typo about an address that has since stopped resolving
    would be telling the customer something false twice.

    Neither email is deleted or rewritten. The customer holds both, so the
    record holds both.
    """
    try:
        domain = domain_service.normalise_domain(payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    reason = (payload.reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(
            status_code=422,
            detail="Say what was wrong with the first email. The customer is "
                   "getting a second message and needs to know which to trust.")

    state = domain_service.get_state(domain)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"No domain service is recorded for '{domain}'.")
    first = state.get("notification")
    if not first:
        raise HTTPException(
            status_code=409,
            detail=f"'{domain}' has not been emailed yet, so there is nothing "
                   f"to correct. Send the confirmation first.")
    if (state.get("sends") or [])[-1].get("correction"):
        raise HTTPException(
            status_code=409,
            detail=f"The last email for '{domain}' was already a correction "
                   f"({(state.get('sends') or [])[-1].get('at', '')}). One "
                   f"correction is the record; send another only if this is a "
                   f"separate new problem.")

    # Re-check. The original was justified by a check that passed at the time.
    result = check_forwarding(domain, state.get("forwarding_kind", ""),
                              state.get("forwarding_target", ""))
    if result.get("status") != "forwarded":
        raise HTTPException(
            status_code=409,
            detail=(f"Refusing to send a correction: '{domain}' is no longer "
                    f"forwarded as described ({result.get('message')}). Fix the "
                    f"forwarding first, then send a correction that says what is "
                    f"true."))

    recipient = (payload.recipient or "").strip() or state.get("email", "")
    kind = state.get("forwarding_kind", "")
    target = state.get("forwarding_target", "")
    observed = result.get("observed") or state.get("observed", [])
    final_subject = ((payload.subject or "").strip() or correction_subject(domain))
    # The framing is added here and cannot be replaced: an operator editing the
    # correction must not be able to produce an email that reads like a first one.
    final_body = correction_body(domain, reason, kind, target, observed, payload.body)

    sent, message = send_forwarding_correction(
        domain=domain, email=recipient, kind=kind, target=target,
        observed=observed, subject=final_subject, body=final_body, reason=reason)

    entry = domain_service.record_notification(
        domain, sent, message, subject=final_subject, body=final_body,
        recipient=recipient, kind=kind, edited=bool((payload.body or "").strip()),
        correction=True, reason=reason)
    if not sent:
        raise HTTPException(status_code=502, detail=message)
    return {"success": True, "message": message, "service": entry,
            "sent": entry.get("notification", {}),
            "corrects": first.get("at", "")}


class DomainForwardingUpdate(BaseModel):
    kind: str
    target: str


@app.patch("/api/v1/domain-services/{domain}/forwarding",
           dependencies=[Depends(require_api_token)])
def update_domain_forwarding(domain: str, payload: DomainForwardingUpdate):
    """
    Correct the nameserver or address a forwarding was supposed to use.

    A pasted name server list is easy to get wrong -- two hosts joined by a
    dot instead of a comma reads as one hostname that can never match, and the
    domain then sits on "mismatch" forever with no way to say so. This is the
    way to fix it without surrendering the registration and starting again.

    The parsed result is returned alongside, so the operator can see what was
    actually understood rather than finding out from the next check.
    """
    try:
        entry = domain_service.update_forwarding(domain, payload.kind, payload.target)
    except ValueError as e:
        # A rule, not a crash: an already-notified domain, a hosted one, or an
        # unusable target. All are things the operator needs to be told plainly.
        raise HTTPException(status_code=409, detail=str(e))

    from dns_check import split_target
    return {
        "service": entry,
        "will_check_as": split_target(entry.get("forwarding_target", "")),
        "message": f"The forwarding for {entry['domain']} is now checked against "
                   f"{', '.join(split_target(entry.get('forwarding_target', '')))}. "
                   f"Press Check to confirm it.",
    }


class DomainManualSend(BaseModel):
    recipient: str
    note: str = ""
    # Optional, so a note written up after the email can say when it actually went.
    at: str = ""


@app.post("/api/v1/domain-services/{domain}/manual-send",
          dependencies=[Depends(require_api_token)])
def record_domain_manual_send(domain: str, payload: DomainManualSend):
    """
    Record that the customer was emailed by hand, so they are not emailed again.

    Not a workaround for the notify guard and not a way to fake a send. The
    recorded copy says it is a note and not the email, because the wording went
    out from a mail client and this system never saw it. What it buys is the one
    thing that matters here: the row stops offering to send a second copy of news
    a customer has already had.
    """
    try:
        entry = domain_service.record_manual_send(
            domain, payload.recipient, payload.note, payload.at)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "service": entry,
            "sent": entry.get("notification", {}),
            "message": f"Noted: {entry['domain']} was emailed by hand to "
                       f"{payload.recipient.strip()}. No further email will be sent."}


@app.get("/api/v1/domain-services/{domain}", dependencies=[Depends(require_api_token)])
def get_domain_service(domain: str):
    """
    One recorded domain service, including the email that was sent to the
    customer if it has been.

    The list endpoint is enough to work the queue, but not to answer "what did
    we actually tell them" -- and after a send the only copy is the one kept
    here.
    """
    try:
        domain = domain_service.normalise_domain(domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    state = domain_service.get_state(domain)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"No domain service is recorded for '{domain}'.")
    return state


@app.post("/api/v1/domain-services/email-preview", dependencies=[Depends(require_api_token)])
def preview_domain_email(payload: DomainOnlyRequest):
    """
    The email as it would be sent, without sending it.

    So the wording can be read, and changed, before it reaches a customer. The
    default is the format BT already uses, with the check output in it, so what
    the customer reads is the same evidence the operator was shown.
    """
    try:
        domain = domain_service.normalise_domain(payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    state = domain_service.get_state(domain)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"No domain service is recorded for '{domain}'.")

    observed = state.get("observed") or []
    if not observed:
        # Without a check there is nothing truthful to put in the body, and an
        # empty record section would read as a forwarding that went nowhere.
        result = check_forwarding(domain, state.get("forwarding_kind", ""),
                                  state.get("forwarding_target", ""))
        observed = result.get("observed") or []
    return {
        "domain": domain,
        "to": state.get("email", ""),
        "subject": forwarding_subject(domain),
        "body": forwarding_text(domain, state.get("forwarding_kind", ""),
                                state.get("forwarding_target", ""), observed),
        "verified": bool(observed),
        "status": state.get("status", ""),
    }


@app.post("/api/v1/domain-services/notify", dependencies=[Depends(require_token_for_destructive)])
def notify_domain_service(payload: DomainNotifyRequest):
    """
    Email the customer that their domain has been forwarded.

    Refused unless a DNS check has already confirmed the forwarding. This is the
    only outward-facing step in the flow, and the email asserts something
    factual about the public internet -- so the check is a precondition, not a
    suggestion. Otherwise a customer is told their domain is live while it still
    points nowhere.
    """
    try:
        domain = domain_service.normalise_domain(payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    state = domain_service.get_state(domain)
    if state is None:
        raise HTTPException(status_code=404,
                            detail=f"No domain service is recorded for '{domain}'.")
    if state.get("service") != "forwarding":
        raise HTTPException(status_code=400,
                            detail=f"'{domain}' is recorded as {state.get('service')}. "
                                   f"There is no forwarding to notify about.")
    if not state.get("email"):
        raise HTTPException(status_code=422,
                            detail=f"No customer email is recorded for '{domain}', "
                                   f"so there is nobody to notify.")
    if state.get("status") == NOTIFIED:
        raise HTTPException(status_code=409,
                            detail=f"'{domain}' was already notified on "
                                   f"{state.get('notified_at')}. It has not been emailed again.")

    # The gate. A status of verified only ever comes from a successful check.
    if state.get("status") != VERIFIED:
        last = state.get("last_check_message") or "it has not been checked yet"
        raise HTTPException(
            status_code=409,
            detail=(f"Refusing to notify: the forwarding for '{domain}' is not "
                    f"verified ({last}). Run the DNS check first. Emailing a "
                    f"customer that their domain is live, when it is not, is "
                    f"worse than not emailing them at all."))

    # Work out what is about to go out, so the same text is recorded as is sent.
    # Re-deriving it afterwards would risk the record and the message disagreeing
    # if the default text ever changed.
    kind = state.get("forwarding_kind", "")
    final_subject = (payload.subject or "").strip() or forwarding_subject(domain)
    final_body = ((payload.body or "").strip()
                  or forwarding_text(domain, kind, state.get("forwarding_target", ""),
                                     state.get("observed", [])))

    sent, message = send_forwarding_confirmation(
        domain=domain,
        email=state.get("email", ""),
        kind=kind,
        target=state.get("forwarding_target", ""),
        observed=state.get("observed", []),
        # The operator's wording if they supplied any. The verified values are
        # still what the check found; only the prose is theirs to change.
        subject=final_subject,
        body=final_body,
    )
    entry = domain_service.record_notification(
        domain, sent, message,
        subject=final_subject, body=final_body,
        recipient=state.get("email", ""), kind=kind,
        edited=bool((payload.body or "").strip()))
    if not sent:
        raise HTTPException(status_code=502, detail=message)
    # Returned so the operator can see what went out immediately, rather than
    # only finding out that it did.
    return {"success": True, "message": message, "service": entry,
            "sent": entry.get("notification", {})}
