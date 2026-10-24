# Commands

Run from the repo root in PowerShell or cmd.

## Daily runs

| Command | What it does |
|---|---|
| `.venv\Scripts\python main.py` | Live run: Gmail drafts, tracker sheet, digest email, phone push. Runs once per Karachi day. |
| `.venv\Scripts\python main.py --force` | Live run even if today already ran. |
| `.venv\Scripts\python main.py --dry-run` | Everything local only: `reports/<date>.md` and `reports/dry-run/`. No Google calls. |
| `.venv\Scripts\python main.py --sources hn,wwr` | Limit to some sources (`hn,remoteok,wwr,yc`). |
| `.venv\Scripts\python main.py --rescore` | Re-score already scored fresh jobs (uses a Claude call). |
| `.venv\Scripts\python main.py label` | Read your labels from `reports/*.md` and print calibration. |

## Daily scheduler (Windows task `ClientOutreachAgent`)

| Command | What it does |
|---|---|
| `.venv\Scripts\python main.py scheduler status` | Show the task's state (`Ready`, `Disabled`, `not installed`). |
| `.venv\Scripts\python main.py scheduler off` | Pause the daily task (stays registered). Manual runs still work. |
| `.venv\Scripts\python main.py scheduler on` | Resume it. |
| `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Live` | Register (or re-register) the task in live mode. |
| `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1` | Register it in dry-run mode. |
| `schtasks /Delete /TN ClientOutreachAgent /F` | Remove the task completely. |

The task fires on logon, unlock, wake and hourly; the once-per-day guard decides whether it runs.
Logs: `logs/<date>.log`.

## One-time setup

| Command | What it does |
|---|---|
| `py -3 -m venv .venv` | Create the virtual environment. |
| `.venv\Scripts\python -m pip install -r requirements.txt` | Install dependencies. |
| `.venv\Scripts\python -m playwright install chromium` | Browser for the YC pages. |
| `.venv\Scripts\python main.py setup-google` | Google OAuth, create the tracker sheet, upload resumes to Drive. |
| `.venv\Scripts\python main.py yc-login` | Log in to Work at a Startup yourself; only the session is saved. Rerun when it expires. |

## Tests

| Command | What it does |
|---|---|
| `.venv\Scripts\python -m pytest` | All tests (fixtures only, no network, no real Claude or schtasks calls). |
