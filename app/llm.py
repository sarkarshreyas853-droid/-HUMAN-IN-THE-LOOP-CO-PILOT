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
from .models import Drafts, Enrichment, LeadContext
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


class GroqLLM:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.groq_api_key

    async def _chat(self, messages: list[dict], model: str, max_tokens: int = 1500) -> str:
        """Send one chat request to Groq and return the reply text."""
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set in your .env file.")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
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
            settings.groq_model, max_tokens=1200,
        )
        data = parse_json_object(reply)
        sources = data.get("sources") or [r["url"] for r in results[:3]]
        if isinstance(sources, str):
            sources = [sources]
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