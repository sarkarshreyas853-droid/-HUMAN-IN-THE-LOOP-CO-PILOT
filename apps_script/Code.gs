// Google Apps Script web app that lets the copilot write to the Google Sheet
// without a Google Cloud service account.
//
// Setup: open the sheet, Extensions > Apps Script, paste this file, set TOKEN,
// then Deploy > New deployment > Web app (Execute as: Me, Access: Anyone).
// Put the web app URL and the same token in .env as APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN.

const TOKEN = "PASTE-YOUR-SECRET-TOKEN-HERE";
const TAB_NAME = "Leads";

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.token !== TOKEN) {
    return reply({ ok: false, error: "bad token" });
  }
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TAB_NAME);
  if (!sheet) {
    return reply({ ok: false, error: "No tab named Leads" });
  }

  if (body.action === "append") {
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(body.headers);
      sheet.setFrozenRows(1);
      sheet.getRange(1, 1, 1, body.headers.length).setFontWeight("bold");
    }
    sheet.appendRow(body.row);
    return reply({ ok: true, row: sheet.getLastRow() });
  }

  if (body.action === "list") {
    return reply({ ok: true, rows: sheet.getDataRange().getValues() });
  }

  if (body.action === "update") {
    const lastColumn = sheet.getLastColumn();
    const header = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
    for (const name of Object.keys(body.updates)) {
      const col = header.indexOf(name) + 1;
      if (col === 0) {
        return reply({ ok: false, error: "Unknown column: " + name });
      }
      sheet.getRange(body.row, col).setValue(body.updates[name]);
    }
    return reply({ ok: true });
  }

  return reply({ ok: false, error: "Unknown action" });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}