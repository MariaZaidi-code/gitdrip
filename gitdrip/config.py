from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

GITDRIP_DIR = ".gitdrip"
CONFIG_NAME = "config.json"
QUEUE_NAME = "queue.json"
STAGED_DIR = "staged"
LOG_NAME = "run.log"


class GitdripError(Exception):
    pass


@dataclass
class Config:
    target_repo: str
    remote: str = "origin"
    branch: str = "main"
    push_time: str = "09:00"
    batch_size: int = 5
    task_name: str = "gitdrip"
    created: str = field(default="")


def gitdrip_dir(project: Path) -> Path:
    return project / GITDRIP_DIR


def find_project(explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (gitdrip_dir(root) / CONFIG_NAME).is_file():
            raise GitdripError(f"no gitdrip config in {root}")
        return root
    cur = Path.cwd().resolve()
    for candidate in [cur, *cur.parents]:
        if (gitdrip_dir(candidate) / CONFIG_NAME).is_file():
            return candidate
    raise GitdripError("not inside a gitdrip project (run 'gitdrip init' first)")


def load_config(project: Path) -> Config:
    path = gitdrip_dir(project) / CONFIG_NAME
    if not path.is_file():
        raise GitdripError(f"missing config: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


def save_config(project: Path, cfg: Config) -> None:
    path = gitdrip_dir(project) / CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")


def validate_time(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise GitdripError(f"invalid time '{value}', expected HH:MM (24h)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise GitdripError(f"invalid time '{value}', expected HH:MM (24h)")
    return f"{hour:02d}:{minute:02d}"
