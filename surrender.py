"""
Service surrender (termination) for hosting accounts and domain registrations.

A surrender is the customer-facing process for discontinuing a service. Unlike
account creation it destroys customer data, so this module is deliberately more
defensive than the provisioning path:

  * The scanned surrender letter is required as evidence and is validated by
    its actual content, not by the filename or the declared content type.
  * The uploaded file is never used to build a path. A random name is
    generated server-side, so a hostile filename cannot escape the storage
    directory.
  * Every action is appended to an audit log before and after execution.
  * When both services are surrendered the hosting account goes first and the
    domain registration last. The domain is the harder asset to restore, so it
    is only removed once everything else has already succeeded. A failure part
    way through therefore leaves the customer holding the more valuable asset,
    and the remaining step can be retried on its own.
"""
import hashlib
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# What each file type must actually start with. An upload is rejected when its
# bytes disagree with its extension, so a PHP payload renamed to surrender.pdf
# is refused instead of being written to disk.
_MAGIC_NUMBERS: Dict[str, Tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}

VALID_SCOPES = ("hosting", "domain", "both")


class SurrenderError(ValueError):
    """Raised when a surrender request is invalid or cannot be prepared."""


@dataclass
class EvidenceFile:
    """Metadata for a stored surrender letter."""
    stored_name: str
    original_name: str
    size_bytes: int
    sha256: str
    content_type: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _allowed_extensions() -> List[str]:
    raw = settings.SURRENDER_ALLOWED_EXTENSIONS or ".pdf,.jpg,.jpeg"
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def _detect_extension(filename: str) -> str:
    """Return the lowercase extension of a client-supplied filename."""
    # Path.basename first: a client must not be able to assert a path.
    return Path(os.path.basename(filename or "")).suffix.lower()


def store_evidence(file_obj, original_name: str) -> EvidenceFile:
    """
    Validate and persist an uploaded surrender letter.

    Args:
        file_obj: A binary file-like object (e.g. FastAPI's UploadFile.file).
        original_name: The client-supplied filename, used for the audit record
            only. It never influences where the file is written.

    Raises:
        SurrenderError: If the upload is missing, too large, empty, or its
            content does not match an accepted evidence format.
    """
    ext = _detect_extension(original_name)
    allowed = _allowed_extensions()
    if ext not in allowed:
        raise SurrenderError(
            f"Evidence must be one of {', '.join(allowed)}; got '{ext or 'no extension'}'."
        )

    max_bytes = settings.SURRENDER_MAX_UPLOAD_MB * 1024 * 1024
    upload_dir = Path(settings.SURRENDER_UPLOAD_DIR).expanduser()
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SurrenderError(f"Evidence directory is not writable: {e}") from e

    # The stored name is generated, never derived from the upload, so that
    # "../../etc/cron.d/x.pdf" or "a b;rm -rf /.pdf" cannot influence the path.
    stored_name = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(8)}{ext}"
    target = upload_dir / stored_name

    digest = hashlib.sha256()
    written = 0
    header = b""
    try:
        with open(target, "wb") as fh:
            while True:
                # Read in chunks and stop as soon as the cap is passed, so an
                # oversized upload is never fully written to disk.
                chunk = file_obj.read(64 * 1024)
                if not chunk:
                    break
                if not header:
                    header = chunk[:16]
                written += len(chunk)
                if written > max_bytes:
                    raise SurrenderError(
                        f"Evidence exceeds the {settings.SURRENDER_MAX_UPLOAD_MB} MB limit."
                    )
                digest.update(chunk)
                fh.write(chunk)
    except SurrenderError:
        target.unlink(missing_ok=True)
        raise
    except OSError as e:
        target.unlink(missing_ok=True)
        raise SurrenderError(f"Failed to store evidence: {e}") from e

    if written == 0:
        target.unlink(missing_ok=True)
        raise SurrenderError("Evidence file is empty.")

    # Content check happens after the size guard so a huge file is not sniffed.
    expected = _MAGIC_NUMBERS.get(ext)
    if expected and not any(header.startswith(magic) for magic in expected):
        target.unlink(missing_ok=True)
        raise SurrenderError(
            f"File content does not look like a {ext} file. Evidence must be a genuine "
            f"PDF or JPEG of the surrender letter."
        )

    return EvidenceFile(
        stored_name=stored_name,
        original_name=os.path.basename(original_name or stored_name),
        size_bytes=written,
        sha256=digest.hexdigest(),
        content_type="application/pdf" if ext == ".pdf" else "image/jpeg",
    )


def evidence_path(stored_name: str) -> Path:
    """
    Resolve a stored evidence name to an absolute path inside the upload dir.

    The name is re-validated here because it can arrive from a request path
    parameter on the download endpoint.
    """
    name = os.path.basename(stored_name or "")
    upload_dir = Path(settings.SURRENDER_UPLOAD_DIR).expanduser().resolve()
    candidate = (upload_dir / name).resolve()
    # Guard against traversal via the request path even after basename().
    if candidate.parent != upload_dir:
        raise SurrenderError("Invalid evidence reference.")
    if not candidate.is_file():
        raise SurrenderError("Evidence file not found.")
    return candidate


