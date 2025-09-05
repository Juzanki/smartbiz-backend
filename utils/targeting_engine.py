from sqlalchemy.orm import Session
from backend.models.customer import Customer
from backend.models.order import Order
from backend.models.smart_tags import Tag
from backend.models.message import MessageLog
from backend.schemas.targeting import TargetingCriteria
from sqlalchemy import func

def filter_customers(db: Session, criteria: TargetingCriteria):
    query = db.query(Customer)

    if criteria.regions:
        query = query.filter(Customer.region.in_(criteria.regions))

    if criteria.tags:
        query = query.join(Customer.tags).filter(Tag.name.in_(criteria.tags))

    if criteria.last_purchase_after:
        subquery = (
            db.query(Order.customer_id)
            .filter(Order.created_at >= criteria.last_purchase_after)
            .subquery()
        )
        query = query.filter(Customer.id.in_(subquery))

    if criteria.has_replied is not None:
        subquery = (
            db.query(MessageLog.customer_id)
            .filter(MessageLog.is_reply == True)
            .subquery()
        )
        if criteria.has_replied:
            query = query.filter(Customer.id.in_(subquery))
        else:
            query = query.filter(~Customer.id.in_(subquery))

    return query.distinct().all()
