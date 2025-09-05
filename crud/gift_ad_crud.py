from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.gift_transaction import GiftTransaction
from backend.models.ad_earning import AdEarning
from backend.models.wallet import Wallet
from backend.schemas.gift_ad import GiftTransactionCreate, AdEarningCreate
from datetime import datetime

# === GIFT LOGIC ===
def send_gift_and_credit(db: Session, data: GiftTransactionCreate):
    gift = GiftTransaction(
        sender_id=data.sender_id,
        recipient_id=data.recipient_id,
        gift_name=data.gift_name,
        gift_value=data.gift_value,
        created_at=datetime.utcnow()
    )
    db.add(gift)

    # Add smartcoin to recipient wallet
    recipient_wallet = db.query(Wallet).filter(Wallet.user_id == data.recipient_id).first()
    if recipient_wallet:
        recipient_wallet.smartcoin += data.gift_value
        db.commit()
        db.refresh(gift)
        return gift
    else:
        db.rollback()
        raise Exception("Recipient wallet not found")

# === AD LOGIC ===
def credit_ad_earning(db: Session, data: AdEarningCreate):
    ad = AdEarning(
        user_id=data.user_id,
        ad_type=data.ad_type,
        smartcoins_earned=data.smartcoins_earned,
        details=data.details,
        created_at=datetime.utcnow()
    )
    db.add(ad)

    # Add to wallet
    wallet = db.query(Wallet).filter(Wallet.user_id == data.user_id).first()
    if wallet:
        wallet.smartcoin += data.smartcoins_earned
        db.commit()
        db.refresh(ad)
        return ad
    else:
        db.rollback()
        raise Exception("User wallet not found")

