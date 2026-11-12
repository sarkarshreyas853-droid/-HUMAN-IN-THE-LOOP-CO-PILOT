"""Thin wrapper around the Groq API (free tier): one method per pipeline step.

Groq serves open models through an OpenAI-compatible endpoint. Each method sends one
prompt and returns parsed JSON. The pipeline only depends on the three methods
extract_context, enrich and draft.
"""
import base64
import json
import re

import httpx

from . import prompts
from .config import settings
from .models import NOT_FOUND, Drafts, Enrichment, LeadContext
from .search import web_search

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def parse_json_object(text: str) -> dict:
    """Pull the last JSON object out of a model reply (tolerates prose or ``` fences around it)."""
    decoder = json.JSONDecoder()
    found: list[dict] = []
    for match in re.finditer(r"\{", text or ""):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    if not found:
        raise ValueError(f"No JSON object found in model reply: {text[:200]!r}")
    # Outer objects are found before their nested ones; prefer the biggest of the last reply.
    return max(found, key=lambda d: len(json.dumps(d)))


def word_count(text: str) -> int:
    return len((text or "").split())


# ---------- evidence checks: nothing reaches the sheet unless a source backs it ----------

# Words that say what kind of business it is, not which business it is.
_NAME_NOISE = {"the", "private", "limited", "pvt", "ltd", "llp", "inc", "co", "company",
               "technologies", "india"}
_COMMON_FIRST_WORDS = {"event", "events", "wedding", "weddings", "dream", "royal", "grand"}


