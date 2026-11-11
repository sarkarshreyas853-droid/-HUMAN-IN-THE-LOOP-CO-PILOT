"""FastAPI app: the inbound triggers and the leads list.

    GET  /                           web page (submit a lead, edit drafts, see the leads list)
    POST /api/leads/stream           submit a lead; streams progress steps, then the result
    POST /api/leads                  submit a lead in one response (no progress steps)
    GET  /api/leads                  list saved leads, newest first
    POST /api/leads/{row}/status     set a lead's status (Pending Review, Approved, Sent, ...)
    POST /api/leads/{row}/fields     save edited drafts and notes
    POST /telegram/webhook/{secret}  Telegram bot webhook
    GET  /health                     config check
"""
import asyncio
import hmac
import json
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import sheets, telegram
from .config import settings
from .models import HEADERS
from .pipeline import PipelineError, process_lead

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copilot")

app = FastAPI(title="GoPratle Lead Enrichment & Drafting Copilot")
INDEX_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
GENERIC_FAILURE = "Processing failed. Check the server logs and your API keys."


class StatusUpdate(BaseModel):
    status: str


class FieldsUpdate(BaseModel):
    fields: dict[str, str]


async def _read_input(url: str, screenshot: UploadFile | None) -> tuple[str | None, bytes | None, str]:
    """Check the submitted link or screenshot. Returns (url, image bytes, media type)."""
    url = (url or "").strip()
    image_bytes, media_type = None, "image/jpeg"
    if screenshot is not None and screenshot.filename:
        media_type = screenshot.content_type or "image/jpeg"
        if media_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(400, "Screenshot must be a JPG, PNG, WEBP or GIF image.")
        image_bytes = await screenshot.read()
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(400, "Screenshot is larger than 5 MB.")
    if not url and not image_bytes:
        raise HTTPException(400, "Provide a URL or a screenshot.")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url or None, image_bytes, media_type


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML.replace("{{SHEET_URL}}", sheets.sheet_url())


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": settings.groq_model,
        "groq_key_set": bool(settings.groq_api_key),
        "tavily_search": bool(settings.tavily_api_key),
        "web_search": settings.enable_web_search,
        "google_sheet_configured": sheets.sheets_configured(),
        "telegram_configured": bool(settings.telegram_bot_token),
    }


@app.post("/api/leads/stream")
async def create_lead_stream(url: str = Form(""), screenshot: UploadFile | None = File(None)) -> StreamingResponse:
    """Runs the pipeline and streams one JSON object per line: {"step": ...}, then {"done": ...} or {"error": ...}."""
    url, image_bytes, media_type = await _read_input(url, screenshot)
    queue: asyncio.Queue = asyncio.Queue()

    async def progress(message: str) -> None:
        await queue.put({"step": message})

    async def worker() -> None:
        try:
            lead, saved_to = await process_lead(
                url=url, image_bytes=image_bytes, image_media_type=media_type,
                channel="web", progress=progress,
            )
            await queue.put({
                "done": True,
                "saved_to": saved_to,
                "row_number": lead.row,
                "row": dict(zip(HEADERS, lead.to_row())),
            })
        except PipelineError as exc:
            await queue.put({"error": str(exc)})
        except Exception:
            log.exception("Lead processing failed")
            await queue.put({"error": GENERIC_FAILURE})
        finally:
            await queue.put(None)

    asyncio.create_task(worker())

    async def events():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield json.dumps(item) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/leads")
async def create_lead(url: str = Form(""), screenshot: UploadFile | None = File(None)) -> dict:
    url, image_bytes, media_type = await _read_input(url, screenshot)
    try:
        lead, saved_to = await process_lead(
            url=url, image_bytes=image_bytes, image_media_type=media_type, channel="web"
        )
    except PipelineError as exc:
        raise HTTPException(422, str(exc))
    except Exception:
        log.exception("Lead processing failed")
        raise HTTPException(502, GENERIC_FAILURE)

    return {
        "saved_to": saved_to,
        "sheet_url": sheets.sheet_url(),
        "row_number": lead.row,
        "row": dict(zip(HEADERS, lead.to_row())),
    }


@app.get("/api/leads")
async def get_leads() -> dict:
    try:
        leads = sheets.list_leads()
    except Exception:
        log.exception("Could not read leads")
        raise HTTPException(502, "Could not read leads. Check the Google Sheet or leads.csv.")
    return {"leads": leads, "statuses": sheets.STATUSES, "backend": sheets.backend_name()}


@app.post("/api/leads/{row}/status")
async def update_status(row: int, body: StatusUpdate) -> dict:
    if body.status not in sheets.STATUSES:
        raise HTTPException(400, "Unknown status.")
    try:
        sheets.set_status(row, body.status)
    except LookupError:
        raise HTTPException(404, "Lead not found.")
    except Exception:
        log.exception("Could not update status")
        raise HTTPException(502, "Could not update the status. Try again.")
    return {"ok": True, "status": body.status}


@app.post("/api/leads/{row}/fields")
async def update_fields(row: int, body: FieldsUpdate) -> dict:
    if any(name not in sheets.EDITABLE_FIELDS for name in body.fields):
        raise HTTPException(400, "Only the drafts and notes can be edited.")
    try:
        sheets.update_fields(row, body.fields)
    except LookupError:
        raise HTTPException(404, "Lead not found.")
    except Exception:
        log.exception("Could not save changes")
        raise HTTPException(502, "Could not save your changes. Try again.")
    return {"ok": True}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request, background: BackgroundTasks) -> dict:
    if not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        raise HTTPException(403, "Bad secret")
    update = await request.json()
    # Reply to Telegram immediately; do the slow work in the background so it doesn't retry.
    background.add_task(telegram.handle_update, update)
    return {"ok": True}