"""Output destination and lead list.

Three ways to store leads, picked in this order:
  1. Apps Script web app (APPS_SCRIPT_URL): no Google Cloud project needed.
  2. Google Sheet via a service account (GOOGLE_SHEET_ID + credentials).
  3. A local CSV file (leads.csv), if neither is set up.

Rows are never lost: if a Google write fails, the row is written to the CSV instead.
"""
import csv
import json
import logging
import os

import httpx

from .config import settings
from .models import HEADERS, Lead

log = logging.getLogger("copilot")

# Statuses a human can set from the leads table.
STATUSES = ["Pending Review", "Approved", "Sent", "Replied", "Not interested"]
STATUS_COL = HEADERS.index("Status")  # 0-based column index
# Columns the reviewer may edit from the page.
EDITABLE_FIELDS = ["Draft: Email", "Draft: WhatsApp", "Notes"]

_worksheet = None  # cached between requests


def _backend() -> str:
    if settings.apps_script_url:
        return "script"
    has_creds = bool(settings.google_credentials_json) or os.path.exists(settings.google_credentials_file)
    if settings.google_sheet_id and has_creds:
        return "gspread"
    return "csv"


# ---------- Apps Script web app ----------

def _script(action: str, **payload) -> dict:
    """Send one request to the Apps Script web app and return its JSON reply."""
    body = {"token": settings.apps_script_token, "action": action, **payload}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.post(settings.apps_script_url, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apps Script error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script: {data.get('error', 'unknown error')}")
    return data


# ---------- Google Sheet via service account (gspread) ----------

def _get_worksheet():
    global _worksheet
    if _worksheet is not None:
        return _worksheet
    import gspread

    if settings.google_credentials_json:
        client = gspread.service_account_from_dict(json.loads(settings.google_credentials_json))
    else:
        client = gspread.service_account(filename=settings.google_credentials_file)
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    try:
        ws = spreadsheet.worksheet(settings.google_worksheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(settings.google_worksheet_name, rows=1000, cols=len(HEADERS))
    if ws.row_values(1) != HEADERS:
        ws.update(values=[HEADERS], range_name="A1")
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    _worksheet = ws
    return ws


def sheets_configured() -> bool:
    return _backend() != "csv"


def sheet_url() -> str:
    if not settings.google_sheet_id:
        return ""
    return f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"


def backend_name() -> str:
    return "Google Sheet" if sheets_configured() else "leads.csv"


# ---------- local CSV ----------

def _read_csv_rows() -> list[list[str]]:
    path = settings.csv_fallback_path
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def _write_csv_rows(rows: list[list[str]]) -> None:
    with open(settings.csv_fallback_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def _append_csv(lead: Lead) -> str:
    path = settings.csv_fallback_path
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(HEADERS)
        writer.writerow(lead.to_row())
    lead.row = len(_read_csv_rows())
    return f"local file {path}"


# ---------- writing ----------

def append_lead(lead: Lead) -> str:
    """Save the lead, set lead.row, and return a short description of where it went."""
    backend = _backend()
    if backend != "csv":
        try:
            if backend == "script":
                data = _script("append", headers=HEADERS, row=lead.to_row())
                lead.row = int(data["row"])
            else:
                ws = _get_worksheet()
                ws.append_row(lead.to_row(), value_input_option="RAW")
                lead.row = len(ws.col_values(1))
            return "Google Sheet"
        except Exception as exc:  # network, permissions, quota: never lose the lead
            log.warning("Google Sheet write failed (%s); falling back to CSV", exc)
            return _append_csv(lead) + " (Google Sheet write failed)"
    return _append_csv(lead)


# ---------- reading and updates ----------

def list_leads(limit: int = 200) -> list[dict]:
    """All leads, newest first. Each dict has the sheet headers plus '_row' (the row number to update)."""
    backend = _backend()
    if backend == "script":
        rows = _script("list")["rows"]
    elif backend == "gspread":
        rows = _get_worksheet().get_all_values()
    else:
        rows = _read_csv_rows()
    if len(rows) < 2:
        return []
    header, body = rows[0], rows[1:]
    leads = []
    for row_number, values in enumerate(body, start=2):  # row 1 is the header
        padded = values + [""] * (len(header) - len(values))
        lead = dict(zip(header, padded))
        lead["_row"] = row_number
        leads.append(lead)
    return leads[::-1][:limit]


def _update_cells(row: int, updates: dict[str, str]) -> None:
    """Write named columns of one row, in whichever backend is active."""
    backend = _backend()
    if backend == "script":
        _script("update", row=row, updates=updates)
    elif backend == "gspread":
        ws = _get_worksheet()
        for name, value in updates.items():
            ws.update_cell(row, HEADERS.index(name) + 1, value)
    else:
        rows = _read_csv_rows()
        if row < 2 or row > len(rows):
            raise LookupError(f"No lead in row {row}")
        # Older CSV files may not have the newer columns yet: add them at the end.
        header = rows[0]
        for name in HEADERS:
            if name not in header:
                header.append(name)
        for r in rows:
            r.extend([""] * (len(header) - len(r)))
        for name, value in updates.items():
            rows[row - 1][header.index(name)] = value
        _write_csv_rows(rows)


def set_status(row: int, status: str) -> None:
    """Set the Status cell of one lead. `row` is the row number from list_leads()."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    if _backend() == "script":
        _script("update", row=row, updates={"Status": status})
        return
    _update_cells(row, {"Status": status})


def update_fields(row: int, updates: dict[str, str]) -> None:
    """Save edited drafts and notes for one lead. Only EDITABLE_FIELDS are accepted."""
    for name in updates:
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"Field cannot be edited: {name}")
    _update_cells(row, updates)