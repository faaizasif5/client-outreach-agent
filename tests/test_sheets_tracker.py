"""Sheets tracker logic (spec section 11) against an in-memory fake sheet."""
from datetime import timedelta

import pytest

from db import repo
from outputs import sheets_tracker as st
from tests.conftest import NOW, make_job


class FakeSheetsApi:
    def __init__(self):
        self.tabs = {t: [list(h)] for t, h in st.HEADERS.items()}
        self.down = False
        self.updates = []

    def gids(self, sid):
        if self.down:
            raise ConnectionError("Sheets API down")
        return {st.APPS: 0, st.KITS: 1, st.NETWORK: 2}

    def read(self, sid, tab, formulas=False):
        return [list(r) for r in self.tabs[tab]]

    def ensure_tab(self, sid, tab):
        self.tabs.setdefault(tab, [list(st.HEADERS[tab])])

    def delete_rows(self, sid, tab, rows):
        for n in sorted(set(rows), reverse=True):
            del self.tabs[tab][n - 1]

    def append(self, sid, tab, rows, raw=False):
        if self.down:
            raise ConnectionError("Sheets API down")
        self.tabs[tab] += [list(r) for r in rows]
        n = len(self.tabs[tab])
        return f"{tab}!A{n}:L{n}"

    def update_cell(self, sid, a1, value):
        tab, cell = a1.split("!")
        col, row = ord(cell[0]) - ord("A"), int(cell[1:])
        self.tabs[tab][row - 1][col] = value
        self.updates.append(a1)

    # helpers
    def row(self, tab, job_key):
        return next(r for r in self.tabs[tab] if r[0] == job_key)


