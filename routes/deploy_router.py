from __future__ import annotations
# backend/routes/deploy_router.py
import os
import re
import io
import hashlib
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from contextlib import suppress

from fastapi import APIRouter, HTTPException, status, Header, Depends, Response, Query
from pydantic import BaseModel, Field

# ---------- Auth & RBAC ----------
def _not_configured(*_a, **_k):
    raise HTTPException(status_code=401, detail="Auth not configured")

with suppress(Exception):
    from backend.auth import get_current_user  # must return user obj with .role
if "get_current_user" not in globals():
    get_current_user = _not_configured  # type: ignore

def _require_admin(user=Depends(get_current_user)):
    role = getattr(user, "role", "user")
    if role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin only")
    return user

# ---------- Audit (best-effort) ----------
def _audit(db, **kw):
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(db, **kw)

with suppress(Exception):
    from backend.db import get_db  # optional

router = APIRouter(prefix="/deploy", tags=["Code Deployment"])

# ---------- Config ----------
ROOT_DIR = Path(__file__).resolve().parents[2]  # project root (…/)
DEPLOY_TOKEN = os.getenv("DEPLOY_TOKEN", "")  # weka kwenye .env / secrets
ENV_MODE = (os.getenv("ENVIRONMENT") or "development").strip().lower()

MAX_FILE_MB = float(os.getenv("DEPLOY_MAX_FILE_MB", "2"))
MAX_FILE_BYTES = int(MAX_FILE_MB * 1024 * 1024)

# Whitelist ya files tunazoruhusu
FILE_MAP: Dict[str, str] = {
    "backend:fibonacci": "backend/routes/ai_functions.py",
    "backend:orders":    "backend/routes/orders.py",
    # ongeza zingine hapa...
}

START_FMT = "# ==== GPT_INSERT_START [{tag}] ===="
END_FMT   = "# ==== GPT_INSERT_END [{tag}] ===="

# ---------- Schemas ----------
class InjectRequest(BaseModel):
    tag: str = Field(..., min_length=3, max_length=64)
    code: str = Field(..., min_length=1)
    dry_run: bool = False
    create_if_missing: bool = True
    backup: bool = True
    encoding: Optional[str] = Field(None, description="Ikiwa 'base64', code itatafsiriwa kwanza")

class InjectResponse(BaseModel):
    message: str
    file: str
    bytes_before: int
    bytes_after: int
    changed: bool
    etag: str
    backup_path: Optional[str] = None
    diff: Optional[str] = None  # hutolewa ukiomba dry_run

# ---------- Utils ----------
def _within_root(p: Path) -> bool:
    try:
        p.resolve().relative_to(ROOT_DIR)
        return True
    except Exception:
        return False

def _etag_bytes(data: bytes) -> str:
    return 'W/"' + hashlib.sha256(data).hexdigest()[:16] + '"'

def _unified_diff(old: str, new: str, fname: str) -> str:
    import difflib
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"{fname} (old)", tofile=f"{fname} (new)"
        )
    )

def _decode_code(code: str, encoding: Optional[str]) -> str:
    if (encoding or "").lower() == "base64":
        import base64
        return base64.b64decode(code).decode("utf-8", errors="strict")
    return code

def _replace_block(original: str, start_tag: str, end_tag: str, new_body: str) -> str:
    # Regex: pata block kati ya start/end; DOTALL ili ivute mistari mingi
    start_re = re.escape(start_tag)
    end_re = re.escape(end_tag)
    pattern = re.compile(rf"({start_re}\s*)(.*?)(\s*{end_re})", re.DOTALL)
    if pattern.search(original):
        return pattern.sub(rf"\1{new_body}\3", original, count=1)
    # hakuna block; tutarudisha original (mwisho injinia ataamua kuunda)
    return original

