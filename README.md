# gitdrip

Two ways to keep your GitHub profile moving — one tool, one command set:

1. **Batch mode** — queue a *finished* codebase and push it **one batch per day** at a time you pick.
2. **Agent mode** — upload a project document; a planner turns it into a day-by-day phase plan, and every day an implement agent writes the next phase, a validator runs your checks, and a report lands in your inbox or as a GitHub issue. Runs even when your laptop is off (GitHub Actions).

## Install

```bash
pip install git+https://github.com/MariaZaidi-code/gitdrip.git
```

Or from a checkout: `pip install -e ".[all]"` (extras: `web` dashboard, `pdf` document parsing, `deploy` secret sync).

Requires Python 3.9+ and `git` on PATH.

## Batch mode quick start

```bash
cd C:\path\to\your-project
gitdrip init --remote-url https://github.com/you/your-repo.git --time 09:00 --batch-size 5
gitdrip stage . -m "initial upload"     # split all files into daily batches
gitdrip schedule                        # register daily push (Windows Task Scheduler)
gitdrip run --dry-run                   # preview what is queued
```

Each day at the configured time, the next batch is copied into the repo, committed, and pushed. When the queue empties, nothing happens until you stage more.

## Agent mode quick start

```bash
cd C:\path\to\your-new-project
gitdrip init --remote-url https://github.com/you/your-repo.git --time 09:00
gitdrip plan 5 --doc spec.md             # planner builds phases with reasoning
gitdrip agent-run                        # implement + validate + report today's phase
gitdrip web                              # dashboard at http://127.0.0.1:7788
```

Or do all of it from the dashboard: upload the document (`.md`, `.txt`, `.docx`, `.pdf`), press **Plan**, press **Run day**, watch reports and verdicts.

### Daily loop

For each day the pipeline is: **plan → implement → validate → output report → deliver → commit & push**.

- The implement agent gets the phase, the repo tree, and prior reasoning, and returns whole files as JSON.
- Validations (from the plan, e.g. `python -m compileall -q .`, tests, linters) run locally; failures trigger a repair pass with the error output.
- The report (what changed, files, validation table, verdict `CONFIRMED` / `NEEDS_REVIEW`, notes for tomorrow) is saved to `.gitdrip/reports/day-N.md`, committed, and delivered by **email (SMTP)** if configured, otherwise filed as a **GitHub issue** labeled `gitdrip-report`.
- `plan_state.json` records history, verdicts and commit SHAs; re-running a finished day needs `--force`.

### LLM providers

LLM policy: the **free keyless endpoint** works with no key; paid providers (`openai`, `anthropic`, `gemini`, `groq`, `openrouter`, `custom`) run **only** with a user-entered API key (dashboard secret field or `*_API_KEY` env var). **No local LLMs.**