def portal(conn, key="p1", company="acme", created=NOW):
    make_job(conn, job_key=key, company=company.title(), company_norm=company, apply_url=f"https://{company}.com/jobs/1")
    app_id = repo.create_application(conn, job_key=key, outreach_type="application_brief", bucket="eligible",
                                     resume_key="fullstack", now=created)
    app = dict(conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
    job = dict(repo.get_job(conn, key))
    kit = {"cover_note": "Hello", "form_answers": [{"question": "Why us?", "answer": "Because."}]}
    st.enqueue(conn, st.KITS, key, st.kit_row(job, kit, "Email: a@b.c"), NOW)
    st.enqueue(conn, st.APPS, key, st.application_row(job, app, 85, "Backend fit", None, "__KIT__"), NOW)
    return app_id


def status(conn, key="p1"):
    return conn.execute("SELECT status FROM applications WHERE job_key=?", (key,)).fetchone()[0]


def test_flush_appends_kit_then_linked_application(conn):
    api = FakeSheetsApi()
    portal(conn)
    out = st.flush_queue(conn, api, "sid", NOW)
    assert out == {"written": 2, "queued": 0, "error": None}
    assert api.row(st.KITS, "p1")[3] == "Hello"
    assert api.row(st.APPS, "p1")[st.COL["Kit"]].startswith('=HYPERLINK("#gid=1&range=A"&MATCH(')
    assert api.row(st.APPS, "p1")[st.COL["Status"]] == "Pending"
    assert st.flush_queue(conn, api, "sid", NOW)["written"] == 0     # never re-appended


def test_api_down_rows_stay_queued_then_written(conn):
    api = FakeSheetsApi()
    portal(conn)
    api.down = True
    out = st.flush_queue(conn, api, "sid", NOW)
    assert out["written"] == 0 and out["queued"] == 2 and "down" in out["error"]
    api.down = False
    assert st.flush_queue(conn, api, "sid", NOW + timedelta(days=1))["written"] == 2


def test_sync_matches_by_id_after_sorting(conn):
    api = FakeSheetsApi()
    portal(conn, "p1", "acme")
    portal(conn, "p2", "globex")
    st.flush_queue(conn, api, "sid", NOW)
    # I sort the sheet (rows swap) and edit Status / Notes / Outcome
    api.tabs[st.APPS][1], api.tabs[st.APPS][2] = api.tabs[st.APPS][2], api.tabs[st.APPS][1]
    api.row(st.APPS, "p1")[st.COL["Status"]] = "applied"
    api.row(st.APPS, "p1")[st.COL["Notes"]] = "applied via Ashby"
    api.row(st.APPS, "p2")[st.COL["Status"]] = "discarded"
    out = st.sync_from_sheet(conn, api, "sid", NOW)
    assert (out["applied"], out["discarded"]) == (1, 1)
    assert status(conn, "p1") == "applied" and status(conn, "p2") == "discarded"
    assert repo.is_company_contacted(conn, "acme", None)                 # applied = contacted
    assert repo.company_block_reason(conn, "globex", None) is None       # discarded frees it
    assert conn.execute("SELECT notes FROM applications WHERE job_key='p1'").fetchone()[0] == "applied via Ashby"


def test_my_edits_survive_a_run(conn):
    api = FakeSheetsApi()
    portal(conn, "p1")
    st.flush_queue(conn, api, "sid", NOW)
    api.row(st.APPS, "p1")[st.COL["Notes"]] = "my note"
    portal(conn, "p2", "globex")
    st.flush_queue(conn, api, "sid", NOW)                      # new rows only appended
    st.sync_from_sheet(conn, api, "sid", NOW)
    assert api.row(st.APPS, "p1")[st.COL["Notes"]] == "my note"
    assert len(api.tabs[st.APPS]) == 3


def test_unknown_status_ignored_and_reported(conn):
    api = FakeSheetsApi()
    portal(conn)
    st.flush_queue(conn, api, "sid", NOW)
    api.row(st.APPS, "p1")[st.COL["Status"]] = "maybe later"
    out = st.sync_from_sheet(conn, api, "sid", NOW)
    assert out["unknown"] == ["p1: Status 'maybe later'"] and status(conn) == "pending"


def test_outcome_copied(conn):
    api = FakeSheetsApi()
    portal(conn)
    st.flush_queue(conn, api, "sid", NOW)
    api.row(st.APPS, "p1")[st.COL["Status"]] = "applied"
    api.row(st.APPS, "p1")[st.COL["Outcome"]] = "interview"
    st.sync_from_sheet(conn, api, "sid", NOW)
    row = conn.execute("SELECT status, outcome FROM applications WHERE job_key='p1'").fetchone()
    assert tuple(row) == ("interview", "interview")


def test_expire_pending_after_7_days_only_status_cell(conn):
    api = FakeSheetsApi()
    portal(conn, "old", "acme", created=NOW - timedelta(days=8))
    portal(conn, "new", "globex", created=NOW - timedelta(days=1))
    st.flush_queue(conn, api, "sid", NOW)
    assert st.expire_pending(conn, api, "sid", NOW, 7) == 1
    assert status(conn, "old") == "expired" and status(conn, "new") == "pending"
    assert api.row(st.APPS, "old")[st.COL["Status"]] == "Expired"
    assert api.updates == [f"{st.APPS}!J2"]
    assert repo.company_block_reason(conn, "acme", None) is None


def test_closed_job_expires_pending_kit(conn):
    api = FakeSheetsApi()
    portal(conn)
    st.flush_queue(conn, api, "sid", NOW)
    conn.execute("UPDATE jobs SET lifecycle='closed' WHERE job_key='p1'")
    assert st.expire_pending(conn, api, "sid", NOW, 7) == 1


def test_ats_confirmation_marks_applied(conn, cfg):
    portal(conn, "p1", "acme")
    later = int((NOW + timedelta(hours=2)).timestamp() * 1000)
    inbox = [{"from": "no-reply@ashbyhq.com", "subject": "Thank you for applying to Acme!", "internal_ms": later},
             {"from": "x@y.z", "subject": "Thanks for applying to Globex", "internal_ms": later}]
    assert st.detect_confirmations(conn, inbox, cfg["outreach"]["ats_confirmation"], NOW) == 1
    assert status(conn) == "applied" and repo.is_company_contacted(conn, "acme", None)


def test_confirmation_before_kit_creation_ignored(conn, cfg):
    portal(conn, "p1", "acme")
    earlier = int((NOW - timedelta(days=3)).timestamp() * 1000)
    inbox = [{"from": "x", "subject": "Thank you for applying to Acme", "internal_ms": earlier}]
    assert st.detect_confirmations(conn, inbox, cfg["outreach"]["ats_confirmation"], NOW) == 0


@pytest.mark.parametrize("value,expected", [
    ("=IMPORTXML(\"http://evil\")", "'=IMPORTXML(\"http://evil\")"), ("+1 555", "'+1 555"), ("Acme", "Acme"),
])
def test_formula_injection_neutralized(value, expected):
    assert st.safe(value) == expected


def test_rebuild_from_sqlite(conn):
    portal(conn)
    import json
    conn.execute("UPDATE applications SET draft_json=?, notes='n1' WHERE job_key='p1'",
                 (json.dumps({"kit": {"cover_note": "Hi", "form_answers": []}}),))
    rows = st.rebuild_rows(conn, {})
    assert [t for t, _, _ in rows] == [st.KITS, st.APPS]
    assert rows[1][2][st.COL["Notes"]] == "n1"


def test_layout_freezes_one_row_and_sizes_every_column():
    reqs = st.layout_requests({st.APPS: 0, st.KITS: 1, st.NETWORK: 2, st.ARCHIVE: 3})
    freezes = [r["updateSheetProperties"]["properties"]["gridProperties"]["frozenRowCount"]
               for r in reqs if "updateSheetProperties" in r]
    assert freezes == [1, 1, 1, 1]
    for tab, gid in ((st.APPS, 0), (st.KITS, 1), (st.NETWORK, 2)):
        widths = [r for r in reqs if "updateDimensionProperties" in r
                  and r["updateDimensionProperties"]["range"]["sheetId"] == gid
                  and r["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"]
        assert len(widths) == len(st.HEADERS[tab]) == len(st.LAYOUT[tab])
    wraps = {r["repeatCell"]["range"]["startColumnIndex"]: r["repeatCell"]["cell"]["userEnteredFormat"]["wrapStrategy"]
             for r in reqs if "repeatCell" in r and r["repeatCell"]["range"]["sheetId"] == 0
             and r["repeatCell"]["range"].get("startRowIndex") == 1}
    assert wraps[st.COL["Why"]] == "WRAP" and wraps[st.COL["Company"]] == "CLIP"


def test_append_never_inserts_rows_into_the_frozen_header():
    calls = {}

    class Values:
        def append(self, **kw):
            calls.update(kw)
            return self

        def execute(self):
            return {"updates": {"updatedRange": "Kits!A2:F2"}}

    class Sheets:
        def spreadsheets(self):
            return self

        def values(self):
            return Values()
    api = st.SheetsApi(Sheets(), None, lambda req: req.execute())
    api.append("sid", st.KITS, [["a"]], raw=True)
    assert calls["insertDataOption"] == "OVERWRITE"


def test_row_height_follows_wrapped_text():
    short = st.row_height(["id", "Acme", "x", "Short note"], st.LAYOUT[st.KITS])
    long = st.row_height(["id", "Acme", "x", "word " * 300, "a\nb\nc"], st.LAYOUT[st.KITS])
    huge = st.row_height(["id", "Acme", "x", "word " * 5000], st.LAYOUT[st.KITS])
    assert short == 25 and long > 200 and huge == st.MAX_ROW_PX
    clipped = st.row_height(["x" * 900], st.LAYOUT[st.APPS])        # ID column clips: no growth
    assert clipped == 25


def test_style_is_idempotent_and_colours_statuses():
    gids = {st.APPS: 0, st.KITS: 1, st.NETWORK: 2}
    fresh = st.style_requests(gids, {"sheets": [{"properties": {"sheetId": g}} for g in gids.values()]})
    assert sum(1 for r in fresh if "addBanding" in r) == 3
    assert not any("deleteConditionalFormatRule" in r for r in fresh)
    status_rules = [r for r in fresh if "addConditionalFormatRule" in r
                    and r["addConditionalFormatRule"]["rule"].get("booleanRule", {}).get("condition", {}).get("type") == "TEXT_EQ"
                    and r["addConditionalFormatRule"]["rule"]["ranges"][0]["sheetId"] == 0
                    and r["addConditionalFormatRule"]["rule"]["ranges"][0]["startColumnIndex"] == st.COL["Status"]]
    assert {r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            for r in status_rules} == {st.label(v) for v in st.STATUS}
    # Second run: existing banding kept, old rules removed before re-adding (no pile-up)
    meta = {"sheets": [{"properties": {"sheetId": g}, "bandedRanges": [{"bandedRangeId": 1}],
                        "conditionalFormats": [{}] * 5} for g in gids.values()]}
    again = st.style_requests(gids, meta)
    assert not any("addBanding" in r for r in again)
    assert sum(1 for r in again if "deleteConditionalFormatRule" in r) == 15



# ---------------------------------------------------------------- auto-archive

def network_row(conn, api, key="n1", company="acme", status="to_connect", note="Hi"):
    from tests.conftest import make_job as mj
    mj(conn, job_key=key, company=company.title(), company_norm=company)
    conn.execute("INSERT OR IGNORE INTO people (company_norm, name, title, source, verified, fetched_at) "
                 "VALUES (?,?,?,?,?,?)", (company, "Sam", "Founder", "yc_page", 1, repo.iso(NOW)))
    pid = conn.execute("SELECT id FROM people WHERE company_norm=?", (company,)).fetchone()[0]
    nid = repo.create_network_entry(conn, job_key=key, person_id=pid, connection_note=note, now=NOW)
    row = dict(conn.execute("SELECT n.*, p.name, p.title, p.linkedin_url, p.source, p.verified FROM network n "
                            "JOIN people p ON p.id=n.person_id WHERE n.id=?", (nid,)).fetchone())
    api.tabs[st.NETWORK].append(st.network_row(row, {"company": company.title()}, "LinkedIn pitch"))
    if status != "to_connect":
        api.row(st.NETWORK, nid)[st.NET_COL["Status"]] = status
    return nid


def test_discarded_and_skipped_rows_move_to_archive(conn):
    api = FakeSheetsApi()
    portal(conn, "p1", "acme")
    portal(conn, "p2", "globex")
    st.flush_queue(conn, api, "sid", NOW)
    n_skip = network_row(conn, api, "n1", "initech", status="skipped")
    n_keep = network_row(conn, api, "n2", "hooli", status="to_connect")
    api.row(st.APPS, "p1")[st.COL["Status"]] = "discarded"
    api.row(st.APPS, "p1")[st.COL["Notes"]] = "not interested"
    st.sync_from_sheet(conn, api, "sid", NOW)
    st.sync_network(conn, api, "sid", NOW)
    out = st.archive_rows(conn, api, "sid", NOW)
    assert out == {"archived": 2}
    assert [r[0] for r in api.tabs[st.APPS][1:]] == ["p2"]                 # in-progress row stays
    assert [r[0] for r in api.tabs[st.KITS][1:]] == ["p2"]                 # its kit went with it
    assert [r[0] for r in api.tabs[st.NETWORK][1:]] == [n_keep]
    archived = {r[2]: r for r in api.tabs[st.ARCHIVE][1:]}
    assert archived["p1"][1] == st.APPS and archived["p1"][5] == "Not interested" and archived["p1"][7] == "not interested"
    assert archived[n_skip][1] == st.NETWORK and archived[n_skip][5] == "Skipped"
    assert st.archive_rows(conn, api, "sid", NOW) == {"archived": 0}         # nothing moved twice


def test_closed_linkedin_row_archived_and_company_stays_contacted(conn):
    api = FakeSheetsApi()
    nid = network_row(conn, api, "n1", "acme")
    api.row(st.NETWORK, nid)[st.NET_COL["Status"]] = "requested"
    st.sync_network(conn, api, "sid", NOW)
    api.row(st.NETWORK, nid)[st.NET_COL["Status"]] = "closed"
    st.sync_network(conn, api, "sid", NOW)
    assert conn.execute("SELECT status FROM network WHERE id=?", (nid,)).fetchone()[0] == "closed"
    assert repo.is_company_contacted(conn, "acme", None)                   # the outreach happened
    assert st.archive_rows(conn, api, "sid", NOW) == {"archived": 1}


def test_closing_an_unsent_request_is_a_skip(conn):
    api = FakeSheetsApi()
    nid = network_row(conn, api, "n1", "acme")
    api.row(st.NETWORK, nid)[st.NET_COL["Status"]] = "closed"
    st.sync_network(conn, api, "sid", NOW)
    assert conn.execute("SELECT status FROM network WHERE id=?", (nid,)).fetchone()[0] == "skipped"
    assert not repo.is_company_contacted(conn, "acme", None)
    assert repo.company_block_reason(conn, "acme", None) is None           # free for future outreach


def test_rejected_outcome_is_archived(conn):
    api = FakeSheetsApi()
    portal(conn, "p1", "acme")
    st.flush_queue(conn, api, "sid", NOW)
    api.row(st.APPS, "p1")[st.COL["Status"]] = "applied"
    api.row(st.APPS, "p1")[st.COL["Outcome"]] = "rejected"
    st.sync_from_sheet(conn, api, "sid", NOW)
    assert st.archive_rows(conn, api, "sid", NOW) == {"archived": 1}



@pytest.mark.parametrize("shown,key", [("To connect", "to_connect"), ("to_connect", "to_connect"),
                                       ("Not interested", "discarded"), ("discarded", "discarded"),
                                       (" Applied ", "applied"), ("Interview", "interview"), ("", "")])
def test_sheet_labels_map_back_to_status_keys(shown, key):
    assert st.to_key(shown) == key


def test_friendly_status_typed_in_sheet_syncs(conn):
    api = FakeSheetsApi()
    portal(conn)
    st.flush_queue(conn, api, "sid", NOW)
    api.row(st.APPS, "p1")[st.COL["Status"]] = "Not interested"
    assert st.sync_from_sheet(conn, api, "sid", NOW)["discarded"] == 1