def _inject_content(path: Path, tag: str, code: str, create_if_missing: bool, make_backup: bool, dry_run: bool) -> Dict[str, Any]:
    start_tag = START_FMT.format(tag=tag)
    end_tag   = END_FMT.format(tag=tag)

    if not path.exists():
        if not create_if_missing:
            raise HTTPException(status_code=400, detail=f"File '{path}' haipo, weka create_if_missing=true au tengeneza kwanza.")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = f"{start_tag}\n{end_tag}\n"
        path.write_text(original, encoding="utf-8")
    else:
        # kikomo cha ukubwa
        if path.stat().st_size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large (> {MAX_FILE_MB}MB)")

        original = path.read_text(encoding="utf-8", errors="strict")

    if start_tag not in original or end_tag not in original:
        # kisa file mpya bila tags
        if create_if_missing and not original.strip():
            original = f"{start_tag}\n{end_tag}\n"
        else:
            raise HTTPException(status_code=400, detail=f"Missing tag markers for '{tag}' in file.")

    # safisha & weka code
    body = code.strip() + "\n"
    updated = _replace_block(original, start_tag, end_tag, body)

    if original == updated:
        # huenda code mpya = ya sasa
        return {
            "changed": False,
            "bytes_before": len(original.encode("utf-8")),
            "bytes_after": len(updated.encode("utf-8")),
            "backup_path": None,
            "diff": None,
            "etag": _etag_bytes(updated.encode("utf-8")),
        }

    if dry_run:
        return {
            "changed": True,
            "bytes_before": len(original.encode("utf-8")),
            "bytes_after": len(updated.encode("utf-8")),
            "backup_path": None,
            "diff": _unified_diff(original, updated, str(path)),
            "etag": _etag_bytes(updated.encode("utf-8")),
        }

    # backup
    backup_path = None
    if make_backup and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
        shutil.copy2(path, backup_path)

    # atomic write
    tmp = Path(f"{path}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(path)  # atomic on same filesystem

    return {
        "changed": True,
        "bytes_before": len(original.encode("utf-8")),
        "bytes_after": len(updated.encode("utf-8")),
        "backup_path": str(backup_path) if backup_path else None,
        "diff": None,
        "etag": _etag_bytes(updated.encode("utf-8")),
    }

# ---------- Endpoints ----------
@router.get("/files", summary="Orodhesha tags na files")
def list_files():
    return {"root": str(ROOT_DIR), "files": FILE_MAP}

@router.post(
    "/inject-code",
    response_model=InjectResponse,
    summary="🧠 Inject code inside tagged block (atomic + backup + idempotent)"
)
def inject_code(
    data: InjectRequest,
    response: Response,
    db=Depends(get_db) if "get_db" in globals() else None,
    current_user=Depends(_require_admin),
    x_deploy_token: Optional[str] = Header(None, alias="X-Deploy-Token"),
):
    # Token check (mahiri zaidi kwenye production)
    if not DEPLOY_TOKEN:
        raise HTTPException(status_code=500, detail="DEPLOY_TOKEN is not configured")
    if not x_deploy_token or x_deploy_token != DEPLOY_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid deploy token")

    if data.tag not in FILE_MAP:
        raise HTTPException(status_code=404, detail=f"Tag '{data.tag}' not found in file_map")

    file_rel = FILE_MAP[data.tag]
    path = (ROOT_DIR / file_rel).resolve()

    # hard guard: lazima iwe ndani ya project root
    if not _within_root(path):
        raise HTTPException(status_code=400, detail="Resolved path is outside project root")

    # soma & inject
    try:
        code_str = _decode_code(data.code, data.encoding)
        result = _inject_content(
            path=path,
            tag=data.tag,
            code=code_str,
            create_if_missing=data.create_if_missing,
            make_backup=data.backup,
            dry_run=data.dry_run,
        )
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=422, detail=f"Invalid text encoding: {e}") from e

    # headers kwa UX (ETag, Cache-Control)
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = result["etag"]

    # Audit best-effort
    _audit(
        db,
        action="deploy.inject",
        status="success" if result["changed"] else "nochange",
        severity="high" if ENV_MODE == "production" else "info",
        actor_id=getattr(current_user, "id", None),
        actor_email=getattr(current_user, "email", None),
        resource_type="file",
        resource_id=str(path),
        meta={"tag": data.tag, "dry_run": data.dry_run, "bytes_after": result["bytes_after"]},
    )

    return InjectResponse(
        message=("✅ Code injected" if result["changed"] else "ℹ️ No changes (idempotent)"),
        file=str(path),
        bytes_before=result["bytes_before"],
        bytes_after=result["bytes_after"],
        changed=bool(result["changed"]),
        etag=result["etag"],
        backup_path=result.get("backup_path"),
        diff=result.get("diff"),
    )

@router.get(
    "/preview",
    response_model=InjectResponse,
    summary="Angalia diff ya mabadiliko kabla ya kuandika (dry-run)"
)
def preview_injection(
    tag: str = Query(..., min_length=3),
    code: str = Query(..., min_length=1),
    encoding: Optional[str] = Query(None),
    current_user=Depends(_require_admin),
    x_deploy_token: Optional[str] = Header(None, alias="X-Deploy-Token"),
):
    if not DEPLOY_TOKEN or not x_deploy_token or x_deploy_token != DEPLOY_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid deploy token")

    if tag not in FILE_MAP:
        raise HTTPException(status_code=404, detail=f"Tag '{tag}' not found in file_map")

    path = (ROOT_DIR / FILE_MAP[tag]).resolve()
    if not _within_root(path):
        raise HTTPException(status_code=400, detail="Resolved path is outside project root")

    # Hakikisha file lipo; kama halipo, tutaonyesha diff ya kuanzisha block
    if not path.exists():
        start_tag = START_FMT.format(tag=tag)
        end_tag = END_FMT.format(tag=tag)
        base = f"{start_tag}\n{end_tag}\n"
        new_body = _decode_code(code, encoding).strip() + "\n"
        updated = base.replace(f"{start_tag}\n{end_tag}", f"{start_tag}\n{new_body}{end_tag}")
        diff = _unified_diff(base, updated, str(path))
        return InjectResponse(
            message="Dry-run (file does not exist; would be created)",
            file=str(path),
            bytes_before=len(base.encode("utf-8")),
            bytes_after=len(updated.encode("utf-8")),
            changed=True,
            etag=_etag_bytes(updated.encode("utf-8")),
            backup_path=None,
            diff=diff,
        )

    # File ipo; soma na tengeneza diff
    original = path.read_text(encoding="utf-8", errors="strict")
    start_tag = START_FMT.format(tag=tag)
    end_tag = END_FMT.format(tag=tag)
    if start_tag not in original or end_tag not in original:
        raise HTTPException(status_code=400, detail=f"Missing tag markers for '{tag}' in file.")

    body = _decode_code(code, encoding).strip() + "\n"
    updated = _replace_block(original, start_tag, end_tag, body)
    changed = (original != updated)
    diff = _unified_diff(original, updated, str(path))

    return InjectResponse(
        message="Dry-run (no write)",
        file=str(path),
        bytes_before=len(original.encode("utf-8")),
        bytes_after=len(updated.encode("utf-8")),
        changed=changed,
        etag=_etag_bytes(updated.encode("utf-8")),
        backup_path=None,
        diff=diff,
    )
