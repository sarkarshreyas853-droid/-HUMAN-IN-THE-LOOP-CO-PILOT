"""Fetch a URL and pull out readable text plus any contact details on the page.

Deliberately simple: plain HTTP + BeautifulSoup, no headless browser.
Sites that block bots (LinkedIn, Instagram) return little or nothing; in that
case the pipeline still works because Claude's web search fills the gap.
"""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}|\+\d{1,3}[\s-]?\d{2,5}[\s-]?\d{3,5}[\s-]?\d{3,5}")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
# Pages most likely to name the team or list contact details.
USEFUL_LINK_RE = re.compile(r"about|team|contact|people|leadership|founder|who-we-are", re.I)
MAX_CHARS_PER_PAGE = 6000
MAX_EXTRA_PAGES = 3


class ScrapeResult:
    def __init__(self) -> None:
        self.text: str = ""
        self.emails: list[str] = []
        self.phones: list[str] = []
        self.pages_fetched: list[str] = []
        self.error: str = ""


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,)") if match else None


def is_safe_public_url(url: str) -> bool:
    """Block localhost / private network targets so the server can't be used to probe them."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


def html_to_text(html: str) -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    description = meta.get("content", "") if meta else ""
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    text = f"TITLE: {title}\nDESCRIPTION: {description}\nBODY: {body}"
    return text[:MAX_CHARS_PER_PAGE], soup


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


async def scrape(url: str) -> ScrapeResult:
    result = ScrapeResult()
    if not is_safe_public_url(url):
        result.error = "URL is not a public http(s) address"
        return result

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            result.error = f"Could not fetch page: {exc.__class__.__name__}"
            return result

        text, soup = html_to_text(resp.text)
        chunks = [f"--- {url} ---\n{text}"]
        raw_html = resp.text
        result.pages_fetched.append(url)

        # Follow a few same-site links like /about, /team, /contact.
        host = urlparse(str(resp.url)).hostname
        extra: list[str] = []
        for a in soup.find_all("a", href=True):
            full = urljoin(str(resp.url), a["href"]).split("#")[0]
            label = f"{a['href']} {a.get_text(' ', strip=True)}"
            if urlparse(full).hostname == host and USEFUL_LINK_RE.search(label):
                extra.append(full)
        for link in _unique(extra)[:MAX_EXTRA_PAGES]:
            if link in result.pages_fetched or not is_safe_public_url(link):
                continue
            try:
                sub = await client.get(link)
                sub.raise_for_status()
            except httpx.HTTPError:
                continue
            sub_text, _ = html_to_text(sub.text)
            chunks.append(f"--- {link} ---\n{sub_text}")
            raw_html += sub.text
            result.pages_fetched.append(link)

    result.text = "\n\n".join(chunks)
    result.emails = [
        e for e in _unique(EMAIL_RE.findall(raw_html))
        if not e.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"))
    ][:8]
    result.phones = _unique(PHONE_RE.findall(BeautifulSoup(raw_html, "html.parser").get_text(" ")))[:8]
    return result
