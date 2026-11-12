"""Offline tests: the LLM and network are faked, so these run without any API keys.

    pytest -q
"""
import asyncio
import csv

import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import main, pipeline, scraper, sheets, telegram
from app.config import settings
from app.llm import GroqLLM, parse_json_object, word_count
from app.models import HEADERS, Drafts, Enrichment, Lead, LeadContext


class FakeLLM:
    """Stands in for GroqLLM with canned answers."""

    def __init__(self, company="Sparkle Events"):
        self.company = company
        self.calls = []

    async def extract_context(self, **kwargs):
        self.calls.append(("extract", kwargs))
        return LeadContext(company_name=self.company, city="Mumbai",
                           requirement_context="Needs 30 ushers for a festival", website="")

    async def enrich(self, context, **kwargs):
        self.calls.append(("enrich", kwargs))
        return Enrichment(decision_maker="Asha Rao", title="Head of Production",
                          email="hello@sparkle.example", phone="+91 98765 43210",
                          sources=["https://sparkle.example/team"], confidence="high")

    async def draft(self, context, enrichment):
        self.calls.append(("draft", {}))
        return Drafts(email_subject="Crew for your festival", email_body="Hi Asha,\nA. B. C.\nTeam",
                      whatsapp="Hi Asha, saw the festival. Need ushers?")


@pytest.fixture
def csv_store(tmp_path, monkeypatch):
    path = tmp_path / "leads.csv"
    monkeypatch.setattr(settings, "csv_fallback_path", str(path))
    monkeypatch.setattr(settings, "google_sheet_id", "")
    return path


async def _no_scrape(url):
    result = scraper.ScrapeResult()
    result.text, result.emails = "TITLE: Sparkle Events", ["hello@sparkle.example"]
    return result


# ---------- JSON parsing ----------
def test_parse_json_plain_and_fenced_and_with_prose():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": "x"}\n```') == {"a": "x"}
    reply = 'I searched {the web}. Result:\n{"decision_maker": "Asha", "sources": ["u"], "n": {"k": 1}}'
    assert parse_json_object(reply)["decision_maker"] == "Asha"


def test_parse_json_raises_when_missing():
    with pytest.raises(ValueError):
        parse_json_object("no json here")


# ---------- scraper helpers ----------
def test_extract_first_url_and_safety():
    assert scraper.extract_first_url("see https://abc.com/x. thanks") == "https://abc.com/x"
    assert scraper.extract_first_url("no link") is None
    assert not scraper.is_safe_public_url("http://127.0.0.1:8000/admin")
    assert not scraper.is_safe_public_url("http://localhost/")
    assert not scraper.is_safe_public_url("ftp://example.com")


def test_html_to_text_strips_scripts():
    text, _ = scraper.html_to_text(
        "<html><head><title>Acme Events</title><script>var x=1</script></head>"
        "<body><p>We run festivals in Pune</p></body></html>")
    assert "Acme Events" in text and "festivals in Pune" in text and "var x" not in text


# ---------- row shape ----------
def test_row_matches_headers_and_required_columns():
    lead = Lead(context=LeadContext(company_name="X"), enrichment=Enrichment(), drafts=Drafts())
    assert len(lead.to_row()) == len(HEADERS)
    assert HEADERS[:8] == ["Company Name", "City", "Decision Maker", "Title", "Contact Details",
                           "Draft: Email", "Draft: WhatsApp", "Status"]
    assert lead.to_row()[7] == "Pending Review"
    assert lead.to_row()[4] == "Not found"


