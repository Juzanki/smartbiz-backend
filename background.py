import asyncio
import logging

# Kazi ya kuendelea kukimbia kwa background kila dakika
async def start_background_tasks():
    logging.info("🚀 Background tasks started...")

    while True:
        logging.info("⏱ Still running background tasks...")
        # Hapa unaweza kuongeza kazi nyingine: cron sync, cleanup, metrics, etc
        await asyncio.sleep(60)
