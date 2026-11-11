"""Run the Telegram bot locally WITHOUT a public URL (long polling).

Use this for the demo on your laptop:   python run_telegram_polling.py
In production use the webhook instead (see README: "Telegram webhook").
"""
import asyncio
import logging

import httpx

from app.config import settings
from app.telegram import API, handle_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copilot")


async def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    base = f"{API}/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=40) as client:
        await client.post(f"{base}/deleteWebhook")  # polling and webhook can't both be active
        log.info("Bot is listening. Send it a link or a screenshot. Ctrl+C to stop.")
        offset = 0
        while True:
            try:
                resp = await client.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 30})
                updates = resp.json().get("result", [])
            except httpx.HTTPError as exc:
                log.warning("Polling error: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                asyncio.create_task(handle_update(update))


if __name__ == "__main__":
    asyncio.run(main())
