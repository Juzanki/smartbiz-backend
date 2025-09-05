from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models import Subscription
from backend.models.subscription import SubscriptionPlan
from backend.schemas.subscription import PlanCreate

# --- Create Plan ---


def create_plan(db: Session, plan: PlanCreate) -> SubscriptionPlan:
    db_plan = SubscriptionPlan(**plan.dict())
    db.add(db_plan)
    db.commit()
    db.refresh(db_plan)
    return db_plan

# --- Get All Plans ---


def get_all_plans(db: Session) -> List[SubscriptionPlan]:
    return db.query(SubscriptionPlan).order_by(
        SubscriptionPlan.price.asc()).all()

# --- Get User Active Subscription ---


def get_user_subscription(db: Session, user_id: int) -> Subscription:
    return db.query(Subscription).filter(
        Subscription.user_id == user_id,
        Subscription.is_active).first()


