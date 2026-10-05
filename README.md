# gitdrip

Queue a finished codebase and push it to GitHub **one batch per day** at a time you pick — consistent contribution activity without writing everything at once.

## Why

You finish a project in one sitting, but you want your GitHub profile to show steady daily commits. gitdrip snapshots your files into ordered batches, then pushes exactly one batch per day (manually, via Windows Task Scheduler, or via a background daemon).

## Install

```bash
pip install -e .
```

Requires Python 3.9+ and `git` on PATH.

## Quick start

```bash
cd C:\path\to\your-project
gitdrip init --remote-url https://github.com/you/your-repo.git --time 09:00 --batch-size 5
gitdrip stage . -m "initial upload"     # split all files into daily batches
gitdrip schedule                        # register daily push (Windows Task Scheduler)
gitdrip run --dry-run                   # preview what is queued
```

Each day at the configured time, the next batch is copied into the repo, committed, and pushed. When the queue empties, nothing happens until you stage more.

## Commands

| Command | What it does |
|---|---|
| `gitdrip init` | Create the queue, init the git repo, set remote / push time / batch size |
| `gitdrip stage <paths>` | Snapshot files into batches (`-m` message, `-n` files per batch) |
| `gitdrip run` | Push the next batch now (`--dry-run` to preview, `--all` to drain the queue) |
| `gitdrip schedule [--time HH:MM]` | Register the daily Windows scheduled task |
| `gitdrip unschedule` | Remove the scheduled task |
| `gitdrip daemon` | Stay running and push at the configured time (no Task Scheduler needed) |
| `gitdrip status` | Config, queue depth, scheduler state, last push |
| `gitdrip list -v` | Every batch and the files inside it |

## How batching works

- `stage` walks your paths (skipping `.git`, `.gitdrip`, `node_modules`, `__pycache__`, virtualenvs, hidden dirs except `.github`/`.circleci`/`.husky`) and copies each file into `.gitdrip/staged/<batch-id>/`.
- Files are grouped into batches of `--batch-size` (default 5). Each batch becomes one commit.
- `run` pops **one** batch: copies its snapshot into the target repo, commits only those paths, and pushes. Remaining batches stay queued for the following days.
- Staging the same file twice while it is still pending is a no-op.

## Scheduling options

**Windows Task Scheduler** (via `gitdrip schedule`): registers a daily task with `StartWhenAvailable` enabled, so a missed run fires as soon as the machine is next available.

**Daemon** (works everywhere, also a good fallback):

```bash
gitdrip daemon            # sleeps until --time, pushes, repeats
gitdrip daemon --run-now  # push one batch immediately, then wait for the schedule
```

The daemon reloads `config.json` after every push, so you can change the push time without restarting it.

**Linux/macOS**: `gitdrip schedule` prints the cron line to add yourself.

## Files on disk

```
.gitdrip/
├── config.json    # target repo, remote, branch, push time, batch size
├── queue.json     # every batch, its status, messages and timestamps
├── staged/        # snapshots awaiting their push day (deleted once pushed)
└── run.log        # history of pushes and scheduled runs
```

`.gitdrip/` is added to `.gitignore` during `init` so it never reaches GitHub.

## Typical workflow

```bash
gitdrip status                     # how many days of pushes are left
gitdrip stage src/new-feature -m "add payment module"
gitdrip run                        # optional: push today's batch manually
gitdrip daemon                     # or let the daemon/scheduler handle it
```
