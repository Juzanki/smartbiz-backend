import requests
import hmac
import hashlib
from sqlalchemy.orm import Session
from backend.crud import webhook_crud

def send_webhook(
    db: Session,
    endpoint,
    payload: dict,
    max_retries: int = 3
):
    import json, time

    payload_str = json.dumps(payload)
    headers = {
        "Content-Type": "application/json"
    }

    # Optional: sign payload with secret
    if endpoint.secret:
        signature = hmac.new(
            key=endpoint.secret.encode("utf-8"),
            msg=payload_str.encode("utf-8"),
            digestmod=hashlib.sha256
        ).hexdigest()
        headers["X-Signature"] = signature

    success = False
    response_code = None
    error_message = None

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(endpoint.url, data=payload_str, headers=headers, timeout=10)
            response_code = res.status_code
            if res.status_code >= 200 and res.status_code < 300:
                success = True
                break
            else:
                error_message = f"Non-200 status: {res.status_code}"
        except Exception as e:
            error_message = str(e)

        time.sleep(1)  # Optional delay between retries

    # Log result
    webhook_crud.log_delivery(
        db=db,
        endpoint_id=endpoint.id,
        payload=payload_str,
        response_code=response_code or 0,
        success=success,
        error_message=error_message,
        attempts=attempt
    )

    return success
