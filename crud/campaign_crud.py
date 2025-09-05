from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
from backend.models.campaign import Campaign
from backend.schemas.campaign import CampaignRequest

# --- Create Campaign ---


def create_campaign(
        db: Session,
        user_id: int,
        campaign_data: CampaignRequest) -> Campaign:
    campaign = Campaign(
        user_id=user_id,
        product_id=campaign_data.product_id,
        platforms=','.join(campaign_data.platforms),
        media_type=campaign_data.media_type,
        schedule_time=campaign_data.schedule_time,
        created_at=datetime.utcnow()
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign

# --- Get Campaigns by User ---


def get_user_campaigns(db: Session, user_id: int) -> List[Campaign]:
    return db.query(Campaign).filter(Campaign.user_id == user_id).order_by(Campaign["created_at"]desc()).all()


