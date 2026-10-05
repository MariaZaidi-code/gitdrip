from __future__ import annotations

import datetime as dt
import shutil
import traceback
from pathlib import Path

from gitdrip import gitops
from gitdrip.config import GITDRIP_DIR, LOG_NAME, Config, GitdripError, gitdrip_dir
from gitdrip.queue import Batch, load_queue, mark_finished, pending_batches


def _log(project: Path, line: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = gitdrip_dir(project) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {line}\n")


def _apply_batch(project: Path, cfg: Config, batch: Batch) -> tuple[bool, str]:
    target = Path(cfg.target_repo).resolve()
    gitops.ensure_repo(target)
    for sf in batch.files:
        src = project / sf.snapshot
        if not src.is_file():
            raise GitdripError(f"snapshot missing for {sf.target_rel} (batch {batch.id})")
        dest = target / sf.target_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    changed, sha_or_note = gitops.commit_paths(target, [f.target_rel for f in batch.files], batch.message)
    remote_url = gitops.git(target, "remote", "get-url", cfg.remote, check=False)
    pushed = False
    if remote_url.returncode == 0:
        gitops.push(target, cfg.remote, cfg.branch, set_upstream=not gitops.has_upstream(target, cfg.remote, cfg.branch))
        pushed = True
    if changed:
        return True, f"commit {sha_or_note}" + (" + push" if pushed else " (no remote, commit only)")
    return True, ("pushed existing commits" if pushed else "no changes")


def run_once(project: Path, cfg: Config, dry_run: bool = False, run_all: bool = False) -> list[str]:
    queue = load_queue(project)
    batches = sorted(pending_batches(queue), key=lambda b: b.id)
    if not batches:
        return ["queue is empty, nothing to push"]
    if dry_run:
        return [f"[dry-run] batch {b.id}: {len(b.files)} file(s) - {b.message}" for b in batches]
    results = []
    selected = batches if run_all else batches[:1]
    for batch in selected:
        preview = f"batch {batch.id}: {len(batch.files)} file(s) - {batch.message}"
        try:
            _, note = _apply_batch(project, cfg, batch)
            mark_finished(project, batch.id, note.split()[1] if note.startswith("commit ") else "")
            line = f"{preview} -> {note}"
        except Exception as exc:
            _log(project, f"ERROR batch {batch.id}: {exc}")
            results.append(f"FAILED {preview}: {exc}")
            _log(project, "\n".join(results))
            raise GitdripError(f"batch {batch.id} failed: {exc}") from exc
        _log(project, line)
        results.append(line)
    remaining = len(batches) - len(selected)
    if remaining:
        results.append(f"{remaining} batch(es) still queued for later days")
    return results


def run_scheduled(project: Path) -> int:
    try:
        from gitdrip.config import load_config

        cfg = load_config(project)
        run_once(project, cfg)
        return 0
    except Exception:
        _log(project, "SCHEDULED RUN FAILED\n" + traceback.format_exc())
        return 1