def _squash(text: str) -> str:
    """Lowercase and keep only letters and digits, so 'Event-Planet' matches 'event planet'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _mentions_company(text: str, company_name: str) -> bool:
    """True when the text names this company (not merely a similar-sounding one)."""
    words = [w for w in re.findall(r"[a-z0-9]+", company_name.lower()) if w not in _NAME_NOISE]
    if not words:
        return False
    squashed = _squash(text)
    if "".join(words) in squashed:
        return True
    first = words[0]
    return len(first) >= 5 and first not in _COMMON_FIRST_WORDS and first in squashed


def _linkedin_key(url: str) -> str:
    """'https://in.linkedin.com/in/Asha-Rao/' -> 'linkedin.com/in/asha-rao'."""
    match = re.search(r"linkedin\.com/(in|company)/([^/?#\s]+)", (url or "").lower())
    return f"linkedin.com/{match.group(1)}/{match.group(2)}" if match else ""


def verify_enrichment(
    data: dict, company_name: str, site_text: str, site_emails: list[str],
    site_phones: list[str], results: list[dict],
) -> dict:
    """Blank out anything the model returned that no source actually contains.

    The model is told not to guess, but it sometimes does. This is the safety net:
    a name, email, phone or LinkedIn link is kept only if it appears in the company's
    own site or in a search result, and a LinkedIn link must come from a result that
    names this company.
    """
    site_blob = " ".join([site_text or "", " ".join(site_emails), " ".join(site_phones)])
    result_blobs = [f"{r.get('title', '')} {r.get('url', '')} {r.get('content', '')}" for r in results]
    evidence = (site_blob + " " + " ".join(result_blobs)).lower()
    evidence_digits = re.sub(r"\D", "", evidence)
    clean = dict(data)

    # Decision-maker: every part of the name must appear in a source.
    name = str(clean.get("decision_maker") or "").strip()
    name_parts = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) >= 3]
    looks_like_placeholder = bool(re.search(r"\bnot\b|unknown|n/a|\(", name.lower()))
    if not name_parts or looks_like_placeholder or not all(w in evidence for w in name_parts):
        clean["decision_maker"] = NOT_FOUND
        clean["title"] = NOT_FOUND
        clean["confidence"] = "low"

    # Email: must appear word for word.
    email = str(clean.get("email") or "").strip()
    if email and email.lower() not in evidence:
        clean["email"] = ""

    # Phone: the last 10 digits must appear.
    phone_digits = re.sub(r"\D", "", str(clean.get("phone") or ""))
    if phone_digits and (len(phone_digits) < 7 or phone_digits[-10:] not in evidence_digits):
        clean["phone"] = ""

    # LinkedIn: the exact profile must be in a source that names this company.
    key = _linkedin_key(str(clean.get("linkedin") or ""))
    on_site = bool(key) and key in site_blob.lower()
    in_matching_result = bool(key) and any(
        key in blob.lower() and _mentions_company(blob, company_name) for blob in result_blobs
    )
    if clean.get("linkedin") and not (on_site or in_matching_result):
        clean["linkedin"] = ""

    # Sources: only pages we really looked at.
    known_urls = {r.get("url", "") for r in results}
    sources = clean.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    clean["sources"] = [
        str(u) for u in sources if str(u) in known_urls or str(u).lower() in site_blob.lower()
    ]
    return clean


class GroqLLM:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.groq_api_key

    async def _chat(
        self, messages: list[dict], model: str, max_tokens: int = 1500, temperature: float = 0.3,
    ) -> str:
        """Send one chat request to Groq and return the reply text."""
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set in your .env file.")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        # gpt-oss models reason before answering. Keep the reasoning short so it does not
        # use up max_tokens and leave the visible reply empty.
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                GROQ_URL, headers={"Authorization": f"Bearer {self.api_key}"}, json=body
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Groq error {resp.status_code}: {resp.text[:300]}")
        content = resp.json()["choices"][0]["message"].get("content") or ""
        if not content.strip():
            raise RuntimeError(f"Model {model} returned an empty reply. Check GROQ_MODEL in .env.")
        return content

    # ---- Step 1: context extraction (text or screenshot) ----
    async def extract_context(
        self, page_text: str = "", image_bytes: bytes | None = None,
        image_media_type: str = "image/jpeg", source_url: str = "",
    ) -> LeadContext:
        text = "Extract the lead details from this input."
        if source_url:
            text += f"\nSource URL: {source_url}"
        if page_text:
            text += f"\n\nPage content:\n{page_text}"
        elif source_url and not image_bytes:
            text += "\n\n(The page could not be fetched. Infer what you safely can from the URL itself.)"

        if image_bytes:
            encoded = base64.standard_b64encode(image_bytes).decode()
            user_content = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{encoded}"}},
            ]
            model = settings.groq_vision_model
        else:
            user_content = text
            model = settings.groq_model

        reply = await self._chat(
            [{"role": "system", "content": prompts.EXTRACT_SYSTEM}, {"role": "user", "content": user_content}],
            model, max_tokens=800,
        )
        data = parse_json_object(reply)
        return LeadContext(**{k: str(v) for k, v in data.items() if k in LeadContext.model_fields and v})

    # ---- Step 2: decision-maker + contact lookup ----
    async def enrich(
        self, context: LeadContext, site_text: str = "",
        site_emails: list[str] | None = None, site_phones: list[str] | None = None,
    ) -> Enrichment:
        results = await web_search([
            f"{context.company_name} {context.city} founder production head contact",
            f"{context.company_name} {context.city} linkedin",
        ])
        results_text = "\n\n".join(
            f"- {r['title']} ({r['url']})\n  {r['content']}" for r in results
        ) or "(no web search results)"

        user = (
            f"Company: {context.company_name}\nCity: {context.city}\n"
            f"Website: {context.website or 'unknown'}\n"
            f"What they need: {context.requirement_context}\n\n"
            f"Emails seen on their site: {', '.join(site_emails or []) or 'none'}\n"
            f"Phone numbers seen on their site: {', '.join(site_phones or []) or 'none'}\n\n"
            f"Website content:\n{site_text or '(not available)'}\n\n"
            f"Web search results:\n{results_text}"
        )
        reply = await self._chat(
            [{"role": "system", "content": prompts.ENRICH_SYSTEM}, {"role": "user", "content": user}],
            settings.groq_model, max_tokens=1200, temperature=0,  # same input, same answer
        )
        data = verify_enrichment(
            parse_json_object(reply), context.company_name,
            site_text, site_emails or [], site_phones or [], results,
        )
        sources = data.get("sources") or [r["url"] for r in results[:3]]
        clean = {
            k: str(v) for k, v in data.items()
            if k in Enrichment.model_fields and k != "sources" and v
        }
        return Enrichment(**clean, sources=[str(s) for s in sources])

    # ---- Step 3: email + WhatsApp drafts ----
    async def draft(self, context: LeadContext, enrichment: Enrichment) -> Drafts:
        system = prompts.DRAFT_SYSTEM.format(pitch=prompts.GOPRATLE_PITCH)
        user = (
            f"Company: {context.company_name}\nCity: {context.city}\n"
            f"Requirement / context: {context.requirement_context}\n"
            f"Decision-maker: {enrichment.decision_maker} ({enrichment.title})\n"
            f"Sender name for sign-off: {settings.sender_name}"
        )
        requirement = context.requirement_context.lower()
        if context.requirement_context == NOT_FOUND or "no specific" in requirement:
            user += (
                "\nNote: no live event, campaign or hiring need was seen for this company. "
                "Do not say or imply that they have one. Refer only to their line of work."
            )
        reply = await self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            settings.groq_model, max_tokens=1000,
        )
        data = parse_json_object(reply)
        drafts = Drafts(**{k: str(v) for k, v in data.items() if k in Drafts.model_fields and v})

        # Hard rule from the brief: WhatsApp must be under 60 words. Ask once for a shorter one.
        if word_count(drafts.whatsapp) >= 60:
            fix = prompts.WHATSAPP_SHORTEN.format(count=word_count(drafts.whatsapp), draft=drafts.whatsapp)
            try:
                shorter_reply = await self._chat(
                    [{"role": "system", "content": "You edit messages for brevity."}, {"role": "user", "content": fix}],
                    settings.groq_model, max_tokens=300,
                )
                shorter = parse_json_object(shorter_reply)
                if shorter.get("whatsapp") and word_count(shorter["whatsapp"]) < 60:
                    drafts.whatsapp = str(shorter["whatsapp"])
            except ValueError:
                pass
        return drafts