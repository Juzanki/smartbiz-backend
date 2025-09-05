# === smartinject_bridge.py ===
# 📍 Weka huu file ndani ya SmartBiz_Assistance/backend/services/
import os
import httpx
import logging

SMARTINJECT_URL = os.getenv("SMARTINJECT_URL", "http://127.0.0.1:8010")
ADMIN_SECRET = os.getenv("ADMIN_SECRET_KEY", "super-secret")

headers = {"X-ADMIN-KEY": ADMIN_SECRET}

async def notify_kernel(prompt: str):
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"{SMARTINJECT_URL}/observe",
                json={"prompt": prompt},
                headers=headers
            )
            res.raise_for_status()
            logging.info("🧠 Kernel notified successfully")
    except Exception as e:
        logging.error(f"❌ Failed to notify kernel: {str(e)}")


async def execute_kernel():
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(f"{SMARTINJECT_URL}/execute", headers=headers)
            res.raise_for_status()
            logging.info("🧠 Kernel executed successfully")
    except Exception as e:
        logging.error(f"❌ Kernel execution failed: {str(e)}")


async def scan_kernel_vision():
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{SMARTINJECT_URL}/vision/scan", headers=headers)
            res.raise_for_status()
            data = res.json()
            logging.info(f"📷 Vision scan: {data}")
    except Exception as e:
        logging.error(f"❌ Vision scan failed: {str(e)}")


async def kernel_health():
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{SMARTINJECT_URL}/health", headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logging.error(f"⚠️ Kernel health check failed: {str(e)}")
        return {"status": "unreachable"}
