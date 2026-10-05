from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from gitdrip.config import load_config
from gitdrip.runner import run_once


def next_run_time(push_time: str) -> dt.datetime:
    hour, minute = (int(p) for p in push_time.split(":"))
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def _sleep_until(target: dt.datetime) -> None:
    while True:
        remaining = (target - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(30.0, remaining))


def daemon(project: Path, run_now: bool = False) -> int:
    cfg = load_config(project)
    if run_now:
        for line in run_once(project, cfg):
            print(line)
    while True:
        cfg = load_config(project)
        target = next_run_time(cfg.push_time)
        print(f"gitdrip daemon: next push at {target:%Y-%m-%d %H:%M} (ctrl+c to stop)")
        _sleep_until(target)
        print(f"gitdrip daemon: pushing batch at {dt.datetime.now():%H:%M}")
        try:
            for line in run_once(project, cfg):
                print(line)
        except Exception as exc:
            print(f"gitdrip daemon: push failed: {exc}")
            time.sleep(60)
