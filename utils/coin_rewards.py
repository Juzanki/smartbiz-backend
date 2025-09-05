from backend.models.user import User
from backend.utils.coin_logger import log_smartcoin_transaction

# SmartCoin reward values
REWARD_AMOUNTS = {
    "referral_success": 100,
    "receive_gift": 25,
    "train_model": 10,
    "api_infer_used": 1,
    "upgrade_to_pro": 50
}

def reward_user(db, user: User, event: str, description: str = ""):
    if event not in REWARD_AMOUNTS:
        raise ValueError("Invalid reward event type")

    amount = REWARD_AMOUNTS[event]
    user.coin_balance += amount
    log_smartcoin_transaction(db, user.id, "earn", amount, description or f"Reward for: {event}")
    db.commit()