# ---------- pipeline ----------
def test_pipeline_url_to_csv_row(csv_store, monkeypatch):
    monkeypatch.setattr(scraper, "scrape", _no_scrape)
    fake = FakeLLM()
    lead, saved_to = asyncio.run(pipeline.process_lead(url="https://sparkle.example", llm=fake))
    assert [c[0] for c in fake.calls] == ["extract", "enrich", "draft"]
    assert fake.calls[1][1]["site_emails"] == ["hello@sparkle.example"]
    assert lead.context.website == "https://sparkle.example"
    assert "local file" in saved_to
    rows = list(csv.reader(open(csv_store, encoding="utf-8")))
    assert rows[0] == HEADERS
    assert rows[1][0] == "Sparkle Events" and rows[1][2] == "Asha Rao" and rows[1][7] == "Pending Review"
    assert "Subject: Crew for your festival" in rows[1][5]


def test_pipeline_screenshot_only(csv_store):
    fake = FakeLLM()
    lead, _ = asyncio.run(pipeline.process_lead(image_bytes=b"\x89PNG", image_media_type="image/png", llm=fake))
    assert fake.calls[0][1]["image_bytes"] == b"\x89PNG"
    assert lead.source == "screenshot"


def test_pipeline_rejects_unknown_company_and_empty_input(csv_store):
    with pytest.raises(pipeline.PipelineError):
        asyncio.run(pipeline.process_lead(image_bytes=b"x", llm=FakeLLM(company="Not found")))
    with pytest.raises(pipeline.PipelineError):
        asyncio.run(pipeline.process_lead(llm=FakeLLM()))
    assert not csv_store.exists()


# ---------- GroqLLM with a fake _chat (no network) ----------
def _groq(replies):
    client = GroqLLM(api_key="test-key")
    client.sent = []
    queue = list(replies)

    async def fake_chat(messages, model, max_tokens=1500, temperature=0.3):
        client.sent.append({"messages": messages, "model": model})
        return queue.pop(0)

    client._chat = fake_chat
    return client


def test_enrich_uses_search_results_and_site_data(monkeypatch):
    async def fake_search(queries, max_results=4):
        return [{"title": "B Co team", "url": "https://b.co/team", "content": "Asha Rao, Founder. Write to a@b.co"}]

    monkeypatch.setattr(llm_module, "web_search", fake_search)
    client = _groq(['Here you go {"decision_maker": "Asha Rao", "title": "Founder", "phone": "", '
                    '"email": "a@b.co", "linkedin": "", "sources": ["https://b.co/team"], "confidence": "medium"}'])
    result = asyncio.run(client.enrich(LeadContext(company_name="B Co", city="Pune")))
    assert "Asha Rao, Founder" in client.sent[0]["messages"][1]["content"]
    assert result.decision_maker == "Asha Rao" and result.email == "a@b.co" and result.phone == ""
    assert result.contact_details() == "Email: a@b.co"


def test_enrich_works_without_a_search_key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    client = _groq(['{"decision_maker": "Not found", "title": "Not found"}'])
    result = asyncio.run(client.enrich(LeadContext(company_name="C Co", city="Delhi")))
    assert "(no web search results)" in client.sent[0]["messages"][1]["content"]
    assert result.decision_maker == "Not found"


def test_extract_uses_vision_model_for_screenshots():
    client = _groq(['{"company_name": "Acme", "city": "Goa", "requirement_context": "x", "website": ""}'])
    ctx = asyncio.run(client.extract_context(image_bytes=b"abc", image_media_type="image/png"))
    user = client.sent[0]["messages"][1]["content"]
    assert isinstance(user, list) and user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert client.sent[0]["model"] == settings.groq_vision_model
    assert ctx.company_name == "Acme" and ctx.website == ""


def test_whatsapp_over_60_words_is_shortened():
    long_msg = " ".join(["word"] * 75)
    client = _groq([
        '{"email_subject": "S", "email_body": "B", "whatsapp": "%s"}' % long_msg,
        '{"whatsapp": "Hi Asha, short and sweet. Open to a quick call?"}',
    ])
    drafts = asyncio.run(client.draft(LeadContext(company_name="A"), Enrichment()))
    assert word_count(drafts.whatsapp) < 60
    assert len(client.sent) == 2


