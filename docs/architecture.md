# gitdrip architecture

How the pieces fit: agents, data flow, files, scheduling, safety, and LLM policy.

## 1. Agents

```
                 +------------------+
                 |   Orchestrator   |  (CLI tick / daemon / Actions / dashboard Run)
                 |  run_day / tick  |
                 +--------+---------+
                          |
        +-----------------+------------------+------------------+
        |                 |                  |                  |
+-------v------+ +--------v---------+ +------v-------+ +--------v--------+
| Requirements | | Architecture /   | |   Planning   | |     Coding      |
| extractor    | | plan shaper      | |   (Planner)  | | (Implementer +  |
| REQ-001..N   | | modules, files,  | | day phases,  | |  repair loop)   |
| from doc     | | validation cmds  | | reasoning    | | whole files,    |
+-------+------+ +--------+---------+ +------+-------+ | max 3/round     |
        |                 |                  |         +--------+--------+
        +-----------------+------------------+                  |
                          |                                     |
                   +------v-------+                    +--------v--------+
                   | plan.json    |                    |    Testing      |
                   | requirements |                    | validate cmds,  |
                   | architecture |                    | PASS/FAIL table |
                   | days[]       |                    +--------+--------+
                   +--------------+                             |
                                                                |
                                                       +--------v--------+
                                                       | Output + Email  |
                                                       | report md,      |
                                                       | verdict, inbox/ |
                                                       | GitHub issue    |
                                                       +----------------+
```

- Requirements + architecture passes turn the uploaded document into
  `requirements[]` (stable REQ ids) and an `architecture` sketch.
- Planner emits `days[]`: phase, reasoning, goal, tasks, files,
  validation, `report_expectation`, covered `reqs[]`, plus `attempts[]`.
- Coding writes whole files (≤3 per round, ≤8 rounds), then Testing runs
  each validation command locally. Failures trigger one repair round
  with the error output attached.
- Output writes the daily report and derives the verdict.

## 2. Data flow: doc -> plan -> day loop -> git -> report

```
project-doc.md
      |
      v
requirements + architecture  ──>  plan.json (days 1..N, reqs, attempts)
                                      |
                                      v  (once per day)
                              +---------------+
                              | implement (N) |── repair on FAIL
                              +-------+-------+
                                      |
                              +-------v-------+
                              | validate cmds |
                              +-------+-------+
                                      |
                              +-------v-------+
                              | output report |── reports/day-N.md
                              +-------+-------+
                                      |
                              +-------v-------+
                              | deliver       |── SMTP email, else GitHub issue
                              +-------+-------+
                                      |
                              +-------v-------+
                              | commit + push |── plan_state.json {days_done,
                              +----------------+  history[], pending_approval}
```

Each day appends to `plan_state.json` history:
`{day, phase, verdict, files[], provider, time, sha, error?}`.
Verdict is `CONFIRMED` (all green), `NEEDS_REVIEW` (partial),
`BLOCKED` (nothing usable), `NEEDS_APPROVAL` (review mode),
or `PAUSED` (loop suspended).

## 3. `.gitdrip` file layout

```
.gitdrip/
├── config.json        # target repo, remote, branch, push time, batch size (local only)
├── settings.json      # llm {provider,model,base_url}, email/smtp, paused, mode (committed)
├── secrets.json       # llm_key, smtp_password (NEVER committed)
├── project-doc.md     # uploaded document snapshot (committed)
├── plan.json          # title, summary, requirements[], architecture, days[], attempts (committed)
├── plan_state.json    # days_done, history[], pending_approval (committed)
├── agent-log.jsonl    # JSON-lines events {t, event, day, detail} (local diagnostic tail)
├── reports/           # day-1.md, day-2.md, ... (committed)
├── queue.json         # batch mode: pending/pushed batches (local)
├── staged/            # batch mode snapshots (local)
└── run.log            # push / tick history (local)
```

Committed files (`plan.json`, `plan_state.json`, `project-doc.md`,
`settings.json`, `reports/`) travel to GitHub so cloud runs can
continue without the laptop. Secrets, queue, staged snapshots and
logs stay local. Coverage (`{total, done, partial, open, missing}`)
is derived from `requirements[]` × per-day `reqs[]` × history
verdicts (`gitdrip/memory.py` when present, else the web fallback).

## 4. Scheduler tracks

```
LOCAL (this machine)                      CLOUD (laptop off)
---------------------                     --------------------
schedule --time HH:MM                     deploy
  |                                         |
  v                                         v
Windows Task Scheduler                    .github/workflows/gitdrip-daily.yml
  `gitdrip run --scheduled`                 cron (UTC) → pip install gitdrip
  |                                         → gitdrip cloud-run
  v                                         → commit day files w/ runner token
daemon (stay resident,                     → report as issue (or SMTP secret)
  fire at push_time)
  |
  v
tick: plan exists? → run_day
      else → next batch
```

The dashboard Deploy tab writes the workflow and optionally syncs
`llm_key` / `smtp_password` into repo secrets. Trigger any time via
Actions → gitdrip daily → Run workflow.

## 5. Safety gates

```
validate ── FAIL ──> repair (once, with error output) ── FAIL ──> BLOCKED: STOP + notify
                                                                              (report + issue/email,
                                                                               history.error kept,
                                                                               no push of broken code)
pause:  POST /api/pause sets settings.paused=true; tick/run short-circuit
        with status PAUSED until POST /api/resume.
review: settings.mode="review" parks finished days in
        plan_state.pending_approval; POST /api/approve releases them.
        mode="auto" commits straight through.
unsafe validation commands (rm -rf, curl/wget, Invoke-WebRequest, …)
  are rejected by the safety filter before execution.
```

## 6. LLM policy

- Default path needs **no key**: ordered chain is explicit user key
  (env or dashboard) → **free keyless endpoint** → structural
  heuristic fallback. Set provider/model/base URL in the dashboard
  or `.gitdrip/settings.json` (`auto`, `free`, `heuristic`, `openai`,
  `anthropic`, `gemini`, `groq`, `openrouter`, `custom`).
- Paid providers are used **only** with a user-entered API key
  (dashboard secret field or `*_API_KEY` env var, optionally synced
  to GitHub repo secrets on deploy). gitdrip never ships keys.
- **No local LLMs**: there is no ollama/localhost/self-hosted path;
  `custom` means an OpenAI-compatible HTTPS endpoint plus your key.
