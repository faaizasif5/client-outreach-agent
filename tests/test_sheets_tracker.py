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

    def read(self, sid, tab):
        return [list(r) for r in self.tabs[tab]]

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
    assert api.row(st.APPS, "p1")[st.COL["Kit"]] == '=HYPERLINK("#gid=1&range=A2","Kit")'
    assert api.row(st.APPS, "p1")[st.COL["Status"]] == "pending"
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
    assert api.row(st.APPS, "old")[st.COL["Status"]] == "expired"
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
