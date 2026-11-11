"""Telegram trigger: send the bot a link or a screenshot, get the drafts back in chat.

The same handle_update() is used by the webhook (app/main.py) and by the
long-polling runner (run_telegram_polling.py) for local demos.
"""
import logging

import httpx

from .config import settings
from .models import Lead
from .pipeline import PipelineError, process_lead
from .scraper import extract_first_url
from .sheets import sheet_url

log = logging.getLogger("copilot")
API = "https://api.telegram.org"

HELP = (
    "Send me an event agency link (website, LinkedIn page or post) "
    "or a screenshot of a crew/hiring notice.\n"
    "I'll find the decision-maker and draft an Email + WhatsApp message for review."
)


async def _call(method: str, **payload) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{API}/bot{settings.telegram_bot_token}/{method}", json=payload)
        return resp.json()


async def send_message(chat_id: int, text: str) -> None:
    await _call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)


async def download_photo(file_id: str) -> bytes:
    info = await _call("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{API}/file/bot{settings.telegram_bot_token}/{path}")
        resp.raise_for_status()
        return resp.content


def format_reply(lead: Lead, saved_to: str) -> str:
    e = lead.enrichment
    link = sheet_url()
    return (
        f"✅ {lead.context.company_name} · {lead.context.city}\n"
        f"Need: {lead.context.requirement_context}\n\n"
        f"👤 {e.decision_maker} — {e.title} (confidence: {e.confidence})\n"
        f"{e.contact_details()}\n\n"
        f"✉️ EMAIL DRAFT\n{lead.drafts.email_full()}\n\n"
        f"💬 WHATSAPP DRAFT\n{lead.drafts.whatsapp}\n\n"
        f"Saved to {saved_to} as “{lead.status}”." + (f"\n{link}" if link else "")
    )


async def handle_update(update: dict) -> None:
    """Process one Telegram update end to end. Never raises: errors go back to the chat."""
    message = update.get("message") or update.get("channel_post") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    text = message.get("text") or message.get("caption") or ""
    photos = message.get("photo") or []
    document = message.get("document") or {}
    url = extract_first_url(text)

    image_id, media_type = None, "image/jpeg"
    if photos:
        image_id = photos[-1]["file_id"]  # last item is the highest resolution
    elif document.get("mime_type", "").startswith("image/"):
        image_id, media_type = document["file_id"], document["mime_type"]

    if not url and not image_id:
        await send_message(chat_id, HELP)
        return

    await send_message(chat_id, "Got it. Researching the company and drafting, about 30-60 seconds…")
    try:
        image_bytes = await download_photo(image_id) if image_id else None
        lead, saved_to = await process_lead(
            url=url, image_bytes=image_bytes, image_media_type=media_type, channel="telegram"
        )
        await send_message(chat_id, format_reply(lead, saved_to))
    except PipelineError as exc:
        await send_message(chat_id, f"⚠️ {exc}")
    except Exception:
        log.exception("Telegram lead failed")
        await send_message(chat_id, "⚠️ Something went wrong while processing that. Please try again.")
