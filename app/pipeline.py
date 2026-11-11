"""The enrichment pipeline: input -> context -> decision-maker -> drafts -> sheet row.

    URL or screenshot
        |  scrape (URL only)
        v
    1. extract_context   -> company, city, requirement
    2. enrich            -> decision-maker, title, contacts (site data + web search)
    3. draft             -> 3-sentence email + <60-word WhatsApp
    4. store             -> Google Sheet row, Status = Pending Review (human reviews before sending)

Pass a `progress` callback to get a short message at each step (the web page shows them).
"""
import logging

from . import scraper, sheets
from .llm import GroqLLM
from .models import NOT_FOUND, Lead

log = logging.getLogger("copilot")


class PipelineError(Exception):
    """Raised with a message that is safe to show to the person who submitted the lead."""


async def process_lead(
    url: str | None = None,
    image_bytes: bytes | None = None,
    image_media_type: str = "image/jpeg",
    channel: str = "web",
    llm=None,
    store=sheets.append_lead,
    progress=None,
) -> tuple[Lead, str]:
    """Run the full pipeline. Returns the lead and a note on where it was saved."""
    if not url and not image_bytes:
        raise PipelineError("Send a URL or a screenshot.")
    llm = llm or GroqLLM()

    async def say(message: str) -> None:
        if progress:
            await progress(message)

    # --- scrape (URL inputs only) ---
    await say("Reading the page" if url else "Reading the screenshot")
    scraped = scraper.ScrapeResult()
    if url:
        scraped = await scraper.scrape(url)
        if scraped.error:
            log.info("Scrape failed for %s: %s (continuing with web search)", url, scraped.error)

    # --- 1. context extraction ---
    await say("Identifying the company")
    context = await llm.extract_context(
        page_text=scraped.text, image_bytes=image_bytes,
        image_media_type=image_media_type, source_url=url or "",
    )
    if context.company_name == NOT_FOUND:
        raise PipelineError(
            "Could not work out which company this is about. "
            "Try the agency's website or a clearer screenshot."
        )

    # If we started from a post/screenshot but learned the company website, scrape that too.
    if context.website and not scraped.text:
        site = context.website if context.website.startswith("http") else f"https://{context.website}"
        scraped = await scraper.scrape(site)
    elif url and not context.website and scraped.text and "linkedin.com" not in url and "instagram.com" not in url:
        context.website = url

    # --- 2. decision-maker + contacts ---
    await say("Looking for the decision-maker and contacts")
    enrichment = await llm.enrich(
        context, site_text=scraped.text, site_emails=scraped.emails, site_phones=scraped.phones
    )

    # --- 3. drafts ---
    await say("Writing the email and WhatsApp drafts")
    drafts = await llm.draft(context, enrichment)

    lead = Lead(
        context=context, enrichment=enrichment, drafts=drafts,
        source=url or "screenshot", channel=channel,
    )

    # --- 4. store for human review ---
    await say("Saving to the leads list")
    saved_to = store(lead)
    log.info("Saved lead %s -> %s (row %s)", context.company_name, saved_to, lead.row)
    return lead, saved_to