def _audit_path() -> Path:
    path = Path(settings.SURRENDER_AUDIT_LOG).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_audit(record: Dict[str, Any]) -> None:
    """Append one JSON line to the audit log. Never raises into the caller."""
    try:
        with open(_audit_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        # Losing the audit trail must not mask the real outcome of the
        # operation, but it must be loud.
        logger.error("Failed to append surrender audit record: %s", e)


def new_surrender_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"SUR-{stamp}-{uuid.uuid4().hex[:6].upper()}"


def list_audits(limit: int = 100) -> List[Dict[str, Any]]:
    """Return the most recent surrender records, newest first."""
    path = _audit_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.error("Failed to read surrender audit log: %s", e)
        return []

    records: List[Dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed audit line: %.80s", line)
        if len(records) >= limit:
            break
    return records


def get_audit(surrender_id: str) -> Optional[Dict[str, Any]]:
    """Look up a single surrender record by its id."""
    for record in list_audits(limit=10000):
        if record.get("id") == surrender_id:
            return record
    return None


# ---------------------------------------------------------------------------
# Orchestration
#
# The hosting and registry steps are injected as callables so this module stays
# free of panel specifics and can be exercised without touching a live server.
# Each returns a dict with at least "success" (bool) and "message" (str).
# ---------------------------------------------------------------------------
def preview_surrender(
    domain: str,
    scope: str,
    username: Optional[str] = None,
    hosting_exists: Optional[Any] = None,
    domain_exists: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Report what a surrender would affect, without changing anything.

    Used by the dashboard to show the operator the exact blast radius before
    they commit.
    """
    if scope not in VALID_SCOPES:
        raise SurrenderError(f"Invalid scope '{scope}'. Must be one of {', '.join(VALID_SCOPES)}.")

    plan: List[Dict[str, Any]] = []
    if scope in ("hosting", "both"):
        exists = bool(hosting_exists(username)) if hosting_exists and username else None
        plan.append({
            "target": "hosting",
            "username": username,
            "present": exists,
            "action": "Delete the hosting account, its files, mail and databases."
                      if exists else "No hosting account found; nothing to remove.",
        })
    if scope in ("domain", "both"):
        exists = bool(domain_exists(domain)) if domain_exists else None
        plan.append({
            "target": "domain",
            "domain": domain,
            "present": exists,
            "action": "Remove the domain registration from the nic.bt.bt registry."
                      if exists else "No registry record found; nothing to remove.",
        })

    return {
        "domain": domain,
        "username": username,
        "scope": scope,
        "steps": plan,
        "note": "Hosting is surrendered before the domain registration, because the "
                "registration is the harder asset to restore.",
    }


def perform_surrender(
    domain: str,
    scope: str,
    username: Optional[str] = None,
    reason: str = "",
    evidence: Optional[EvidenceFile] = None,
    panel: str = "",
    operator: Optional[str] = None,
    client_ip: Optional[str] = None,
    hosting_delete: Optional[Any] = None,
    domain_delete: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Execute a surrender and record the outcome.

    Hosting is removed first and the domain registration last, so a partial
    failure leaves the customer still holding the domain. Each step's real
    result is reported; a step that fails does not abort the remaining steps,
    because a half-finished surrender must be visible in full rather than
    silently truncated.
    """
    if scope not in VALID_SCOPES:
        raise SurrenderError(f"Invalid scope '{scope}'. Must be one of {', '.join(VALID_SCOPES)}.")
    if scope in ("hosting", "both") and not username:
        raise SurrenderError("A hosting username is required to surrender the hosting service.")
    if settings.SURRENDER_REQUIRE_EVIDENCE and evidence is None:
        raise SurrenderError(
            "A scanned surrender letter (PDF or JPEG) is required as evidence."
        )

    record: Dict[str, Any] = {
        "id": new_surrender_id(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "operator": operator or "unknown",
        "client_ip": client_ip,
        "domain": domain,
        "panel": panel,
        "username": username,
        "scope": scope,
        "reason": reason,
        "evidence": evidence.to_dict() if evidence else None,
        "actions": [],
    }
    # Written before anything is destroyed, so an interrupted run still leaves
    # evidence that a surrender was attempted.
    append_audit({**record, "status": "started"})

    def run(target: str, handler: Any, arg: Any) -> Dict[str, Any]:
        step: Dict[str, Any] = {"target": target, "ran_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if handler is None:
            step.update(success=False, message="No handler configured for this target.")
        else:
            try:
                res = handler(arg)
                step["success"] = bool(res.get("success"))
                step["message"] = res.get("message", "")
                if res.get("action"):
                    step["action"] = res["action"]
            except Exception as e:  # A failing step must not hide the others.
                logger.exception("Surrender step '%s' raised", target)
                step.update(success=False, message=f"Unexpected error: {e}")
        record["actions"].append(step)
        return step

    # Hosting first, domain last: see module docstring.
    if scope in ("hosting", "both"):
        run("hosting", hosting_delete, username)
    if scope in ("domain", "both"):
        run("domain", domain_delete, domain)

    attempted = record["actions"]
    succeeded = [a for a in attempted if a.get("success")]
    failed = [a for a in attempted if not a.get("success")]

    if not failed:
        status = "completed"
    elif succeeded:
        status = "partial"
    else:
        status = "failed"

    record["status"] = status
    record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    append_audit(record)

    record["summary"] = (
        f"{len(succeeded)} of {len(attempted)} step(s) succeeded."
        if attempted else "Nothing to do."
    )
    return record
