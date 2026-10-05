from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from gitdrip import gitops, scheduler
from gitdrip.config import (
    Config,
    GitdripError,
    find_project,
    gitdrip_dir,
    load_config,
    save_config,
    validate_time,
)
from gitdrip.queue import load_queue, parse_batches, pending_batches, stage_paths
from gitdrip.runner import run_once, run_scheduled


def _cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project or Path.cwd()).resolve()
    target = Path(args.target or project).resolve()
    gitdrip_dir(project).mkdir(parents=True, exist_ok=True)
    if not gitops.is_git_repo(target):
        gitops.git(target, "init", "-b", args.branch)
        print(f"initialized git repo at {target}")
    if args.remote_url:
        existing = gitops.git(target, "remote", "get-url", args.remote, check=False)
        if existing.returncode == 0:
            gitops.git(target, "remote", "set-url", args.remote, args.remote_url)
        else:
            gitops.git(target, "remote", "add", args.remote, args.remote_url)
    cfg = Config(
        target_repo=str(target),
        remote=args.remote,
        branch=gitops.current_branch(target) or args.branch,
        push_time=validate_time(args.time),
        batch_size=args.batch_size,
        task_name=f"gitdrip-{target.name.lower()}",
        created=dt.datetime.now().isoformat(timespec="seconds"),
    )
    save_config(project, cfg)
    _ensure_gitignore(target, project)
    print(f"gitdrip project ready at {project}")
    print(f"  target repo : {cfg.target_repo}")
    print(f"  branch      : {cfg.branch}")
    print(f"  push time   : {cfg.push_time}")
    print(f"  batch size  : {cfg.batch_size} file(s) per push")
    print("next: gitdrip stage <files-or-dirs>  then  gitdrip schedule")
    return 0


def _ensure_gitignore(target: Path, project: Path) -> None:
    try:
        project.relative_to(target)
    except ValueError:
        return
    path = target / ".gitignore"
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    if any(line.strip() == ".gitdrip/" for line in content.splitlines()):
        return
    if content and not content.endswith("\n"):
        content += "\n"
    content += ".gitdrip/\n"
    path.write_text(content, encoding="utf-8")


def _cmd_stage(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    cfg = load_config(project)
    created = stage_paths(project, cfg, args.paths, args.message, args.batch_size)
    total_files = sum(len(b.files) for b in created)
    print(f"staged {total_files} file(s) into {len(created)} batch(es):")
    for batch in created:
        print(f"  batch {batch.id}: {len(batch.files)} file(s) - {batch.message}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.scheduled:
        project = find_project(args.project)
        return run_scheduled(project)
    project = find_project(args.project)
    cfg = load_config(project)
    for line in run_once(project, cfg, dry_run=args.dry_run, run_all=args.all):
        print(line)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    cfg = load_config(project)
    queue = load_queue(project)
    pending = sorted(pending_batches(queue), key=lambda b: b.id)
    done = [b for b in parse_batches(queue) if b.status != "pending"]
    print(f"project     : {project}")
    print(f"target repo : {cfg.target_repo}")
    print(f"branch      : {cfg.branch} -> {cfg.remote}")
    print(f"push time   : {cfg.push_time}")
    print(f"batch size  : {cfg.batch_size}")
    print(f"scheduler   : {scheduler.schedule_status(cfg)}")
    print(f"queue       : {len(pending)} pending, {len(done)} completed")
    for batch in pending[:5]:
        print(f"  next batch {batch.id}: {len(batch.files)} file(s) - {batch.message}")
    if len(pending) > 5:
        print(f"  ... and {len(pending) - 5} more")
    if done:
        last = done[-1]
        print(f"  last push  : batch {last.id} {last.sha} at {last.finished}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    queue = load_queue(project)
    for batch in sorted(parse_batches(queue), key=lambda b: b.id):
        marker = "*" if batch.status == "pending" else "x"
        print(f"{marker} batch {batch.id:3d} [{batch.status:7s}] {len(batch.files):3d} file(s)  {batch.message}")
        if args.verbose:
            for f in batch.files:
                print(f"      {f.target_rel}")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    cfg = load_config(project)
    if args.time:
        cfg.push_time = validate_time(args.time)
        save_config(project, cfg)
    print(scheduler.schedule(cfg, project))
    return 0


def _cmd_unschedule(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    cfg = load_config(project)
    print(scheduler.unschedule(cfg))
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    from gitdrip.daemon import daemon

    project = find_project(args.project)
    try:
        daemon(project, run_now=args.run_now)
    except KeyboardInterrupt:
        print("\ndaemon stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitdrip",
        description="Queue finished code and push it to GitHub in daily batches.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a gitdrip project")
    p.add_argument("--project", help="where to keep the gitdrip queue (default: cwd)")
    p.add_argument("--target", help="git repo to push to (default: project dir)")
    p.add_argument("--remote-url", help="remote url, e.g. https://github.com/user/repo.git")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="main")
    p.add_argument("--time", default="09:00", help="daily push time HH:MM")
    p.add_argument("--batch-size", type=int, default=5, help="files per push")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("stage", help="queue files for future pushes")
    p.add_argument("paths", nargs="+", help="files or directories to queue")
    p.add_argument("-m", "--message", help="commit message for the batch(es)")
    p.add_argument("-n", "--batch-size", type=int, help="files per batch")
    p.add_argument("--project")
    p.set_defaults(func=_cmd_stage)

    p = sub.add_parser("run", help="push the next queued batch")
    p.add_argument("--project")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="drain the whole queue now")
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("status", help="show project, queue and schedule")
    p.add_argument("--project")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("list", help="list queued batches")
    p.add_argument("--project")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("schedule", help="register the daily scheduled push")
    p.add_argument("--project")
    p.add_argument("--time", help="override daily push time HH:MM")
    p.set_defaults(func=_cmd_schedule)

    p = sub.add_parser("unschedule", help="remove the scheduled push")
    p.add_argument("--project")
    p.set_defaults(func=_cmd_unschedule)

    p = sub.add_parser("daemon", help="stay running and push at the configured time")
    p.add_argument("--project")
    p.add_argument("--run-now", action="store_true", help="push one batch before waiting")
    p.set_defaults(func=_cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GitdripError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
