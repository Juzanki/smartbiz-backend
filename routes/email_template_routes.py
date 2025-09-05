from __future__ import annotations
# backend/routes/email_templates.py
 backend.schemas.user import UserOut

import hashlib
from typing import Optional, List, Dict, Any
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Response, Header, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User

# -------- Schemas (tumia zako; zipo fallback ndogo kama hazijapatikana) -------
try:
    from backend.schemas.email_template import (
        EmailTemplateCreate, EmailTemplateOut, EmailTemplateUpdate, EmailTemplatePreviewRequest
    )
except Exception:
    from pydantic import BaseModel, Field

    class EmailTemplateCreate(BaseModel):
        name: str = Field(..., min_length=3, max_length=80)
        subject: str = Field(..., min_length=1, max_length=180)
        body_html: str = Field(..., min_length=1)
        body_text: Optional[str] = None
        description: Optional[str] = None
        tags: Optional[List[str]] = None

    class EmailTemplateUpdate(BaseModel):
        subject: Optional[str] = None
        body_html: Optional[str] = None
        body_text: Optional[str] = None
        description: Optional[str] = None
        tags: Optional[List[str]] = None

    class EmailTemplateOut(EmailTemplateCreate):
        id: int
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        deleted_at: Optional[datetime] = None

    class EmailTemplatePreviewRequest(BaseModel):
        variables: Dict[str, Any] = {}

# -------- CRUD / Model (tumia CRUD zako kama zipo; vinginevyo ORM) -----------
with suppress(Exception):
    from backend.crud import email_crud as _crud
CRUD_CREATE = getattr(_crud, "create_email_template", None) if "_crud" in globals() else None
CRUD_LIST   = getattr(_crud, "get_all_templates", None) if "_crud" in globals() else None
CRUD_GET    = getattr(_crud, "get_template_by_id", None) if "_crud" in globals() else None
CRUD_UPDATE = getattr(_crud, "update_email_template", None) if "_crud" in globals() else None
CRUD_DELETE = getattr(_crud, "delete_email_template", None) if "_crud" in globals() else None

EmailTemplateModel = None
with suppress(Exception):
    from backend.models.email_template import EmailTemplate as EmailTemplateModel  # type: ignore

router = APIRouter(prefix="/email-templates", tags=["Email Templates"])

# -------- Helpers --------
def _require_admin(current_user: User = Depends(get_current_user)) -> UserOut:
    if current_user.role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admins only")
    return current_user

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_from(name: str, subject: str, body_html: str, updated_at: Any = "") -> str:
    base = f"{name}|{subject}|{hashlib.sha256(body_html.encode('utf-8')).hexdigest()}|{updated_at}"
    return 'W/"' + hashlib.sha256(base.encode("utf-8")).hexdigest()[:16] + '"'

def _serialize_one(row: Any) -> EmailTemplateOut:
    if hasattr(EmailTemplateOut, "model_validate"):
        return EmailTemplateOut.model_validate(row, from_attributes=True)  # pyd v2
    return EmailTemplateOut.model_validate(row)  # pyd v1

# ===================== CREATE (idempotent) =====================
@router.post(
    "",
    response_model=EmailTemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Unda email template (Admin only, idempotent)"
)
def create_template(
    template: EmailTemplateCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # Idempotency by (name + checksum) within 15m could be extended via Redis; hapa no-op if identical
    checksum = hashlib.sha256(
        f"{template.name}|{template.subject}|{template.body_html}".encode("utf-8")
    ).hexdigest()

    if EmailTemplateModel:
        existing = (
            db.query(EmailTemplateModel)
            .filter(func.lower(EmailTemplateModel.name) == template.name.lower())
            .first()
        )
        if existing:
            # same content? return existing; else 409
            existing_sum = hashlib.sha256(
                f"{existing.name}|{existing.subject}|{existing.body_html}".encode("utf-8")
            ).hexdigest()
            if existing_sum == checksum:
                response.headers["ETag"] = _etag_from(existing.name, existing.subject, existing.body_html, getattr(existing, "updated_at", ""))
                response.headers["Cache-Control"] = "no-store"
                return _serialize_one(existing)
            raise HTTPException(status_code=409, detail="Template name already exists")

    if CRUD_CREATE:
        row = CRUD_CREATE(db, template)
    elif EmailTemplateModel:
        row = EmailTemplateModel(**template.dict())
        if hasattr(row, "created_at") and not getattr(row, "created_at", None):
            row.created_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)
    else:
        raise HTTPException(status_code=500, detail="Email template storage not configured")

    response.headers["ETag"] = _etag_from(row.name, row.subject, row.body_html, getattr(row, "updated_at", ""))
    response.headers["Cache-Control"] = "no-store"
    return _serialize_one(row)

