"""Google Sheets portal-application tracker (spec section 11). SQLite stays the source of truth;
the sheet is the interface. Rows are matched by ID (job_key), never by row number, so sorting
or filtering the sheet is safe. The tool only appends rows and only edits cells it owns."""
import json
import logging
import re
from datetime import datetime, timedelta

from db import repo

log = logging.getLogger(__name__)

TITLE = "Job Tracker"
RESUME_FOLDER = "Job Tracker Resumes"
APPS = "Applications"
KITS = "Kits"
NETWORK = "Network"
ARCHIVE = "Archive"
HEADERS = {
    APPS: ["ID", "Added", "Company", "Role", "Apply link", "Resume", "Fit", "Why", "Kit", "Status", "Notes", "Outcome"],
    KITS: ["ID", "Company", "Apply link", "Cover note", "Form answers", "Standard fields"],
    NETWORK: ["ID", "Company", "Linked job", "Person", "Title", "LinkedIn", "Source", "Verified",
              "Message (send after they accept)", "Status", "Notes"],
    ARCHIVE: ["Archived", "From", "ID", "Company", "Role / person", "Status", "Outcome", "Notes", "Link"],
}
STATUS = ["pending", "applied", "discarded", "expired"]
OUTCOME = ["replied", "interview", "rejected", "offer"]
NETWORK_STATUS = ["to_connect", "requested", "connected", "skipped", "closed"]
ARCHIVE_APPS = {"discarded", "expired"}          # Applications rows moved to Archive (or Outcome rejected)
ARCHIVE_NETWORK = {"skipped", "closed"}
COL = {name: i for i, name in enumerate(HEADERS[APPS])}
# What you see in the sheet vs what SQLite stores. Sync accepts either form (old rows keep working).
LABEL = {"pending": "Pending", "applied": "Applied", "discarded": "Not interested", "expired": "Expired",
         "replied": "Replied", "interview": "Interview", "rejected": "Rejected", "offer": "Offer",
         "to_connect": "To connect", "requested": "Requested", "connected": "Connected", "skipped": "Skipped",
         "closed": "Closed"}
SOURCE_LABEL = {"yc_page": "YC profile", "team_page": "Company site", "search": "LinkedIn search",
                "listing": "Job listing"}
VERIFIED_LABEL = {True: "\u2713 Verified", False: "Check first"}


def label(key: str | None) -> str:
    return LABEL.get(key or "", key or "")


def to_key(text: str | None) -> str:
    """'To connect' / 'to_connect' / 'Not interested' -> stored status key."""
    t = (text or "").strip()
    for key, shown in LABEL.items():
        if t.lower() in (key, shown.lower()):
            return key
    return t.lower()
FORMULA_START = ("=", "+", "-", "@")


