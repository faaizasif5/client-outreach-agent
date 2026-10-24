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
