# GoPratle Lead Enrichment & Drafting Copilot

A human-in-the-loop copilot for event-season outreach. Send it an agency link or a
screenshot of a crew or hiring notice. It works out which company it is, finds the likely
decision-maker and public contact details, drafts an email and a WhatsApp message, and
saves everything as a row marked **Pending Review**. A person checks the row and sends the
message. Nothing is ever sent automatically.

**Live demo:** https://human-in-the-loop-co-pilot-1.onrender.com (free tier, so the first load can take up to a minute).

## How it works

```
 Web form  ─┐
            ├─►  FastAPI  ─►  1. Read the page (httpx + BeautifulSoup) or the screenshot
 Telegram ──┘                 2. Identify the company  (LLM: company, city, requirement)
                              3. Find decision-maker + contacts (site data + web search)
                              4. Draft email (3 sentences) + WhatsApp (under 60 words)
                              5. Save row to Google Sheet (or leads.csv)  ─►  human reviews and sends
```

Each step reports a short progress message, and the web page shows them as they happen.

| Brief requirement | Where it lives |
|---|---|
| Inbound trigger (web form + Telegram bot) | `app/main.py`, `app/telegram.py`, `app/templates/index.html` |
| URL or screenshot input | Both triggers accept either. Screenshots go to the vision model. |
| Context extraction (agency, city, requirement) | `GroqLLM.extract_context` in `app/llm.py` |
| Decision-maker lookup (Production Head > Ops Director > Head of Events > Founder) | `GroqLLM.enrich` and `ENRICH_SYSTEM` in `app/prompts.py` |
| Web search for the decision-maker | `app/search.py` (Tavily, optional) |
| Contact retrieval (phone/WhatsApp, email, LinkedIn) | Regex on the company site (`app/scraper.py`) plus web search |
| Email draft (3 sentences) and WhatsApp draft (under 60 words) | `GroqLLM.draft` and `DRAFT_SYSTEM`. The word limit is checked in code. |
| Output to Google Sheet with `Status: Pending Review` | `app/sheets.py`, `apps_script/Code.gs` |

Sheet columns: `Company Name | City | Decision Maker | Title | Contact Details | Draft: Email | Draft: WhatsApp | Status`,
then `Requirement Context | Confidence | Sources | Input | Channel | Created At | Notes`.
The helper columns let the reviewer check where a name came from before sending.

## Using the web page

Open http://localhost:8000 after you start the server.

- **Submit a lead.** Paste a link or upload a screenshot (JPG, PNG, WEBP or GIF, up to 5 MB).
  Progress steps appear while it runs.
- **Edit before sending.** The email, the WhatsApp message and the notes are editable.
  The WhatsApp word count is shown as you type. Click **Save changes** to store them.
- **Track status.** Each lead has a status: `Pending Review`, `Approved`, `Sent`, `Replied`
  or `Not interested`. Change it from the table.
- **Find leads.** The "All leads" table has a search box and a status filter. **View** opens a lead.

## Setup

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then create a file named `.env` in this folder with your settings. `.env` is git-ignored,
so keys never reach the repo:

```
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
GROQ_VISION_MODEL=qwen/qwen3.8-27b
TAVILY_API_KEY=
APPS_SCRIPT_URL=
APPS_SCRIPT_TOKEN=
GOOGLE_SHEET_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
SENDER_NAME=Team GoPratle
```

### 2. Groq API key (free, no card needed)

1. Create a key at https://console.groq.com.
2. Set `GROQ_API_KEY` in `.env`.
3. Check the model names. Groq retires models over time, so set these to current ones:
   - `GROQ_MODEL` (text): default `openai/gpt-oss-120b`
   - `GROQ_VISION_MODEL` (screenshots): default `qwen/qwen3.8-27b`

   The current list is at https://console.groq.com/docs/models and retirements are at
   https://console.groq.com/docs/deprecations. A retired name returns `model_not_found`.

### 3. Tavily key for web search (free, optional)

Create a key at https://tavily.com and set `TAVILY_API_KEY`. Web search is used to find the
decision-maker. It is capped by `MAX_WEB_SEARCHES` (default 3). Leave the key blank to
use only the company's own website.

### 4. Google Sheet