def safe(value) -> str:
    """Text from listings/Claude must never become a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(FORMULA_START) else s


def hyperlink(url: str | None, label: str) -> str:
    if not url:
        return safe(label)
    return f'=HYPERLINK("{url.replace(chr(34), "%22")}","{label.replace(chr(34), "")}")'


# ---------------------------------------------------------------- Google API wrapper

class SheetsApi:
    def __init__(self, sheets, drive, execute):
        self.sheets, self.drive, self.execute = sheets.spreadsheets(), drive, execute

    def create(self) -> tuple[str, dict]:
        body = {"properties": {"title": TITLE},
                "sheets": [{"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}} for t in HEADERS]}
        ss = self.execute(self.sheets.create(body=body))
        sid = ss["spreadsheetId"]
        gids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in ss["sheets"]}
        for tab, header in HEADERS.items():
            self.append(sid, tab, [header], raw=True)
        self.execute(self.sheets.batchUpdate(spreadsheetId=sid, body={"requests": _format_requests(gids)}))
        self.apply_layout(sid, gids)
        return sid, gids

    def ensure_tab(self, sid: str, tab: str) -> None:
        """Add a tab (with its header) to an existing tracker, e.g. Archive."""
        if tab in self.gids(sid):
            return
        self.execute(self.sheets.batchUpdate(spreadsheetId=sid, body={"requests": [
            {"addSheet": {"properties": {"title": tab, "gridProperties": {"frozenRowCount": 1}}}}]}))
        self.append(sid, tab, [HEADERS[tab]], raw=True)

    def delete_rows(self, sid: str, tab: str, row_numbers: list[int]) -> None:
        """Delete 1-based rows, bottom first so the remaining numbers stay valid."""
        if not row_numbers:
            return
        gid = self.gids(sid)[tab]
        reqs = [{"deleteDimension": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
                for n in sorted(set(row_numbers), reverse=True)]
        self.execute(self.sheets.batchUpdate(spreadsheetId=sid, body={"requests": reqs}))

    def apply_layout(self, sid: str, gids: dict | None = None) -> None:
        """Column widths, wrapping, header style and a 1-row freeze. Idempotent: safe every run."""
        gids = gids or self.gids(sid)
        meta = self.execute(self.sheets.get(spreadsheetId=sid, fields="sheets(properties(sheetId),bandedRanges(bandedRangeId),"
                                                                       "conditionalFormats)"))
        requests = layout_requests(gids) + style_requests(gids, meta)
        self.execute(self.sheets.batchUpdate(spreadsheetId=sid, body={"requests": requests}))
        fit = fit_rows_requests(gids, {tab: self.read(sid, tab) for tab in LAYOUT})
        if fit:
            self.execute(self.sheets.batchUpdate(spreadsheetId=sid, body={"requests": fit}))

    def exists(self, sid: str) -> bool:
        try:
            self.execute(self.sheets.get(spreadsheetId=sid, fields="spreadsheetId"))
            return True
        except Exception as e:
            if getattr(getattr(e, "resp", None), "status", None) in (403, 404):
                return False
            raise

    def gids(self, sid: str) -> dict:
        ss = self.execute(self.sheets.get(spreadsheetId=sid, fields="sheets.properties"))
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in ss["sheets"]}

    def read(self, sid: str, tab: str, formulas: bool = False) -> list[list[str]]:
        res = self.execute(self.sheets.values().get(spreadsheetId=sid, range=f"{tab}!A:Z",
                                                    valueRenderOption="FORMULA" if formulas else "FORMATTED_VALUE"))
        return res.get("values", [])

    def append(self, sid: str, tab: str, rows: list[list], raw: bool = False) -> str:
        res = self.execute(self.sheets.values().append(
            spreadsheetId=sid, range=f"{tab}!A1", valueInputOption="RAW" if raw else "USER_ENTERED",
            insertDataOption="OVERWRITE", body={"values": rows}))
        return res.get("updates", {}).get("updatedRange", "")

    def update_cell(self, sid: str, a1: str, value: str) -> None:
        self.execute(self.sheets.values().update(spreadsheetId=sid, range=a1, valueInputOption="RAW",
                                                 body={"values": [[value]]}))

    def create_folder(self, name: str) -> str:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        return self.execute(self.drive.files().create(body=meta, fields="id"))["id"]

    def upload_pdf(self, path, name: str, folder_id: str) -> tuple[str, str]:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype="application/pdf")
        f = self.execute(self.drive.files().create(body={"name": name, "parents": [folder_id]}, media_body=media,
                                                   fields="id, webViewLink"))
        return f["id"], f.get("webViewLink", "")


# tab -> [(width px, wrap)] per column, in header order. Long text wraps in wide columns; everything
# else is clipped to one line so rows stay a readable height.
LAYOUT = {
    APPS: [(70, False), (90, False), (150, False), (260, False), (70, False), (120, False), (50, False),
           (380, True), (50, False), (100, False), (240, True), (100, False)],
    KITS: [(70, False), (150, False), (220, False), (440, True), (560, True), (320, True)],
    ARCHIVE: [(90, False), (100, False), (70, False), (150, False), (240, False), (100, False), (100, False),
              (300, True), (90, False)],
    NETWORK: [(70, False), (140, False), (120, False), (160, False), (130, False), (110, False), (100, False),
              (100, False), (520, True), (110, False), (220, True)],
}
HEADER_BG = {"red": 0.93, "green": 0.94, "blue": 0.96}


def layout_requests(gids: dict) -> list[dict]:
    reqs = []
    for tab, cols in LAYOUT.items():
        gid = gids[tab]
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                                               "fields": "gridProperties.frozenRowCount"}})
        for i, (width, wrap) in enumerate(cols):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": width}, "fields": "pixelSize"}})
            reqs.append({"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": i, "endColumnIndex": i + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP" if wrap else "CLIP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
        reqs.append({"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(cols)},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": HEADER_BG,
                                           "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy,verticalAlignment)"}})
    return reqs


LINE_PX, CHAR_PX, MAX_ROW_PX = 17, 7, 420


def row_height(row: list, cols: list[tuple[int, bool]]) -> int:
    """Pixel height that shows a row's wrapped text: lines needed in its tallest wrapped cell."""
    lines = 1
    for (width, wrap), value in zip(cols, row):
        if not wrap or not value:
            continue
        per_line = max(10, width // CHAR_PX)
        need = sum(max(1, -(-len(part) // per_line)) for part in str(value).split("\n"))
        lines = max(lines, need)
    return min(MAX_ROW_PX, 8 + lines * LINE_PX)


def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


TAB_COLOR = {APPS: "#1a73e8", KITS: "#8430ce", NETWORK: "#12b5cb", ARCHIVE: "#9aa0a6"}
HEADER_FILL, HEADER_TEXT = "#1f2a44", "#ffffff"
BAND_A, BAND_B = "#ffffff", "#f6f8fb"
# (tab, column, value) -> (fill, text); whole-row rules use column None
STATUS_COLORS = {
    APPS: {"pending": ("#e8f0fe", "#1967d2"), "applied": ("#e6f4ea", "#137333"),
           "discarded": ("#f1f3f4", "#5f6368"), "expired": ("#f1f3f4", "#80868b")},
    NETWORK: {"to_connect": ("#e8f0fe", "#1967d2"), "requested": ("#fef7e0", "#b06000"),
              "connected": ("#e6f4ea", "#137333"), "skipped": ("#f1f3f4", "#5f6368"),
              "closed": ("#f1f3f4", "#5f6368")},
}
OUTCOME_COLORS = {"interview": ("#e6f4ea", "#137333"), "offer": ("#ceead6", "#0d652d"),
                  "replied": ("#e8f0fe", "#1967d2"), "rejected": ("#fce8e6", "#c5221f")}
VERIFIED_COLORS = {VERIFIED_LABEL[True]: ("#e6f4ea", "#137333"), VERIFIED_LABEL[False]: ("#fef7e0", "#b06000")}
LINK_COLS = {APPS: ["Apply link", "Resume", "Kit"], KITS: ["Apply link"], NETWORK: ["LinkedIn"], ARCHIVE: ["Link"]}
LINK_TEXT = "#1a73e8"


def _col_range(gid, col, rows_from=1):
    return {"sheetId": gid, "startRowIndex": rows_from, "startColumnIndex": col, "endColumnIndex": col + 1}


def _text_rule(gid, col, value, fill, text, bold=True):
    return {"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [_col_range(gid, col)],
        "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                        "format": {"backgroundColor": _rgb(fill), "textFormat": {"foregroundColor": _rgb(text),
                                                                                 "bold": bold}}}}}}


