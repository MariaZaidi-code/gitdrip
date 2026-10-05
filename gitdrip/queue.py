from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from gitdrip.config import GITDRIP_DIR, QUEUE_NAME, STAGED_DIR, Config, GitdripError, gitdrip_dir

DEFAULT_EXCLUDES = {".git", GITDRIP_DIR, "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
DOT_DIRS_ALLOWED = {".github", ".circleci", ".husky"}


@dataclass
class StagedFile:
    target_rel: str
    snapshot: str
    size: int
    source: str = ""


@dataclass
class Batch:
    id: int
    message: str
    created: str
    status: str
    files: list[StagedFile]
    finished: str = ""
    sha: str = ""


def queue_path(project: Path) -> Path:
    return gitdrip_dir(project) / QUEUE_NAME


def load_queue(project: Path) -> dict:
    path = queue_path(project)
    if not path.is_file():
        return {"version": 1, "next_id": 1, "batches": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_queue(project: Path, queue: dict) -> None:
    path = queue_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")


def _parse_batch(raw: dict) -> Batch:
    return Batch(
        id=raw["id"],
        message=raw["message"],
        created=raw["created"],
        status=raw["status"],
        files=[StagedFile(**f) for f in raw["files"]],
        finished=raw.get("finished", ""),
        sha=raw.get("sha", ""),
    )


def parse_batches(queue: dict) -> list[Batch]:
    return [_parse_batch(b) for b in queue["batches"]]


def pending_batches(queue: dict) -> list[Batch]:
    return [b for b in parse_batches(queue) if b.status == "pending"]


def _skip_dir(name: str) -> bool:
    if name in DEFAULT_EXCLUDES or name.endswith(".git"):
        return True
    return name.startswith(".") and name not in DOT_DIRS_ALLOWED


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise GitdripError(f"path not found: {path}")
    if path.resolve() == Path.home().resolve():
        raise GitdripError(
            "refusing to stage your home directory; cd into the project folder first"
        )
    if _skip_dir(path.name):
        return []
    out: list[Path] = []

    def walk(dirpath: Path) -> None:
        try:
            with os.scandir(dirpath) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not _skip_dir(entry.name):
                        walk(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    out.append(Path(entry.path))
            except OSError:
                continue

    walk(path)
    return out


def _target_rel(file: Path, cfg: Config, cwd: Path, project: Path) -> str:
    target = Path(cfg.target_repo).resolve()
    for base in (target, cwd, project):
        try:
            return file.resolve().relative_to(base).as_posix()
        except ValueError:
            continue
    raise GitdripError(
        f"{file} is outside both the target repo and the current directory; "
        "run stage from the source folder"
    )


def _resolve_arg(raw: str, cwd: Path, project: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    candidate = (cwd / path).resolve()
    if candidate.exists() and candidate != Path.home().resolve():
        return cwd / path
    fallback = project / path
    if fallback.exists():
        return fallback
    return cwd / path


def stage_paths(
    project: Path,
    cfg: Config,
    paths: list[str],
    message: str | None,
    batch_size: int | None = None,
) -> list[Batch]:
    if batch_size is None or batch_size < 1:
        batch_size = cfg.batch_size
    cwd = Path.cwd().resolve()
    queue = load_queue(project)
    pending_rels = {f.target_rel for b in pending_batches(queue) for f in b.files}

    files: list[StagedFile] = []
    skipped: list[str] = []
    for raw in paths:
        for file in _iter_files(_resolve_arg(raw, cwd, project)):
            rel = _target_rel(file, cfg, cwd, project)
            if rel in pending_rels:
                skipped.append(rel)
                continue
            files.append(StagedFile(target_rel=rel, snapshot="", size=file.stat().st_size, source=str(file)))
    if not files:
        raise GitdripError("nothing new to stage")

    created: list[Batch] = []
    chunks = [files[i : i + batch_size] for i in range(0, len(files), batch_size)]
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        bid = queue["next_id"]
        queue["next_id"] = bid + 1
        staging_root = gitdrip_dir(project) / STAGED_DIR / str(bid)
        for sf in chunk:
            src = Path(sf.source)
            if not src.is_file():
                raise GitdripError(f"source file disappeared during staging: {sf.source}")
            dest = staging_root / sf.target_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            sf.snapshot = f"{GITDRIP_DIR}/{STAGED_DIR}/{bid}/{sf.target_rel}"
        auto = f"drip: add {chunk[0].target_rel}"
        if len(chunk) > 1:
            auto += f" (+{len(chunk) - 1} more)"
        text = message or auto
        if message and total > 1:
            text = f"{text} ({index}/{total})"
        batch = Batch(
            id=bid,
            message=text,
            created=dt.datetime.now().isoformat(timespec="seconds"),
            status="pending",
            files=chunk,
        )
        queue["batches"].append(_serialize(batch))
        created.append(batch)
    save_queue(project, queue)
    for rel in skipped:
        print(f"skip (already queued): {rel}")
    return created


def _serialize(batch: Batch) -> dict:
    return {
        "id": batch.id,
        "message": batch.message,
        "created": batch.created,
        "status": batch.status,
        "files": [vars(f) for f in batch.files],
        "finished": batch.finished,
        "sha": batch.sha,
    }


def mark_finished(project: Path, batch_id: int, sha: str) -> None:
    queue = load_queue(project)
    for raw in queue["batches"]:
        if raw["id"] == batch_id:
            raw["status"] = "pushed" if sha else "done"
            raw["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            raw["sha"] = sha
    save_queue(project, queue)
    staged = gitdrip_dir(project) / STAGED_DIR / str(batch_id)
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
