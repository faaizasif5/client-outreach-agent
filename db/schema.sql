-- Current full schema for reference. db/migrations/ is authoritative; keep this in sync.
-- 001: initial schema (spec section 15) plus Phase 1 additions, marked "-- added".
-- company_norm / company_domain together form the company identity (domain first, name fallback).

CREATE TABLE jobs (
  job_key TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  company_norm TEXT NOT NULL,
  company_domain TEXT,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,                    -- added
  canonical_url TEXT UNIQUE,
  ats_key TEXT,                                -- added: ats:<provider>:<id>
  sources TEXT NOT NULL,                       -- JSON list of {source, external_id, url}
  location_text TEXT,
  description TEXT,
  contact_email TEXT,
  apply_url TEXT,                              -- added
  founder_links TEXT NOT NULL DEFAULT '[]',    -- added: JSON list of linkedin.com/in links found in the listing (never fetched)
  team_size INTEGER,                           -- added: parsed from listing text when stated
  posted_at TEXT,
  posted_at_source TEXT,                       -- added: source | relative_text | ats | newest_first
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  lifecycle TEXT NOT NULL DEFAULT 'open',      -- open | closed
  eligibility TEXT,                            -- eligible | stretch | uncertain | ineligible
  eligibility_reason TEXT,
  bucket TEXT,                                 -- added: eligible | remote_us_stretch | us_auth_stretch | explicit_us_only
  route TEXT,                                  -- added: email | portal | network | skipped
  status TEXT NOT NULL DEFAULT 'new',          -- added: new | keyword_filtered | scored | score_missing | suppressed_company
  flags TEXT NOT NULL DEFAULT '[]'             -- added: JSON list, e.g. possible_repost:<key>, travel_occasional, date_estimated
);
CREATE INDEX idx_jobs_ats ON jobs(ats_key);
CREATE INDEX idx_jobs_company ON jobs(company_norm);
CREATE INDEX idx_jobs_domain ON jobs(company_domain);

CREATE TABLE evaluations (
  id INTEGER PRIMARY KEY,
  job_key TEXT NOT NULL REFERENCES jobs(job_key),
  run_id TEXT NOT NULL,
  resume_key TEXT NOT NULL,
  fit_score INTEGER NOT NULL,
  evidence TEXT NOT NULL,            -- JSON
  reason TEXT,
  confidence TEXT NOT NULL,
  skip INTEGER NOT NULL,
  skip_reasons TEXT,                 -- JSON
  evaluated_at TEXT NOT NULL,
  model_resume_key TEXT,             -- added: key Claude chose, before the PDF-exists fallback
  UNIQUE (job_key, run_id)
);

CREATE TABLE applications (
  id INTEGER PRIMARY KEY,
  job_key TEXT NOT NULL UNIQUE REFERENCES jobs(job_key),
  company_norm TEXT NOT NULL,
  company_domain TEXT,               -- added
  outreach_type TEXT NOT NULL,       -- direct_email | application_brief
  bucket TEXT NOT NULL,              -- eligible | remote_us_stretch | us_auth_stretch | explicit_us_only
  pitch_variant TEXT,                -- a | b (stretch price-mention test)
  resume_pdf_path TEXT,
  resume_key TEXT NOT NULL,
  status TEXT NOT NULL,
  draft_id TEXT,
  thread_id TEXT,
  recipient TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  outcome TEXT,                      -- replied | interview | rejected | offer | no_response
  updated_at TEXT NOT NULL
);

CREATE TABLE people (
  id INTEGER PRIMARY KEY,
  company_norm TEXT NOT NULL,
  name TEXT NOT NULL,
  title TEXT,
  linkedin_url TEXT,
  source TEXT NOT NULL,              -- yc_page | team_page | search
  verified INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE (company_norm, name)
);

CREATE TABLE network (
  id TEXT PRIMARY KEY,               -- <job_key>-<n>
  job_key TEXT NOT NULL REFERENCES jobs(job_key),
  person_id INTEGER NOT NULL REFERENCES people(id),
  connection_note TEXT,
  after_accept_message TEXT,
  status TEXT NOT NULL,              -- to_connect | requested | connected | skipped
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE companies_contacted (
  company_norm TEXT PRIMARY KEY,
  company_domain TEXT,               -- added
  first_contacted_at TEXT NOT NULL
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  status TEXT,                       -- added: running | success | failed
  counts TEXT,                       -- JSON: fetched, eligible, scored, drafted...
  usage TEXT,                        -- JSON from claude envelope
  errors TEXT
);

-- added: calibration labels read back from the markdown report (spec section 20, Phase 1)
CREATE TABLE labels (
  job_key TEXT PRIMARY KEY REFERENCES jobs(job_key),
  label TEXT NOT NULL,               -- would_apply | would_not
  fit_score INTEGER,
  resume_key TEXT,
  report_file TEXT,
  labeled_at TEXT NOT NULL
);

CREATE TABLE schema_version (version INTEGER NOT NULL);

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

-- 003: Phase 3 (Network tab, YC scraper, tailored resumes). Additions beyond spec section 15.
ALTER TABLE network ADD COLUMN notes TEXT;              -- copied from my Notes cell in the Network tab

-- A company's YC page / team page is fetched once, ever (spec 7a / Network tab).
CREATE TABLE company_pages (
  company_norm TEXT NOT NULL,
  source TEXT NOT NULL,                  -- yc_page | team_page
  url TEXT,
  fetched_at TEXT NOT NULL,
  people_found INTEGER NOT NULL,
  PRIMARY KEY (company_norm, source)
);

-- YC scraper health history and repair attempts (spec 7a).
CREATE TABLE yc_health (
  id INTEGER PRIMARY KEY,
  checked_at TEXT NOT NULL,
  status TEXT NOT NULL,                  -- healthy | broken | warning
  listing_count INTEGER,
  new_count INTEGER,
  details TEXT                           -- JSON: per-check results
);
CREATE TABLE yc_repairs (
  id INTEGER PRIMARY KEY,
  attempted_at TEXT NOT NULL,
  kind TEXT NOT NULL,                    -- auto | manual_needed | disabled
  accepted INTEGER NOT NULL,
  details TEXT
);

-- Tailored resumes (spec 11a): exactly what each company saw.
CREATE TABLE tailored_resumes (
  job_key TEXT PRIMARY KEY REFERENCES jobs(job_key),
  resume_key TEXT NOT NULL,
  spec_json TEXT NOT NULL,               -- Claude's {summary, bullet_ids, skills_first}
  pdf_path TEXT,
  status TEXT NOT NULL,                  -- used | rejected
  problems TEXT,                         -- JSON list when rejected
  created_at TEXT NOT NULL
);

-- 004: the YC scraper reads more than one public results page (ycombinator.com board + Work at a Startup).
ALTER TABLE yc_health ADD COLUMN page TEXT;
ALTER TABLE yc_repairs ADD COLUMN page TEXT;