def style_requests(gids: dict, meta: dict) -> list[dict]:
    """Header, tab colours, banding, hidden gridlines, status chips, fit colour scale, dropdowns.
    Idempotent: our conditional rules are removed and re-added; banding is added once."""
    reqs = [r for r in _format_requests(gids)]   # dropdowns (re-set so new options, e.g. closed, appear)
    sheets = {s["properties"]["sheetId"]: s for s in meta.get("sheets", [])}
    for tab, gid in gids.items():
        if tab not in LAYOUT:
            continue
        ncols = len(LAYOUT[tab])
        existing = sheets.get(gid, {})
        for _ in existing.get("conditionalFormats", []):   # drop old rules (always index 0)
            reqs.append({"deleteConditionalFormatRule": {"sheetId": gid, "index": 0}})
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": gid, "tabColorStyle": {"rgbColor": _rgb(TAB_COLOR[tab])},
                           "gridProperties": {"hideGridlines": True}},
            "fields": "tabColorStyle,gridProperties.hideGridlines"}})
        reqs.append({"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"backgroundColor": _rgb(HEADER_FILL), "verticalAlignment": "MIDDLE",
                                           "textFormat": {"bold": True, "foregroundColor": _rgb(HEADER_TEXT),
                                                          "fontSize": 10},
                                           "padding": {"top": 6, "bottom": 6, "left": 6, "right": 6}}},
            "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat,padding)"}})
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0,
                                                             "endIndex": 1},
                                                   "properties": {"pixelSize": 34}, "fields": "pixelSize"}})
        reqs.append({"repeatCell": {   # body text
            "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 10, "foregroundColor": _rgb("#202124")},
                                           "padding": {"top": 4, "bottom": 4, "left": 6, "right": 6}}},
            "fields": "userEnteredFormat(textFormat,padding)"}})
        reqs.append({"repeatCell": {   # ID column: small and grey (the tool needs it, you rarely do)
            "range": _col_range(gid, 0),
            "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 8, "foregroundColor": _rgb("#9aa0a6")}}},
            "fields": "userEnteredFormat.textFormat"}})
        if not existing.get("bandedRanges"):
            reqs.append({"addBanding": {"bandedRange": {
                "range": {"sheetId": gid, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": ncols},
                "rowProperties": {"headerColor": _rgb(HEADER_FILL), "firstBandColor": _rgb(BAND_A),
                                  "secondBandColor": _rgb(BAND_B)}}}})
        status_col = HEADERS[tab].index("Status") if "Status" in HEADERS[tab] else None
        for value, (fill, text) in STATUS_COLORS.get(tab, {}).items():
            reqs.append(_text_rule(gid, status_col, label(value), fill, text))
        for name in LINK_COLS.get(tab, []):   # links look like links: blue + underlined
            reqs.append({"repeatCell": {"range": _col_range(gid, HEADERS[tab].index(name)),
                                        "cell": {"userEnteredFormat": {"textFormat": {
                                            "foregroundColor": _rgb(LINK_TEXT), "underline": True}}},
                                        "fields": "userEnteredFormat.textFormat(foregroundColor,underline)"}})
        if tab == APPS:
            col_letter = chr(ord("A") + COL["Status"])
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {   # expired rows fade out
                "ranges": [{"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": ncols}],
                "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                              "values": [{"userEnteredValue": f'=${col_letter}2="{label("expired")}"'}]},
                                "format": {"textFormat": {"foregroundColor": _rgb("#9aa0a6"), "strikethrough": True}}}}}})
            for value, (fill, text) in OUTCOME_COLORS.items():
                reqs.append(_text_rule(gid, COL["Outcome"], label(value), fill, text))
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {   # fit: pale to green
                "ranges": [_col_range(gid, COL["Fit"])],
                "gradientRule": {"minpoint": {"color": _rgb("#ffffff"), "type": "NUMBER", "value": "50"},
                                 "maxpoint": {"color": _rgb("#57bb8a"), "type": "NUMBER", "value": "100"}}}}})
            reqs.append({"repeatCell": {"range": _col_range(gid, COL["Fit"]),
                                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER",
                                                                       "textFormat": {"bold": True}}},
                                        "fields": "userEnteredFormat(horizontalAlignment,textFormat.bold)"}})
        if tab == NETWORK:
            for value, (fill, text) in VERIFIED_COLORS.items():
                reqs.append(_text_rule(gid, HEADERS[NETWORK].index("Verified"), value, fill, text, bold=False))
    return reqs


