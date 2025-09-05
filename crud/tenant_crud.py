from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.tenant import Tenant
from backend.schemas.tenant import TenantCreate
from datetime import datetime

def create_tenant(db: Session, tenant: TenantCreate):
    db_tenant = Tenant(
        name=tenant.name,
        slug=tenant.slug,
        domain=tenant.domain,
        created_at=datetime.utcnow()
    )
    db.add(db_tenant)
    db.commit()
    db.refresh(db_tenant)
    return db_tenant

def get_tenant_by_slug(db: Session, slug: str):
    return db.query(Tenant).filter(Tenant.slug == slug).first()

def get_all_tenants(db: Session):
    return db.query(Tenant).all()

