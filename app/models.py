"""Data shapes passed between pipeline steps."""
from datetime import datetime, timezone

from pydantic import BaseModel, Field

NOT_FOUND = "Not found"
STATUS_PENDING = "Pending Review"

# Column order in the sheet. The first 8 are the ones the brief asks for.
HEADERS = [
    "Company Name", "City", "Decision Maker", "Title", "Contact Details",
    "Draft: Email", "Draft: WhatsApp", "Status",
    "Requirement Context", "Confidence", "Sources", "Input", "Channel", "Created At", "Notes",
]


class LeadContext(BaseModel):
    """Step 1 output: what the inbound link/screenshot is about."""
    company_name: str = NOT_FOUND
    city: str = NOT_FOUND
    requirement_context: str = NOT_FOUND
    website: str = ""


class Enrichment(BaseModel):
    """Step 2 output: who to talk to and how to reach them."""
    decision_maker: str = NOT_FOUND
    title: str = NOT_FOUND
    phone: str = ""
    email: str = ""
    linkedin: str = ""
    sources: list[str] = Field(default_factory=list)
    confidence: str = "low"  # low | medium | high

    def contact_details(self) -> str:
        parts = []
        if self.phone:
            parts.append(f"Phone/WhatsApp: {self.phone}")
        if self.email:
            parts.append(f"Email: {self.email}")
        if self.linkedin:
            parts.append(f"LinkedIn: {self.linkedin}")
        return " | ".join(parts) or NOT_FOUND


class Drafts(BaseModel):
    """Step 3 output: ready-to-send copy for a human to review."""
    email_subject: str = ""
    email_body: str = ""
    whatsapp: str = ""

    def email_full(self) -> str:
        return f"Subject: {self.email_subject}\n\n{self.email_body}".strip()


class Lead(BaseModel):
    """One enriched row, ready for the sheet."""
    context: LeadContext
    enrichment: Enrichment
    drafts: Drafts
    source: str = ""           # the URL or "screenshot"
    channel: str = "web"       # web | telegram
    status: str = STATUS_PENDING
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    row: int = 0               # row number in the sheet or CSV, set when the lead is saved

    def to_row(self) -> list[str]:
        return [
            self.context.company_name,
            self.context.city,
            self.enrichment.decision_maker,
            self.enrichment.title,
            self.enrichment.contact_details(),
            self.drafts.email_full(),
            self.drafts.whatsapp,
            self.status,
            self.context.requirement_context,
            self.enrichment.confidence,
            "\n".join(self.enrichment.sources[:5]),
            self.source,
            self.channel,
            self.created_at,
            "",  # Notes: filled in by the reviewer
        ]