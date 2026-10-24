# Job Outreach Agent: notes for Claude Code

Personal tool. `docs/SPEC.md` is the source of truth; if something here or in your own ideas
conflicts with it, follow the spec and flag the conflict. This repo is PRIVATE (the spec
contains a real profile). Never commit `.env`, `credentials.json`, tokens, `data/`,
`candidate/profile.yaml`, `candidate/resumes/`, `candidate/latex/`, `snapshots/`,
`applications/`, `logs/`, `reports/`.

## Current phase
**Phase 1: Discovery + Matching** (spec section 20). In scope: HN + RemoteOK + WWR fetchers,
normalize + layered dedupe, company suppression logic, freshness check #1, eligibility gate,
contact extraction + routing, Claude CLI wrapper, scoring + Pydantic, local markdown report,
`python main.py label` calibration. Out of scope until approved: Gmail, Sheets, ntfy, YC
scraper/Playwright, LinkedIn/Network tab, drafting, tailored resumes. Do not install their
dependencies.

## Hard rules (spec section 2)
- Based in Karachi: nothing proceeds unless the deterministic eligibility gate allows it.
  AI scores never override a hard ineligible result.
- No auto-sending, no auto-applying, no portal submissions, no bot logins, no clicking Apply.
- Daily caps from `config.yaml` (default 6 email drafts / 2 portal kits / 3 LinkedIn requests;
  hard max 10 / 5).
- **One outreach per company, ever, across email, portal and LinkedIn.** Company identity is
  domain when known, else normalized name; a company is blocked if it matches on either.
  Checked in the same SQLite transaction that creates the outreach row. No follow-ups.
- Never claim skills, experience or metrics not in `candidate/profile.yaml`.
- Never answer work-authorization questions untruthfully; **never imply US work
  authorization or citizenship.**
- Claude via `claude -p` (Pro login), ~2 calls/day, tools disabled, one turn, empty cwd.
  Code decides recipients, routes, attachments and caps. Claude only returns text/JSON.
- **Never send any request to linkedin.com.** Only store links found elsewhere.

## Freshness
- Drop jobs older than `max_age_days` (14) by the *source's posted date*.
- `first_seen_at` is when the tool found a job. **It is never a posting date.** A 45-day-old
  job first seen today is stale.
- No date: relative text ("3 days ago") -> ATS API date -> newest-first feed evidence (after a
  baseline run). Otherwise `age_unknown` -> uncertain list, never drafted.

## Claude CLI flags (verified with `claude --help`, v2.1.302)
`claude -p --model <m> --output-format json --tools "" --no-session-persistence
--strict-mcp-config --system-prompt <short>`, prompt via stdin, cwd = empty sandbox dir.
Do NOT use `--bare` (it ignores the Pro OAuth login). Tests must never call the real CLI;
mock `pipeline.claude_cli.run_claude`.

## Running
```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest            # unit + integration tests (fixtures only, no network)
.venv\Scripts\python main.py --dry-run    # live sources -> reports/<date>.md
.venv\Scripts\python main.py label        # read labels from reports/*.md, print calibration
```
Copy `candidate/profile.example.yaml` to `candidate/profile.yaml` and fill it truthfully.
Resume PDFs go in `candidate/resumes/` (a key with no PDF falls back to `fullstack`).

## Conventions
- Only the libraries in spec section 4. No frameworks, no vector DBs.
- Tests use saved fixtures in `tests/fixtures/`, never live sites.
- Be polite to sources: 2-3 s between requests per host, on-disk cache in `data/cache/`.
- Work on the `dev` branch. Do not commit yourself: at the end of each task, give the user a commit message to run.
