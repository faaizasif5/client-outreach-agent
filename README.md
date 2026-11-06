# client-outreach-agent

A personal tool that finds fresh, remote-friendly software jobs, keeps only the ones I can really
apply to from Pakistan, and prepares the outreach for me to review and send myself.

Each morning it:
1. Reads HN "Who is hiring", RemoteOK, We Work Remotely and YC startup jobs.
2. Drops stale, onsite, region-locked and otherwise ineligible jobs (plain rules, no AI).
3. Scores the rest with Claude and picks the right resume.
4. Prepares Gmail drafts, portal application kits (Google Sheet) and LinkedIn messages.
5. Emails me a short digest and pings my phone.

Nothing is ever sent, submitted or applied automatically, and each company is contacted at most once.

## Run

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python main.py --dry-run   # local only: reports/<date>.md
.venv\Scripts\python main.py             # live: Gmail drafts + tracker sheet
.venv\Scripts\python -m pytest
```

One-time setup: `candidate/profile.yaml`, resume PDFs, `credentials.json`, then
`main.py setup-google` and `main.py yc-login`.

Daily schedule: `scripts\install_task.ps1 -Live` registers a Windows task that runs once a day on the
first logon/unlock/wake. Pause or resume it with `main.py scheduler off` / `on` (`status` to check).

## Docs

- `docs/COMMANDS.md`: every command (runs, scheduler on/off, setup, tests).
- `docs/SPEC.md`: full spec and the decisions made while building it (section 0).
- `CLAUDE.md`: rules and conventions for working on this repo with Claude Code.

Private repo: it contains my real profile.