No key required: the default `auto` chain uses whatever is available — your key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` in the environment, or a key stored in the dashboard), then the **keyless free endpoint**, then a structural heuristic fallback. Set an explicit provider/model/base URL in the dashboard or `.gitdrip/settings.json`.

```bash
gitdrip agent-run                   # provider comes from settings.json / dashboard
```

Set an explicit provider/model/base URL in the dashboard or `.gitdrip/settings.json` (`auto`, `free`, `heuristic`, `openai`, `anthropic`, `gemini`, `groq`, `openrouter`, `custom`).

### Drip speed

Days = speed. The Plan tab offers presets that just fill the days input: **Relaxed (10 days)**, **Steady (6)**, **Sprint (3)** — or type any 1–90. Fewer days means bigger phases per day; more days means smaller, safer commits.

### Pause & approval modes

- `gitdrip pause` / dashboard **Pause** sets `settings.paused=true`; the loop reports `PAUSED` and skips runs until `gitdrip resume` (or **Resume**).
- `mode: auto` commits each finished day straight through. `mode: review` parks finished days in `plan_state.json: pending_approval` until you press **Approve** (`gitdrip approve` / `POST /api/approve`).

### Requirement traceability + coverage

The planner extracts stable `REQ-xxx` ids into `plan.json: requirements[]` (+ `architecture`, per-day `reqs[]`, `attempts[]`). Each day card shows its covered REQ ids, provider, commit SHA and any error in red; the plan header shows `Requirements: X done / Y partial / Z open` plus a progress bar and an attempts warning when the planner fell back. Missing/unknown ids surface in `coverage.missing` (`GET /api/status`).

### Project memory

Per-project notes live under `.gitdrip/` (see `docs/architecture.md`): the document snapshot, plan + state history, and reports are committed so cloud runs resume where the laptop left off. When `gitdrip/memory.py` is present the dashboard prefers its `coverage(project)` helper, otherwise it computes coverage from plan + state.

### Agent log

Structured events stream to `.gitdrip/agent-log.jsonl` (`{t, event, day, detail}`). The dashboard **Agent log** tab (or `GET /api/agent-log`, last 200 events) renders them as timestamped rows — handy when a day goes `BLOCKED` or waits for approval.

### Works while your laptop is off

```bash
gitdrip deploy                          # writes .github/workflows/gitdrip-daily.yml + plan, pushes
```

The workflow runs daily on the configured cron (UTC), installs gitdrip from GitHub, runs `gitdrip cloud-run`, commits the day's files with the runner's token, and delivers the report as an issue (or SMTP if you synced that secret). Trigger it any time via **Actions → gitdrip daily → Run workflow**.

## Commands

| Command | What it does |
|---|---|
| `gitdrip init` | Create `.gitdrip/`, init the git repo, set remote / push time / batch size |
| `gitdrip stage <paths>` | Batch mode: snapshot files into daily batches (`-m`, `-n`) |
| `gitdrip run` | Batch mode: push the next batch now (`--dry-run`, `--all`) |
| `gitdrip plan <days> [--doc FILE]` | Agent mode: build the phase plan from a document (re-run to rebuild) |
| `gitdrip replan <days> [--doc FILE]` | Agent mode: rebuild the plan (alias for `plan`; keeps history unless reset) |
| `gitdrip agent-run [--day N] [--force]` | Agent mode: run today's implement/validate/report cycle |
| `gitdrip tick` | Daily entry point: next agent day if a plan exists, else next batch |
| `gitdrip pause` / `gitdrip resume` | Suspend / resume the daily loop (`POST /api/pause`, `/api/resume`; dashboard Settings) |
| `gitdrip approve` | Release a day waiting in review mode (`POST /api/approve`; dashboard Approve button) |
| `gitdrip cloud-run` | What GitHub Actions executes daily (no args needed) |
| `gitdrip deploy` | Push the daily workflow + plan to GitHub (laptop-off mode) |
| `gitdrip web [--port 7788]` | Dashboard: upload, plan, run, reports, settings, deploy |
| `gitdrip schedule [--time HH:MM]` | Register the daily Windows scheduled task |
| `gitdrip unschedule` | Remove the scheduled task |
| `gitdrip daemon` | Stay running and fire at the configured time |
| `gitdrip status` / `gitdrip list -v` | Config, queue depth, plan progress, history |

## Files on disk

```
.gitdrip/
├── config.json       # target repo, remote, branch, push time, batch size (machine-local)
├── settings.json     # LLM provider/model/base URL, email/SMTP, paused, mode (committed)
├── secrets.json      # llm_key, smtp_password (never committed)
├── plan.json         # requirements, architecture, day-by-day phases, reqs, attempts (committed)
├── plan_state.json   # days_done, history, verdicts, SHAs, pending_approval (committed)
├── project-doc.md    # the uploaded document snapshot (committed)
├── agent-log.jsonl   # structured agent events {t, event, day, detail}
├── reports/          # day-1.md, day-2.md, ... (committed)
├── queue.json        # batch mode: pending/pushed batches
├── staged/           # batch mode: snapshots awaiting their push day
└── run.log           # history of pushes and scheduled runs
```

`init` writes an allowlist `.gitignore` so the state files above reach GitHub (needed for cloud runs) while `secrets.json`, `config.json`, staged snapshots and logs stay local.

## Typical workflows

```bash
# steady drip of a finished codebase
gitdrip status
gitdrip stage src/new-feature -m "add payment module"
gitdrip run

# build something new over a week, hands off
gitdrip plan 5 --doc spec.md
gitdrip web            # or schedule/daemon/cloud: gitdrip deploy
gitdrip agent-run      # today's phase, report delivered automatically
```