def fit_rows_requests(gids: dict, rows_by_tab: dict[str, list[list]]) -> list[dict]:
    """Explicit row heights from content (data rows only; the header keeps its own height)."""
    reqs = []
    for tab, rows in rows_by_tab.items():
        for n, row in enumerate(rows[1:], start=1):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": gids[tab], "dimension": "ROWS", "startIndex": n, "endIndex": n + 1},
                "properties": {"pixelSize": row_height(row, LAYOUT[tab])}, "fields": "pixelSize"}})
    return reqs


def _format_requests(gids: dict) -> list[dict]:
    def validation(gid, col, values):
        return {"setDataValidation": {
            "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                     "showCustomUi": True, "strict": False}}}

    a, k, n = gids[APPS], gids[KITS], gids[NETWORK]
    # Clear every tab's dropdowns first: when columns move, old dropdowns must not stay behind.
    clear = [{"setDataValidation": {"range": {"sheetId": g, "startRowIndex": 1}}} for g in gids.values()]
    return clear + [
        validation(a, COL["Status"], [label(v) for v in STATUS]), validation(a, COL["Outcome"], [label(v) for v in OUTCOME]),
        validation(n, HEADERS[NETWORK].index("Status"), [label(v) for v in NETWORK_STATUS]),
    ]


# ---------------------------------------------------------------- rows

def application_row(job: dict, app: dict, fit: int | None, why: str, resume_link: str | None, kit_link: str) -> list:
    return [app["job_key"], app["created_at"][:10], safe(job["company"]), safe(job["title"]),
            hyperlink(job.get("apply_url"), "Apply \u2197"), hyperlink(resume_link, f"{app['resume_key']} PDF \u2197"),
            fit if fit is not None else "", safe(why), kit_link, label(app["status"]), "", ""]


def kit_row(job: dict, kit: dict, standard: str) -> list:
    answers = "\n\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in kit.get("form_answers", []))
    return [job["job_key"], job["company"], job.get("apply_url") or "", kit["cover_note"], answers, standard]


def network_row(row: dict, job: dict, linked: str) -> list:
    return [row["id"], safe(job["company"]), linked, safe(row["name"]), safe(row.get("title")),
            hyperlink(row.get("linkedin_url"), "Search LinkedIn \u2197" if row.get("source") == "search"
                      else "Open profile \u2197"),
            SOURCE_LABEL.get(row.get("source"), row.get("source")), VERIFIED_LABEL[bool(row.get("verified"))],
            safe(row.get("after_accept_message")), label(row["status"]), ""]


def enqueue(conn, tab: str, job_key: str, row: list, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO sheet_queue (tab, job_key, row, created_at) VALUES (?,?,?,?)",
                 (tab, job_key, json.dumps(row), repo.iso(now)))


def _row_number(updated_range: str) -> int | None:
    m = re.search(r"![A-Z]+(\d+)", updated_range or "")
    return int(m.group(1)) if m else None


def flush_queue(conn, api: SheetsApi, sid: str, now: datetime) -> dict:
    """Append queued rows (Kits first, so Applications rows can link to them). Failures stay queued."""
    out = {"written": 0, "queued": 0, "error": None}
    pending = conn.execute("SELECT * FROM sheet_queue WHERE written_at IS NULL "
                           "ORDER BY CASE tab WHEN 'Kits' THEN 0 ELSE 1 END, id").fetchall()
    if not pending:
        return out
    try:
        gids = api.gids(sid)
        kit_rows = {}
        for item in pending:
            row = json.loads(item["row"])
            if item["tab"] == APPS and row[COL["Kit"]] == "__KIT__":
                kit_row_no = kit_rows.get(item["job_key"]) or _find_row(api, sid, KITS, item["job_key"])
                # Found by ID at click time, so archiving or sorting Kits never breaks the link.
                row[COL["Kit"]] = (f'=HYPERLINK("#gid={gids[KITS]}&range=A"&MATCH(INDIRECT("A"&ROW()),'
                                   f'{KITS}!A:A,0),"Kit")' if kit_row_no else "")
            rng = api.append(sid, item["tab"], [row], raw=item["tab"] == KITS)
            if item["tab"] == KITS:
                kit_rows[item["job_key"]] = _row_number(rng)
            conn.execute("UPDATE sheet_queue SET written_at=? WHERE id=?", (repo.iso(now), item["id"]))
            out["written"] += 1
    except Exception as e:   # never fails the pipeline; rows stay queued for next run
        log.warning("sheet write failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    out["queued"] = conn.execute("SELECT COUNT(*) FROM sheet_queue WHERE written_at IS NULL").fetchone()[0]
    return out


def _find_row(api, sid, tab, job_key) -> int | None:
    for n, row in enumerate(api.read(sid, tab), 1):
        if row and row[0] == job_key:
            return n
    return None


# ---------------------------------------------------------------- sync (sheet -> SQLite)

def sync_from_sheet(conn, api: SheetsApi, sid: str, now: datetime) -> dict:
    """Copy my Status / Notes / Outcome into SQLite, matching rows by ID."""
    out = {"applied": 0, "discarded": 0, "outcomes": 0, "unknown": []}
    rows = api.read(sid, APPS)
    for n, row in enumerate(rows[1:], 2):
        row = row + [""] * (len(HEADERS[APPS]) - len(row))
        job_key, status, notes, outcome = (row[COL["ID"]], to_key(row[COL["Status"]]),
                                           row[COL["Notes"]], to_key(row[COL["Outcome"]]))
        app = conn.execute("SELECT * FROM applications WHERE job_key=? AND outreach_type='application_brief'",
                           (job_key,)).fetchone()
        if not app:
            continue
        if notes != (app["notes"] or ""):
            conn.execute("UPDATE applications SET notes=? WHERE id=?", (notes, app["id"]))
        if status and status not in STATUS:
            out["unknown"].append(f"{job_key}: Status '{row[COL['Status']]}'")
        elif status in ("applied", "discarded") and app["status"] == "pending":
            repo.transition_application(conn, app["id"], status, now, source="sheet")
            out[status] += 1
        if outcome:
            if outcome not in OUTCOME:
                out["unknown"].append(f"{job_key}: Outcome '{row[COL['Outcome']]}'")
                continue
            app = conn.execute("SELECT * FROM applications WHERE id=?", (app["id"],)).fetchone()
            if app["outcome"] != outcome:
                if outcome in repo.PORTAL_TRANSITIONS.get(app["status"], set()):
                    repo.transition_application(conn, app["id"], outcome, now, source="sheet", outcome=outcome)
                else:
                    conn.execute("UPDATE applications SET outcome=? WHERE id=?", (outcome, app["id"]))
                out["outcomes"] += 1
    return out


NET_COL = {name: i for i, name in enumerate(HEADERS[NETWORK])}
NETWORK_PATH = {"requested": ["requested"], "connected": ["requested", "connected"], "skipped": ["skipped"],
                "closed": ["closed"]}


def sync_network(conn, api: SheetsApi, sid: str, now: datetime) -> dict:
    """Copy Network Status / Notes into SQLite. requested + note marks the company contacted."""
    out = {"requested": 0, "connected": 0, "skipped": 0, "unknown": []}
    for row in api.read(sid, NETWORK)[1:]:
        row = row + [""] * (len(HEADERS[NETWORK]) - len(row))
        nid, status, notes = row[0], to_key(row[NET_COL["Status"]]), row[NET_COL["Notes"]]
        current = conn.execute("SELECT * FROM network WHERE id=?", (nid,)).fetchone()
        if not current:
            continue
        if notes != (current["notes"] or ""):
            conn.execute("UPDATE network SET notes=? WHERE id=?", (notes, nid))
        if not status or status == current["status"]:
            continue
        if status not in NETWORK_STATUS:
            out["unknown"].append(f"{nid}: Status '{row[NET_COL['Status']]}'")
            continue
        steps = NETWORK_PATH.get(status, [])
        if status == "closed" and current["status"] == "to_connect":
            steps = ["skipped"]       # never sent: closing it is the same as skipping (no outreach recorded)
        for step in steps:
            now_status = conn.execute("SELECT status FROM network WHERE id=?", (nid,)).fetchone()[0]
            if step in repo.NETWORK_TRANSITIONS.get(now_status, set()):
                repo.transition_network(conn, nid, step, now, source="sheet")
        out[status] = out.get(status, 0) + 1
    return out


def expire_pending(conn, api: SheetsApi | None, sid: str | None, now: datetime, days: int) -> int:
    """Pending kits older than `days`, or whose job closed -> expired (only the Status cell)."""
    cutoff = repo.iso(now - timedelta(days=days))
    stale = conn.execute(
        "SELECT a.* FROM applications a JOIN jobs j ON j.job_key = a.job_key "
        "WHERE a.outreach_type='application_brief' AND a.status='pending' "
        "AND (a.created_at < ? OR j.lifecycle='closed')", (cutoff,)).fetchall()
    if not stale:
        return 0
    rows = api.read(sid, APPS) if api and sid else []
    where = {r[0]: n for n, r in enumerate(rows, 1) if r}
    status_col = chr(ord("A") + COL["Status"])
    for app in stale:
        repo.transition_application(conn, app["id"], "expired", now)
        if app["job_key"] in where:
            api.update_cell(sid, f"{APPS}!{status_col}{where[app['job_key']]}", label("expired"))
    return len(stale)


def detect_confirmations(conn, inbox: list[dict], patterns: list[str], now: datetime) -> int:
    """ATS 'Thank you for applying to <company>' email for an open kit -> applied."""
    marked = 0
    pending = conn.execute(
        "SELECT a.*, j.company, j.company_domain FROM applications a JOIN jobs j ON j.job_key=a.job_key "
        "WHERE a.outreach_type='application_brief' AND a.status='pending'").fetchall()
    for app in pending:
        created_ms = repo.parse_iso(app["created_at"]).timestamp() * 1000
        name = app["company_norm"]
        for msg in inbox:
            text = f"{msg['subject']} {msg['from']}".lower()
            company_hit = (name and re.search(rf"\b{re.escape(name)}\b", text)) or (
                app["company_domain"] and app["company_domain"] in text)
            if msg["internal_ms"] >= created_ms and company_hit and any(
                    re.search(p, msg["subject"], re.I) for p in patterns):
                repo.transition_application(conn, app["id"], "applied", now, source="gmail")
                marked += 1
                break
    return marked


def rebuild_rows(conn, profile_links: dict) -> list[tuple[str, str, list]]:
    """Every portal application and kit from SQLite, for recreating a deleted sheet."""
    out = []
    for app in conn.execute("SELECT a.*, j.company, j.title, j.apply_url FROM applications a "
                            "JOIN jobs j ON j.job_key=a.job_key WHERE a.outreach_type='application_brief' "
                            "ORDER BY a.created_at").fetchall():
        app = dict(app)
        draft = json.loads(app["draft_json"] or "{}")
        ev = conn.execute("SELECT fit_score, reason FROM evaluations WHERE job_key=? ORDER BY evaluated_at DESC "
                          "LIMIT 1", (app["job_key"],)).fetchone()
        if draft.get("kit"):
            out.append((KITS, app["job_key"], kit_row(app, draft["kit"], draft.get("standard_fields", ""))))
        row = application_row(app, app, ev["fit_score"] if ev else None, ev["reason"] if ev else "",
                              profile_links.get(app["resume_key"]), "__KIT__" if draft.get("kit") else "")
        row[COL["Notes"]], row[COL["Outcome"]] = app["notes"] or "", label(app["outcome"])
        out.append((APPS, app["job_key"], row))
    return out



# ---------------------------------------------------------------- auto-archive (keep the main tabs tidy)

def _cell(row: list, i: int) -> str:
    return row[i] if i < len(row) else ""


def archive_rows(conn, api: SheetsApi, sid: str, now: datetime) -> dict:
    """Move finished rows to the Archive tab (runs after the sheet sync, so your edits are in SQLite):
    Applications with status discarded/expired or Outcome rejected (their Kits rows are removed too),
    Network rows skipped/closed. In-progress rows are never moved. SQLite keeps the full history."""
    api.ensure_tab(sid, ARCHIVE)
    stamp = now.strftime("%Y-%m-%d")
    archive, moved = [], {APPS: [], KITS: [], NETWORK: []}
    apps = api.read(sid, APPS, formulas=True)
    done_apps = set()
    for n, row in enumerate(apps[1:], start=2):
        key = _cell(row, COL["ID"])
        app = conn.execute("SELECT status, outcome FROM applications WHERE job_key=? AND "
                           "outreach_type='application_brief'", (key,)).fetchone()
        if app and (app["status"] in ARCHIVE_APPS or app["outcome"] == "rejected"):
            archive.append([stamp, APPS, key, _cell(row, COL["Company"]), _cell(row, COL["Role"]), label(app["status"]),
                            label(app["outcome"]), _cell(row, COL["Notes"]), _cell(row, COL["Apply link"])])
            moved[APPS].append(n)
            done_apps.add(key)
    if done_apps:
        for n, row in enumerate(api.read(sid, KITS)[1:], start=2):
            if row and row[0] in done_apps:
                moved[KITS].append(n)
    for n, row in enumerate(api.read(sid, NETWORK, formulas=True)[1:], start=2):
        nid = _cell(row, 0)
        net = conn.execute("SELECT status FROM network WHERE id=?", (nid,)).fetchone()
        if net and net["status"] in ARCHIVE_NETWORK:
            person = f"{_cell(row, NET_COL['Person'])} ({_cell(row, NET_COL['Title'])})"
            archive.append([stamp, NETWORK, nid, _cell(row, NET_COL["Company"]), person, label(net["status"]), "",
                            _cell(row, NET_COL["Notes"]), _cell(row, NET_COL["LinkedIn"])])
            moved[NETWORK].append(n)
    if archive:   # copy first, delete after: a failure in between can only leave a duplicate, never lose a row
        api.append(sid, ARCHIVE, archive)
        for tab, rows in moved.items():
            api.delete_rows(sid, tab, rows)
    return {"archived": len(archive)}
