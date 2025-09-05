from pywebpush import webpush, WebPushException
from backend.models.push_subscription import PushSubscription
import json
import os

# ⚠️ Replace with your actual VAPID keys (you can generate them later)
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "your-public-key")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "your-private-key")
VAPID_CLAIMS = {
    "sub": "mailto:admin@smartbiz.com"
}

def send_push_notification(subscription: PushSubscription, message: str):
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {
                    "p256dh": subscription.p256dh,
                    "auth": subscription.auth
                }
            },
            data=json.dumps({"title": "SmartBiz Notification", "body": message}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
        return True
    except WebPushException as ex:
        print(f"❌ Push failed for {subscription.endpoint}: {repr(ex)}")
        return False