def test_missing_groq_key_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    client = GroqLLM(api_key="")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asyncio.run(client._chat([{"role": "user", "content": "hi"}], "m"))


# ---------- web API ----------
def test_api_and_pages(csv_store, monkeypatch):
    monkeypatch.setattr(scraper, "scrape", _no_scrape)
    monkeypatch.setattr(pipeline, "GroqLLM", FakeLLM)
    client = TestClient(main.app)

    assert client.get("/").status_code == 200 and "GoPratle Copilot" in client.get("/").text
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/api/leads", data={"url": ""}).status_code == 400

    res = client.post("/api/leads", data={"url": "sparkle.example"})
    assert res.status_code == 200
    row = res.json()["row"]
    assert row["Company Name"] == "Sparkle Events" and row["Status"] == "Pending Review"
    assert row["Draft: WhatsApp"] and row["Draft: Email"]

    res = client.post("/api/leads", files={"screenshot": ("n.png", b"\x89PNG", "image/png")})
    assert res.status_code == 200 and res.json()["row"]["Input"] == "screenshot"
    res = client.post("/api/leads", files={"screenshot": ("n.pdf", b"%PDF", "application/pdf")})
    assert res.status_code == 400


def test_telegram_webhook_secret_and_flow(csv_store, monkeypatch):
    monkeypatch.setattr(scraper, "scrape", _no_scrape)
    monkeypatch.setattr(pipeline, "GroqLLM", FakeLLM)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
    sent = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_message", fake_send)
    client = TestClient(main.app)

    assert client.post("/telegram/webhook/wrong", json={}).status_code == 403
    update = {"message": {"chat": {"id": 7}, "text": "check https://sparkle.example please"}}
    assert client.post("/telegram/webhook/s3cret", json=update).status_code == 200
    assert sent[0][0] == 7 and "Researching" in sent[0][1]
    assert "Sparkle Events" in sent[1][1] and "WHATSAPP DRAFT" in sent[1][1] and "Pending Review" in sent[1][1]

    sent.clear()
    client.post("/telegram/webhook/s3cret", json={"message": {"chat": {"id": 7}, "text": "hello"}})
    assert "Send me an event agency link" in sent[0][1]


# ---------- leads list and status updates ----------
def _sample_lead(company):
    return Lead(context=LeadContext(company_name=company, city="Pune"),
                enrichment=Enrichment(decision_maker="Ravi", title="Head of Events"),
                drafts=Drafts(email_subject="Hi", email_body="Body", whatsapp="Hello"))


def test_list_and_set_status_in_csv(csv_store):
    sheets._append_csv(_sample_lead("First Co"))
    sheets._append_csv(_sample_lead("Second Co"))
    leads = sheets.list_leads()
    assert [l["Company Name"] for l in leads] == ["Second Co", "First Co"]   # newest first
    assert leads[0]["_row"] == 3 and leads[1]["_row"] == 2
    sheets.set_status(leads[0]["_row"], "Approved")
    assert sheets.list_leads()[0]["Status"] == "Approved"
    assert sheets.list_leads()[1]["Status"] == "Pending Review"   # other rows untouched
    with pytest.raises(LookupError):
        sheets.set_status(99, "Sent")
    with pytest.raises(ValueError):
        sheets.set_status(2, "Made up")


def test_leads_api_lists_and_updates_status(csv_store):
    sheets._append_csv(_sample_lead("Api Co"))
    client = TestClient(main.app)
    body = client.get("/api/leads").json()
    assert body["backend"] == "leads.csv" and body["leads"][0]["Company Name"] == "Api Co"
    assert "Approved" in body["statuses"]
    row = body["leads"][0]["_row"]
    assert client.post(f"/api/leads/{row}/status", json={"status": "Sent"}).status_code == 200
    assert client.get("/api/leads").json()["leads"][0]["Status"] == "Sent"
    assert client.post(f"/api/leads/{row}/status", json={"status": "Bogus"}).status_code == 400
    assert client.post("/api/leads/99/status", json={"status": "Sent"}).status_code == 404


