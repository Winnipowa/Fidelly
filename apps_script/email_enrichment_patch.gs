/**
 * Fidelly - extension NON DESTRUCTIVE pour l'enrichissement des emails.
 *
 * A coller dans le Code.gs du Web App deja utilise par SHEETS_WEBHOOK_URL.
 * Dans doPost(e), APRES parsing JSON + validation de WEBHOOK_TOKEN, ajouter :
 *
 *   const emailResponse = fidellyEmailEnrichmentRoute_(body);
 *   if (emailResponse) return emailResponse;
 *
 * Si ton objet JSON parse s'appelle autrement que `body`, utilise son vrai nom.
 * Puis Deploy > Manage deployments > Edit > New version > Deploy.
 */

const FIDELLY_EMAIL_SPREADSHEET_ID = '1-WoMQCgV_u1sIotCZtxb1tyyRz1wFfir5xG17RXzbbE';
const FIDELLY_EMAIL_SHEET_NAME = 'Prospects';

function fidellyEmailJson_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function fidellyEmailHeaderMap_(sheet) {
  const lastCol = sheet.getLastColumn();
  const headers = sheet.getRange(1, 1, 1, lastCol).getDisplayValues()[0];
  const map = {};
  headers.forEach((h, i) => {
    const key = String(h || '').trim();
    if (key) map[key] = i;
  });
  return {headers, map};
}

function fidellyEmailEnrichmentRoute_(body) {
  if (!body || !body.action) return null;
  if (body.action === 'list_missing_emails') {
    return fidellyEmailJson_(fidellyListMissingEmails_(body));
  }
  if (body.action === 'patch_email') {
    return fidellyEmailJson_(fidellyPatchEmail_(body));
  }
  return null;
}

function fidellyListMissingEmails_(body) {
  const ss = SpreadsheetApp.openById(FIDELLY_EMAIL_SPREADSHEET_ID);
  const sheet = ss.getSheetByName(FIDELLY_EMAIL_SHEET_NAME);
  if (!sheet) return {ok: false, message: 'Onglet Prospects introuvable'};

  const hm = fidellyEmailHeaderMap_(sheet);
  const headers = hm.headers;
  const map = hm.map;
  if (map.email === undefined || map.siret === undefined) {
    return {ok: false, message: 'Colonnes email/siret introuvables'};
  }

  const lastRow = sheet.getLastRow();
  const lastCol = sheet.getLastColumn();
  if (lastRow <= 1) return {ok: true, rows: [], next_cursor: null, done: true};

  const limit = Math.max(1, Math.min(200, Number(body.limit || 50)));
  const rawCursor = Math.max(0, Number(body.cursor || 0));
  const data = sheet.getRange(2, 1, lastRow - 1, lastCol).getDisplayValues();
  const cursor = data.length ? (rawCursor % data.length) : 0;
  const wanted = new Set([
    'priority','score','zone','keyword','name','legal_name','address','postcode','city',
    'siret','siren','naf','website','email','email_source','phone',
    'instagram','facebook','tiktok','linkedin'
  ]);

  const rows = [];
  let i = cursor;
  for (; i < data.length && rows.length < limit; i++) {
    const values = data[i];
    const siret = String(values[map.siret] || '').trim();
    const email = String(values[map.email] || '').trim();
    if (!siret || email) continue;
    const row = {_sheet_row: i + 2};
    headers.forEach((h, col) => {
      if (wanted.has(h)) row[h] = values[col];
    });
    rows.push(row);
  }

  const done = i >= data.length;
  return {ok: true, rows, next_cursor: done ? null : i, done};
}

function fidellyPatchEmail_(body) {
  const updates = Array.isArray(body.updates) ? body.updates : [];
  if (!updates.length) return {ok: true, written: 0, skipped_existing: 0, not_found: 0};

  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const ss = SpreadsheetApp.openById(FIDELLY_EMAIL_SPREADSHEET_ID);
    const sheet = ss.getSheetByName(FIDELLY_EMAIL_SHEET_NAME);
    if (!sheet) return {ok: false, message: 'Onglet Prospects introuvable'};

    const map = fidellyEmailHeaderMap_(sheet).map;
    if (map.siret === undefined || map.email === undefined || map.email_source === undefined) {
      return {ok: false, message: 'Colonnes siret/email/email_source introuvables'};
    }

    const lastRow = sheet.getLastRow();
    if (lastRow <= 1) return {ok: true, written: 0, skipped_existing: 0, not_found: updates.length};
    const sirets = sheet.getRange(2, map.siret + 1, lastRow - 1, 1)
      .getDisplayValues().map(r => String(r[0] || '').trim());
    const rowBySiret = {};
    sirets.forEach((siret, idx) => {
      if (siret && rowBySiret[siret] === undefined) rowBySiret[siret] = idx + 2;
    });

    let written = 0, skippedExisting = 0, notFound = 0;
    const results = [];
    updates.forEach(update => {
      const siret = String(update.siret || '').trim();
      const email = String(update.email || '').trim();
      const source = String(update.email_source || '').trim();
      const rowNum = rowBySiret[siret];
      if (!siret || !email || !rowNum) {
        notFound++;
        results.push({siret, status: 'not_found_or_invalid'});
        return;
      }

      const emailCell = sheet.getRange(rowNum, map.email + 1);
      const currentEmail = String(emailCell.getDisplayValue() || '').trim();
      if (currentEmail) {
        skippedExisting++;
        results.push({siret, status: 'skipped_existing'});
        return;
      }

      emailCell.setValue(email);
      sheet.getRange(rowNum, map.email_source + 1).setValue(source);
      written++;
      results.push({siret, status: 'written', confidence: Number(update.confidence || 0)});
    });
    SpreadsheetApp.flush();
    return {ok: true, written, skipped_existing: skippedExisting, not_found: notFound, results};
  } finally {
    lock.releaseLock();
  }
}
