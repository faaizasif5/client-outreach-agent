-- 002: Phase 2 (drafting, Gmail, Sheets). All additions beyond spec section 15.
ALTER TABLE applications ADD COLUMN draft_json TEXT;     -- exactly what Claude wrote and code accepted (audit)
ALTER TABLE applications ADD COLUMN last_error TEXT;
ALTER TABLE applications ADD COLUMN notes TEXT;          -- copied from my Notes cell in the tracker sheet
ALTER TABLE runs ADD COLUMN digest_draft_id TEXT;      -- Gmail digest draft for that run (replaced on rerun)

-- Dry-run outputs, kept apart from applications so dry runs never block a company.
CREATE TABLE dry_run_drafts (
  job_key TEXT PRIMARY KEY REFERENCES jobs(job_key),
  run_id TEXT NOT NULL,
  outreach_type TEXT NOT NULL,
  payload TEXT NOT NULL,                 -- JSON
  file_path TEXT,
  created_at TEXT NOT NULL
);

-- Rows waiting to be written to the Google Sheet (written next run if the API fails).
CREATE TABLE sheet_queue (
  id INTEGER PRIMARY KEY,
  tab TEXT NOT NULL,                     -- Applications | Kits | Network
  job_key TEXT NOT NULL,
  row TEXT NOT NULL,                     -- JSON list of cell values
  created_at TEXT NOT NULL,
  written_at TEXT,
  UNIQUE (tab, job_key)
);

-- Every status change (spec section 11: "SQLite also keeps every status change").
CREATE TABLE status_history (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                    -- application | network
  ref TEXT NOT NULL,                     -- applications.id or network.id
  old_status TEXT,
  new_status TEXT NOT NULL,
  source TEXT,                           -- tool | sheet | gmail
  changed_at TEXT NOT NULL
);

-- Resume PDFs uploaded to Drive ("Job Tracker Resumes"), so the sheet can link them.
CREATE TABLE drive_files (
  resume_key TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  link TEXT,
  uploaded_at TEXT NOT NULL
);