# ---------- editable drafts, notes and progress steps ----------
def test_update_fields_and_notes_in_csv(csv_store):
    sheets._append_csv(_sample_lead("Notes Co"))
    sheets.update_fields(2, {"Notes": "Called, interested", "Draft: WhatsApp": "Edited hi"})
    lead = sheets.list_leads()[0]
    assert lead["Notes"] == "Called, interested" and lead["Draft: WhatsApp"] == "Edited hi"
    assert lead["Status"] == "Pending Review"                      # other fields untouched
    with pytest.raises(ValueError):
        sheets.update_fields(2, {"Company Name": "Renamed"})


def test_stream_endpoint_sends_steps_then_result(csv_store, monkeypatch):
    import json
    monkeypatch.setattr(scraper, "scrape", _no_scrape)
    monkeypatch.setattr(pipeline, "GroqLLM", FakeLLM)
    client = TestClient(main.app)
    res = client.post("/api/leads/stream", data={"url": "sparkle.example"})
    lines = [json.loads(line) for line in res.text.splitlines() if line.strip()]
    assert any("step" in item for item in lines)
    done = [item for item in lines if item.get("done")][0]
    assert done["row_number"] == 2 and done["row"]["Company Name"] == "Sparkle Events"
    assert "Notes" in done["row"]


def test_fields_api_rejects_non_editable(csv_store):
    sheets._append_csv(_sample_lead("Api Edit Co"))
    client = TestClient(main.app)
    assert client.post("/api/leads/2/fields", json={"fields": {"Notes": "ok"}}).status_code == 200
    assert client.post("/api/leads/2/fields", json={"fields": {"Status": "Sent"}}).status_code == 400
    assert client.post("/api/leads/99/fields", json={"fields": {"Notes": "x"}}).status_code == 404


# ---------- evidence checks ----------

def _enrich_with(monkeypatch, reply, results, company="Event Planet Technologies Private Limited", **kwargs):
    async def fake_search(queries, max_results=4):
        return results

    monkeypatch.setattr(llm_module, "web_search", fake_search)
    client = _groq([reply])
    return asyncio.run(client.enrich(LeadContext(company_name=company, city="Delhi"), **kwargs))


def test_invented_linkedin_and_contacts_are_dropped(monkeypatch):
    results = [{"title": "Event Planet raises funding", "url": "https://news.example/ep",
                "content": "Event Planet founder Rohit Yagya said the app is growing."}]
    reply = ('{"decision_maker": "Rohit Yagya", "title": "Founder & CEO", "phone": "+91 98765 43210", '
             '"email": "rohit@eventplanet.in", "linkedin": "https://www.linkedin.com/in/rohit-yagya-123456789/", '
             '"sources": ["https://news.example/ep", "https://made-up.example"], "confidence": "medium"}')
    result = _enrich_with(monkeypatch, reply, results)
    assert result.decision_maker == "Rohit Yagya"          # the name is in a source, so it stays
    assert result.linkedin == "" and result.email == "" and result.phone == ""
    assert result.sources == ["https://news.example/ep"]


def test_linkedin_of_a_namesake_at_another_company_is_dropped(monkeypatch):
    results = [{"title": "Deepak Tyagi - Acme Logistics | LinkedIn",
                "url": "https://in.linkedin.com/in/deepak-tyagi-14898426",
                "content": "Deepak Tyagi, operations manager at Acme Logistics."}]
    reply = ('{"decision_maker": "Deepak Tyagi", "title": "Founder", '
             '"linkedin": "https://in.linkedin.com/in/deepak-tyagi-14898426", "confidence": "medium"}')
    result = _enrich_with(monkeypatch, reply, results)
    assert result.linkedin == ""