# ===================== LIST (paged + search + sort) =====================
@router.get(
    "",
    response_model=List[EmailTemplateOut],
    summary="Orodha ya templates (Admin only, pagination + search + sorting)"
)
def list_templates(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
    q: Optional[str] = Query(None, description="Tafuta kwa name/subject/tags"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("updated_at", description="name|created_at|updated_at"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    include_deleted: bool = Query(False),
):
    if EmailTemplateModel:
        qry = db.query(EmailTemplateModel)
        if q:
            like = f"%{q}%"
            qry = qry.filter(
                (EmailTemplateModel.name.ilike(like)) |
                (EmailTemplateModel.subject.ilike(like)) |
                (getattr(EmailTemplateModel, "tags_json", func.cast("", func.TEXT)).ilike(like))
            )
        if not include_deleted and hasattr(EmailTemplateModel, "deleted_at"):
            qry = qry.filter(EmailTemplateModel.deleted_at.is_(None))

        sort_whitelist = {
            "name": EmailTemplateModel.name,
            "created_at": getattr(EmailTemplateModel, "created_at", EmailTemplateModel.name),
            "updated_at": getattr(EmailTemplateModel, "updated_at", EmailTemplateModel.name),
        }
        order_col = sort_whitelist.get(sort_by, getattr(EmailTemplateModel, "updated_at", EmailTemplateModel.name))
        qry = qry.order_by(order_col.asc() if order == "asc" else order_col.desc())

        total = qry.count()
        rows = qry.offset(offset).limit(limit).all()
    elif CRUD_LIST:
        rows = CRUD_LIST(db) or []
        total = len(rows)
        # rudimentary in-memory search/sort/page fallback
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in getattr(r, "name", "").lower() or ql in getattr(r, "subject", "").lower()]
        reverse = (order == "desc")
        rows = sorted(rows, key=lambda r: getattr(r, sort_by, getattr(r, "updated_at", None)), reverse=reverse)
        rows = rows[offset: offset + limit]
    else:
        raise HTTPException(status_code=500, detail="Email template listing not configured")

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["Cache-Control"] = "no-store"

    return [_serialize_one(r) for r in rows]

# ===================== GET ONE =====================
@router.get("/{template_id}", response_model=EmailTemplateOut, summary="Pata template moja (Admin only)")
def get_template(
    template_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    if CRUD_GET:
        row = CRUD_GET(db, template_id)
    elif EmailTemplateModel:
        row = db.query(EmailTemplateModel).filter(EmailTemplateModel.id == template_id).first()
    else:
        raise HTTPException(status_code=500, detail="Not configured")

    if not row:
        raise HTTPException(status_code=404, detail="Template not found")

    if response is not None:
        response.headers["ETag"] = _etag_from(row.name, row.subject, row.body_html, getattr(row, "updated_at", ""))
        response.headers["Cache-Control"] = "no-store"

    return _serialize_one(row)

# ===================== UPDATE (If-Match) =====================
@router.put("/{template_id}", response_model=EmailTemplateOut, summary="Sasisha template (optimistic via If-Match)")
def update_template(
    template_id: int,
    payload: EmailTemplateUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    if EmailTemplateModel:
        row = db.query(EmailTemplateModel).filter(EmailTemplateModel.id == template_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")

        # optimistic
        current_tag = _etag_from(row.name, row.subject, row.body_html, getattr(row, "updated_at", ""))
        if if_match and if_match != current_tag:
            raise HTTPException(status_code=412, detail="ETag mismatch (template changed)")

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(row, k, v)
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
    elif CRUD_UPDATE:
        row = CRUD_UPDATE(db, template_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")
    else:
        raise HTTPException(status_code=500, detail="Not configured")

    response.headers["ETag"] = _etag_from(row.name, row.subject, row.body_html, getattr(row, "updated_at", ""))
    response.headers["Cache-Control"] = "no-store"
    return _serialize_one(row)

# ===================== DELETE (soft/hard) =====================
@router.delete("/{template_id}", response_model=dict, summary="Futa template (soft delete ikiwa ina deleted_at)")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    if CRUD_DELETE:
        ok = CRUD_DELETE(db, template_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Template not found")
        return {"detail": "Template deleted"}

    if not EmailTemplateModel:
        raise HTTPException(status_code=500, detail="Not configured")

    row = db.query(EmailTemplateModel).filter(EmailTemplateModel.id == template_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")

    if hasattr(row, "deleted_at"):
        row.deleted_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "Template deleted"}

# ===================== PREVIEW (Jinja2 Strict) =====================
@router.post("/{template_id}/preview", summary="Preview ya template kwa variables (Jinja2 Strict)")
def preview_template(
    template_id: int,
    payload: EmailTemplatePreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    # pata template
    if CRUD_GET:
        row = CRUD_GET(db, template_id)
    elif EmailTemplateModel:
        row = db.query(EmailTemplateModel).filter(EmailTemplateModel.id == template_id).first()
    else:
        raise HTTPException(status_code=500, detail="Not configured")

    if not row:
        raise HTTPException(status_code=404, detail="Template not found")

    # render Jinja2 (StrictUndefined â†’ toa error ikiwa variable haipo)
    try:
        from jinja2 import Environment, StrictUndefined, TemplateError, meta
        env = Environment(undefined=StrictUndefined, autoescape=True)
        ast = env.parse(row.body_html or "")
        expected_vars = sorted(list(meta.find_undeclared_variables(ast)))
        tmpl = env.from_string(row.body_html or "")
        html = tmpl.render(**(payload.variables or {}))
        text = (row.body_text or "") and env.from_string(row.body_text).render(**(payload.variables or {}))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Template render error: {str(e)}")

    missing = [v for v in expected_vars if v not in (payload.variables or {})]

    return {
        "template_id": template_id,
        "expected_variables": expected_vars,
        "missing_variables": missing,
        "rendered_html": html,
        "rendered_text": text or None,
    }


