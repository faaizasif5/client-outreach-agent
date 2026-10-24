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
HEADERS = {
    APPS: ["ID", "Added", "Company", "Role", "Apply link", "Resume", "Fit", "Why", "Kit", "Status", "Notes", "Outcome"],
    KITS: ["ID", "Company", "Apply link", "Cover note", "Form answers", "Standard fields"],
    NETWORK: ["ID", "Company", "Linked job", "Person", "Title", "LinkedIn", "Source", "Verified",
              "Connection note", "After-accept message", "Status", "Notes"],
}
STATUS = ["pending", "applied", "discarded", "expired"]
OUTCOME = ["replied", "interview", "rejected", "offer"]
NETWORK_STATUS = ["to_connect", "requested", "connected", "skipped"]
COL = {name: i for i, name in enumerate(HEADERS[APPS])}
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
        return sid, gids

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

    def read(self, sid: str, tab: str) -> list[list[str]]:
        res = self.execute(self.sheets.values().get(spreadsheetId=sid, range=f"{tab}!A:Z"))
        return res.get("values", [])

    def append(self, sid: str, tab: str, rows: list[list], raw: bool = False) -> str:
        res = self.execute(self.sheets.values().append(
            spreadsheetId=sid, range=f"{tab}!A1", valueInputOption="RAW" if raw else "USER_ENTERED",
            insertDataOption="INSERT_ROWS", body={"values": rows}))
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


def _format_requests(gids: dict) -> list[dict]:
    def validation(gid, col, values):
        return {"setDataValidation": {
            "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                     "showCustomUi": True, "strict": False}}}

    def color_rule(gid, status, rgb):
        col = chr(ord("A") + COL["Status"])
        return {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS[APPS])}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=${col}2="{status}"'}]},
                            "format": {"backgroundColor": dict(zip(("red", "green", "blue"), rgb))}}}}}

    a, k, n = gids[APPS], gids[KITS], gids[NETWORK]
    return [
        validation(a, COL["Status"], STATUS), validation(a, COL["Outcome"], OUTCOME),
        validation(n, HEADERS[NETWORK].index("Status"), NETWORK_STATUS),
        color_rule(a, "expired", (0.85, 0.85, 0.85)), color_rule(a, "applied", (0.86, 0.94, 0.86)),
        {"repeatCell": {"range": {"sheetId": k}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                                                                "verticalAlignment": "TOP"}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
    ]


# ---------------------------------------------------------------- rows

def application_row(job: dict, app: dict, fit: int | None, why: str, resume_link: str | None, kit_link: str) -> list:
    return [app["job_key"], app["created_at"][:10], safe(job["company"]), safe(job["title"]),
            hyperlink(job.get("apply_url"), "Apply"), hyperlink(resume_link, app["resume_key"]),
            fit if fit is not None else "", safe(why), kit_link, app["status"], "", ""]


def kit_row(job: dict, kit: dict, standard: str) -> list:
    answers = "\n\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in kit.get("form_answers", []))
    return [job["job_key"], job["company"], job.get("apply_url") or "", kit["cover_note"], answers, standard]


def network_row(row: dict, job: dict, linked: str) -> list:
    return [row["id"], safe(job["company"]), linked, safe(row["name"]), safe(row.get("title")),
            hyperlink(row.get("linkedin_url"), "search" if row.get("source") == "search" else "profile"),
            row.get("source"), "yes" if row.get("verified") else "check", safe(row.get("connection_note") or ""),
            safe(row.get("after_accept_message")), row["status"], ""]


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
                row[COL["Kit"]] = (f'=HYPERLINK("#gid={gids[KITS]}&range=A{kit_row_no}","Kit")'
                                   if kit_row_no else "")
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
        job_key, status, notes, outcome = (row[COL["ID"]], row[COL["Status"]].strip().lower(),
                                           row[COL["Notes"]], row[COL["Outcome"]].strip().lower())
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
NETWORK_PATH = {"requested": ["requested"], "connected": ["requested", "connected"], "skipped": ["skipped"]}


def sync_network(conn, api: SheetsApi, sid: str, now: datetime) -> dict:
    """Copy Network Status / Notes into SQLite. requested + note marks the company contacted."""
    out = {"requested": 0, "connected": 0, "skipped": 0, "unknown": []}
    for row in api.read(sid, NETWORK)[1:]:
        row = row + [""] * (len(HEADERS[NETWORK]) - len(row))
        nid, status, notes = row[0], row[NET_COL["Status"]].strip().lower(), row[NET_COL["Notes"]]
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
        for step in NETWORK_PATH.get(status, []):
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
            api.update_cell(sid, f"{APPS}!{status_col}{where[app['job_key']]}", "expired")
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
        row[COL["Notes"]], row[COL["Outcome"]] = app["notes"] or "", app["outcome"] or ""
        out.append((APPS, app["job_key"], row))
    return out
