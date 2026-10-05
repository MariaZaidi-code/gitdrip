from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_git_repo(repo: Path) -> bool:
    if not repo.is_dir():
        return False
    return git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def ensure_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise GitError(f"target repo does not exist: {repo}")
    if not is_git_repo(repo):
        raise GitError(f"not a git repository: {repo}")


def has_commits(repo: Path) -> bool:
    return git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def current_branch(repo: Path) -> str:
    proc = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def has_upstream(repo: Path, remote: str, branch: str) -> bool:
    proc = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if proc.returncode == 0:
        return True
    proc = git(repo, "ls-remote", "--exit-code", "--heads", remote, branch, check=False)
    return proc.returncode == 0


def _identity_args(repo: Path) -> list[str]:
    args: list[str] = []
    if not git(repo, "config", "user.name", check=False).stdout.strip():
        args += ["-c", "user.name=gitdrip"]
    if not git(repo, "config", "user.email", check=False).stdout.strip():
        args += ["-c", "user.email=gitdrip@users.noreply.github.com"]
    return args


def commit_type_for(phase_text: str) -> str:
    text = (phase_text or "").lower()
    if any(k in text for k in ("fix", "bug", "error", "patch", "hotfix", "repair", "issue")):
        return "fix"
    if any(k in text for k in ("doc", "readme", "comment", "guide")):
        return "docs"
    if any(k in text for k in ("test", "coverage", "spec")):
        return "test"
    if any(k in text for k in (
        "chore", "refactor", "cleanup", "clean", "dependenc", "bump",
        "polish", "scaffold", "setup", "config", "ci", "build",
    )):
        return "chore"
    return "feat"


def commit_paths(repo: Path, paths: list[str], message: str) -> tuple[bool, str]:
    git(repo, "add", "-A", "--", *paths)
    proc = git(
        repo,
        *_identity_args(repo),
        "commit",
        "-m",
        message,
        "--only",
        "--",
        *paths,
        check=False,
    )
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).strip()
        if "nothing to commit" in out or "no changes added" in out:
            return False, "no changes"
        raise GitError(f"git commit failed:\n{out}")
    sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return True, sha


def push(repo: Path, remote: str, branch: str, set_upstream: bool = False) -> str:
    args = ["push"]
    if set_upstream:
        args += ["-u"]
    args += [remote, branch]
    proc = git(repo, *args, check=False)
    if proc.returncode != 0:
        raise GitError(f"git push failed:\n{(proc.stderr + proc.stdout).strip()}")
    return (proc.stdout + proc.stderr).strip()
