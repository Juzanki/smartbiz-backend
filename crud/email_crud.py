from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.email_template import EmailTemplate
from backend.schemas.email_template import EmailTemplateCreate
from datetime import datetime

def create_email_template(db: Session, template: EmailTemplateCreate):
    db_template = EmailTemplate(
        name=template.name,
        subject=template.subject,
        html_content=template.html_content,
        created_at=datetime.utcnow()
    )
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template

def get_template_by_name(db: Session, name: str):
    return db.query(EmailTemplate).filter(EmailTemplate.name == name).first()

def get_all_templates(db: Session):
    return db.query(EmailTemplate).order_by(EmailTemplate.created_at.desc()).all()

