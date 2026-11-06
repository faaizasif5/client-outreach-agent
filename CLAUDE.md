# Client Outreach Agent: notes for Claude Code

Personal tool. `docs/SPEC.md` is the source of truth (section 0 lists every change from the
original plan); if something here or in your own ideas conflicts with it, follow the spec and flag
the conflict. Keep the spec, this file and README.md in line when behaviour changes. This repo is
PUBLIC: no real names, employers, client/project names, metrics, contact details or resume file
names in any tracked file (spec, prompts, config, tests). Use the fake data from
`candidate/profile.example.yaml`; personal values go in `candidate/profile.yaml` (e.g.
`resume_filename`). Never commit `.env`, `credentials.json`, tokens,
`data/`, `candidate/profile.yaml`, `candidate/resumes/`, `candidate/latex/`, `candidate/bullets.yaml`,
`snapshots/`, `applications/`, `logs/`, `reports/`, `tools/`.

## Current state
All three phases are built. **Live mode is on** (`outreach.live_enabled: true`, approved after a
supervised first live run): Gmail drafts, the tracker sheet and ntfy are real. Phase 1 calibration
labeling continues. The Windows scheduled task `ClientOutreachAgent` runs `main.py` live (registered
with `scripts/install_task.ps1 -Live`, approved 2026-10-25). `main.py scheduler off | on | status`
disables/enables that task via `schtasks` (tests mock `schedule._schtasks`).

Decisions that changed the original spec (details in SPEC section 0):
- Scoring: `sonnet` medium effort. Stretch jobs scored on skills/seniority only.
- Stack matching (`pipeline/stack.py`): drafts lean on the listing's stack the user has, never claim
  a technology outside the profile (checked in code), LinkedIn pitches mention agentic coding with
  Claude Code.
- LinkedIn: requests go out WITHOUT a note; one message after they accept; no numbers/metrics in
  messages; `network.is_pitch` keeps the one-company rule.
- Cold emails: open on a hook copied from the listing, plain facts, honest location line, never
  cost/savings/payroll talk (checked in code). The price A/B test is retired.
- Logging: `logs/<date>.log` (run start, one line per step, errors, end; pruned after 30 days).
  Crashes are recorded; expired Google/Claude logins push an alert right away.
- Resume attachment name comes from `resume_filename` in the gitignored profile, not `config.yaml`.
- YC: three pages (`config/yc_selectors.yaml`), one read with the user's own saved session
  (`main.py yc-login`); never store or type a password; never send logged-in pages to Claude.
- Never pay to apply: gated job-board apply links go to manual review.
- Digest: short HTML email to the user's own address only (`GmailClient.send` refuses anyone else).
- Sheets: friendly labels, styled layout re-applied each run, auto-archive to an `Archive` tab.

## Hard rules (spec section 2)
- Based in Karachi: nothing proceeds unless the deterministic eligibility gate allows it.
  AI scores never override a hard ineligible result.
- No auto-sending of outreach (drafts only), no auto-applying, no portal submissions, no clicking Apply.
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

## Claude CLI flags (verified with `claude --help`, v2.1.318)
`claude -p --model <m> --effort <e> --output-format json --tools "" --no-session-persistence
--strict-mcp-config --setting-sources project --system-prompt <short>`, prompt via stdin, cwd =
empty sandbox dir. Do NOT use `--bare` (it ignores the Pro OAuth login). Tests must never call the
real CLI: `tests/conftest.py` replaces `pipeline.claude_cli._run` for every test.

## Running
```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m pytest              # all tests (fixtures only, no network)
.venv\Scripts\python main.py                # live run (guarded once per Karachi day; --force to rerun)
.venv\Scripts\python main.py --dry-run      # everything local: reports/<date>.md + reports/dry-run/
.venv\Scripts\python main.py label          # read labels from reports/*.md, print calibration
.venv\Scripts\python main.py setup-google   # one-time OAuth, tracker sheet, resumes to Drive
.venv\Scripts\python main.py yc-login       # log in to Work at a Startup yourself; session is saved
.venv\Scripts\python main.py scheduler off  # pause the daily task (on | off | status)
```
Full command list: `docs/COMMANDS.md` (keep it in line when commands change).
Profile: `candidate/profile.yaml` (from `profile.example.yaml`). Resume PDFs in `candidate/resumes/`,
`.tex` sources in `candidate/latex/` (templates and `candidate/bullets.yaml` via
`resume_builder.build_templates`).

## Conventions
- Only the libraries in spec section 4 (+ Tectonic for LaTeX). No frameworks, no vector DBs.
- Tests use saved fixtures in `tests/fixtures/`, never live sites.
- Be polite to sources: 2-3 s between requests per host, on-disk cache in `data/cache/`.
- Work on the `dev` branch. Do not commit yourself: at the end of each task, give the user a commit message to run.
- Commit messages never include a `Co-Authored-By: Claude` (or any AI) trailer.
