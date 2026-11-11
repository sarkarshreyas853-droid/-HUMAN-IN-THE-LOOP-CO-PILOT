"""Web search for the decision-maker lookup, using Tavily's free tier.

Returns an empty list when no TAVILY_API_KEY is set, so the pipeline still works
using only the company's own website.
"""
import logging

import httpx

from .config import settings

log = logging.getLogger("copilot")
TAVILY_URL = "https://api.tavily.com/search"


async def web_search(queries: list[str], max_results: int = 4) -> list[dict]:
    """Run a few searches and return unique results as {title, url, content} dicts."""
    if not settings.tavily_api_key or not settings.enable_web_search:
        return []
    seen: set[str] = set()
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for query in queries[: settings.max_web_searches]:
            try:
                resp = await client.post(
                    TAVILY_URL,
                    headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
                    json={"query": query, "max_results": max_results, "search_depth": "basic"},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Web search failed for %r: %s", query, exc)
                continue
            for item in resp.json().get("results", []):
                url = item.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    results.append({
                        "title": item.get("title", ""),
                        "url": url,
                        "content": (item.get("content") or "")[:800],
                    })
    return results