Without this, rows are written to `leads.csv`, so you can test first. Create a blank Google
Sheet, name its first tab `Leads`, and put the sheet ID from its URL
(`docs.google.com/spreadsheets/d/<THIS_PART>/edit`) in `GOOGLE_SHEET_ID`. Then pick one option.

**Option A: Apps Script web app (no Google Cloud project needed).**

1. In the sheet, open **Extensions > Apps Script** and paste in `apps_script/Code.gs`.
2. Set `TOKEN` on the first line to a long random string.
3. **Deploy > New deployment > Web app**, with "Execute as: Me" and "Who has access: Anyone".
4. Put the web app URL in `APPS_SCRIPT_URL` and the same token in `APPS_SCRIPT_TOKEN`.

**Option B: service account.**

1. In Google Cloud Console, create a project, enable the **Google Sheets API**, create a
   **Service Account**, and download a **JSON key**.
2. Save the key as `service_account.json` in this folder.
3. Copy the `client_email` from the JSON and **Share** the sheet with it as Editor.

The header row is created on the first lead. If `APPS_SCRIPT_URL` is set, it is used first.

### 5. Telegram bot (optional)

Message **@BotFather** on Telegram, send `/newbot`, and put the token in `TELEGRAM_BOT_TOKEN`.
Set `TELEGRAM_WEBHOOK_SECRET` to a long random string.

## Run

```bash
uvicorn app.main:app --reload
```

- Web page: http://localhost:8000
- Config check: http://localhost:8000/health (shows which keys are set, without showing the keys)

**Telegram, local demo (no public URL needed).** In a second terminal:

```bash
python run_telegram_polling.py
```

**Telegram webhook (deployed).** Once the app has a public HTTPS URL:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-host>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>"
```

The webhook returns 200 straight away and processes the lead in the background. The bot
then replies in the chat with the row and both drafts.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web page |
| POST | `/api/leads/stream` | Submit a lead. Streams progress steps as NDJSON, then the result. |
| POST | `/api/leads` | Submit a lead in one response (no progress steps) |
| GET | `/api/leads` | List saved leads, newest first |
| POST | `/api/leads/{row}/status` | Set a lead's status |
| POST | `/api/leads/{row}/fields` | Save edited drafts and notes (only these fields are editable) |
| POST | `/telegram/webhook/{secret}` | Telegram webhook |
| GET | `/health` | Config check |

## Tests

```bash
pytest -q
```

The tests fake the LLM and the network, so they run without API keys. They cover JSON
parsing, the URL safety check, the sheet row shape, the pipeline for URL and screenshot
inputs, the WhatsApp word-limit retry, the progress stream, editing and status updates,
the web page and the Telegram webhook.

## Design decisions

- **Three small prompts, not one big one.** Extract, enrich and draft are separate calls,
  so each can be tuned and tested alone. A weak lookup does not corrupt the drafts.
- **"Not found" beats a guess.** The prompts forbid inventing names or contact details.
  Each row carries a confidence level and source links. A wrong name in outreach costs
  more than a blank cell.
- **Human in the loop.** Nothing is sent. Every row starts as `Pending Review`.
- **Hard limits enforced in code.** If the WhatsApp draft is 60 words or more, the copilot
  asks once for a shorter version.
- **Never lose a lead.** If the Google Sheet write fails, the row falls back to a CSV.
- **Only public business contacts.** The lookup uses details published for business use
  (the company site and official pages).
- **Basic abuse protection.** Fetching is limited to public http(s) addresses, uploads are
  capped at 5 MB, and the Telegram webhook path carries a secret.

## Deployment

The live demo runs on Render's free tier as a Python web service:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- The same settings as `.env` are entered as environment variables in Render.

## Known limits

- LinkedIn and Instagram block plain scraping. For those links the copilot relies on web
  search and whatever the URL reveals. A screenshot of the post works better.
- There is no headless browser, so pages rendered entirely in JavaScript return thin text.
- Small agencies often have no Production Head. The copilot then falls back to the founder.
- Free-tier limits apply on Groq and Tavily. A large batch may hit rate limits.

## Tuning

- `app/prompts.py` holds every prompt. **`GOPRATLE_PITCH` is the paragraph the drafts are
  built on. Edit it to match GoPratle's real offer before using the drafts.**
- `SENDER_NAME` in `.env` sets the sign-off.
- `GROQ_MODEL`, `GROQ_VISION_MODEL` and `MAX_WEB_SEARCHES` in `.env` change the models and the search budget.