def test_linkedin_from_a_result_naming_the_company_is_kept(monkeypatch):
    results = [{"title": "Event Planet | LinkedIn", "url": "https://www.linkedin.com/company/event-planet-india/",
                "content": "Event Planet is an event planning app. Founder: Rohit Yagya."}]
    reply = ('{"decision_maker": "Rohit Yagya", "title": "Founder", '
             '"linkedin": "https://www.linkedin.com/company/event-planet-india", "confidence": "high"}')
    result = _enrich_with(monkeypatch, reply, results)
    assert result.linkedin.endswith("event-planet-india") and result.confidence == "high"


def test_similar_company_name_is_not_a_match(monkeypatch):
    results = [{"title": "The Case Study Project | LinkedIn",
                "url": "https://www.linkedin.com/company/the-case-study-project", "content": "A research studio."}]
    reply = '{"decision_maker": "Not found", "linkedin": "https://www.linkedin.com/company/the-case-study-project"}'
    result = _enrich_with(monkeypatch, reply, results, company="The Case Project")
    assert result.linkedin == ""


def test_placeholder_or_unsourced_name_becomes_not_found(monkeypatch):
    for name in ["Founder (Name not publicly listed)", "Priya Menon"]:
        reply = '{"decision_maker": "%s", "title": "Founder", "confidence": "high"}' % name
        result = _enrich_with(monkeypatch, reply, [], site_text="We plan weddings in Kolkata.")
        assert result.decision_maker == "Not found" and result.title == "Not found"
        assert result.confidence == "low"


def test_site_contacts_are_kept(monkeypatch):
    reply = ('{"decision_maker": "Anirban Chakraborty", "title": "Founder", "phone": "070018 08451", '
             '"email": "hello@karnikaevents.com", "confidence": "high"}')
    result = _enrich_with(
        monkeypatch, reply, [], company="Karnika Event Planners",
        site_text="Founded by Anirban Chakraborty.", site_emails=["hello@karnikaevents.com"],
        site_phones=["07001808451"],
    )
    assert result.decision_maker == "Anirban Chakraborty"
    assert result.email == "hello@karnikaevents.com" and result.phone == "070018 08451"


def test_draft_is_told_when_no_live_requirement_was_seen():
    client = _groq(['{"email_subject": "s", "email_body": "b", "whatsapp": "short one"}'])
    context = LeadContext(company_name="X", city="Y", requirement_context="Sells cases; no specific event requirement seen.")
    asyncio.run(client.draft(context, llm_module.Enrichment()))
    assert "Do not say or imply" in client.sent[0]["messages"][1]["content"]


# ---------- Groq free-tier rate limit ----------

def test_rate_limit_waits_and_retries(monkeypatch):
    calls, waits = [], []

    class FakeResponse:
        def __init__(self, status, payload, text=""):
            self.status_code, self._payload, self.text, self.headers = status, payload, text, {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

        async def post(self, url, headers=None, json=None):
            calls.append(json)
            if len(calls) == 1:
                return FakeResponse(429, {}, "Rate limit reached. Please try again in 2.5s.")
            return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_module.asyncio, "sleep", fake_sleep)
    reply = asyncio.run(GroqLLM(api_key="k")._chat([{"role": "user", "content": "hi"}], "m"))
    assert reply == "ok" and len(calls) == 2 and waits == [3.5]


def test_long_pages_are_trimmed_before_sending():
    client = _groq(['{"company_name": "Acme", "city": "Goa", "requirement_context": "x", "website": ""}',
                    '{"decision_maker": "Not found"}'])
    long_text = "home " * 3000 + "CONTACT PAGE Asha Rao founder"
    asyncio.run(client.extract_context(page_text=long_text, source_url="https://acme.in"))
    assert len(client.sent[0]["messages"][1]["content"]) < 6500
    asyncio.run(client.enrich(LeadContext(company_name="Acme", city="Goa"), site_text=long_text))
    sent = client.sent[1]["messages"][1]["content"]
    assert len(sent) < 6500 and "CONTACT PAGE Asha Rao founder" in sent   # the end of the site is kept