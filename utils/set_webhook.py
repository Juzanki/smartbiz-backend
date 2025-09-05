import requests
import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

def set_webhook():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    payload = {"url": WEBHOOK_URL}
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        print("✅ Webhook set successfully!")
        print(response.json())
    else:
        print("❌ Failed to set webhook.")
        print(response.text)

if __name__ == "__main__":
    set_webhook()
