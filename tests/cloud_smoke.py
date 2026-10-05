"""Cloud smoke test: full laptop-off proof against real GitHub.

Runs only when env GITDRIP_SMOKE == "1", else prints SKIP + exit 0.
Stdlib + fastapi TestClient only (deploy path may need pynacl, already
an optional dep of the package).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SPEC = """# SmokeBoard - Project Specification

## Overview
A tiny task board CLI in Python for tracking work items.

## Core features
- Create, list and complete tasks
- Persist tasks in a JSON file

## Data model
Task: id (int), title (str), done (bool).

## Validation
Python project: compileall must pass.

## Deliverables
Working code, README usage section.
"""

API = "https://api.github.com"
DAY_MSG = re.compile(r"(feat|fix|docs|test|chore)\(day \d+\)", re.IGNORECASE)


def sh(*args, cwd=None, check=True, input=None, timeout=60):
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", input=input, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr or proc.stdout}")
    return proc


def gitdrip_base():
    exe = shutil.which("gitdrip")
    if exe:
        return [exe]
    return [sys.executable, "-m", "gitdrip"]


def api(method, url, token, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitdrip-smoke",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(detail) if detail.strip() else {}
        except Exception:
            return exc.code, {"message": detail[:500]}


def get_token():
    """Token via `git credential fill` for https/github.com (env fallback)."""
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                tok = line[len("password="):].strip()
                if tok:
                    return tok, "credential-helper"
    except Exception as exc:
        print(f"credential fill error (will try env): {exc}")
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env, "GITHUB_TOKEN env"
    return "", ""


def main() -> int:
    if os.environ.get("GITDRIP_SMOKE") != "1":
        print("SKIP cloud smoke (set GITDRIP_SMOKE=1 to run)")
        return 0

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("SMOKE FAIL: needs the web extra: pip install -e '.[web]'",
              file=sys.stderr)
        return 1

    token, source = get_token()
    if not token:
        print("SMOKE FAIL: no GitHub token from `git credential fill` "
              "and no GITHUB_TOKEN env", file=sys.stderr)
        return 1
    print(f"token source: {source}")

    stamp = int(time.time())
    repo_name = f"gitdrip-smoke-{stamp}"
    status, data = api("POST", f"{API}/user/repos", token,
                       {"name": repo_name, "private": True, "auto_init": False})
    if status not in (200, 201) or not data.get("full_name"):
        print(f"SMOKE FAIL: repo create failed ({status}): {data}",
              file=sys.stderr)
        return 1
    slug = data["full_name"]
    print(f"created private repo: {slug}")

    base = Path(tempfile.mkdtemp(prefix="gitdrip-smoke-"))
    root = base / "project"
    root.mkdir(parents=True)
    slug_for_url = slug  # owner/repo
    auth_remote = f"https://x-access-token:{token}@github.com/{slug_for_url}.git"

    def cleanup_warn():
        status, _ = api("DELETE", f"{API}/repos/{slug}", token)
        if status not in (200, 202, 204):
            print(f"WARN: could not auto-delete {slug} "
                  f"(status {status}); manual URL: https://github.com/{slug}")
        else:
            print(f"deleted repo {slug}")

    try:
        sh("git", "init", "-b", "main", str(root))
        sh("git", "-C", str(root), "config", "user.email", "smoke@example.com")
        sh("git", "-C", str(root), "config", "user.name", "gitdrip-smoke")

        proc = sh(*gitdrip_base(), "init", "--project", str(root),
                  "--target", str(root), "--remote-url", auth_remote,
                  "--time", "09:00", check=False)
        print("init:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            print(f"SMOKE FAIL: init failed: {proc.stderr or proc.stdout}",
                  file=sys.stderr)
            cleanup_warn()
            return 1

        from gitdrip.web import create_app
        client = TestClient(create_app(root))

        r = client.post("/api/upload", json={"name": "spec.md", "text": SPEC})
        assert r.status_code == 200, r.text
        r = client.post("/api/config", json={"llm": {"provider": "free"}})
        assert r.status_code == 200, r.text
        r = client.post("/api/plan", json={"days": 2})
        assert r.status_code == 200, r.text
        plan = r.json()["plan"]
        assert len(plan["days"]) == 2, plan
        print(f"plan ready: {plan['title']}")

        r = client.post("/api/deploy", json={})
        assert r.status_code == 200, r.text
        print("deploy:", r.json().get("messages"))

        status, _ = api(
            "POST",
            f"{API}/repos/{slug}/actions/workflows/gitdrip-daily.yml/dispatches",
            token, {"ref": "main"})
        if status not in (200, 201, 204):
            print(f"SMOKE FAIL: workflow_dispatch failed ({status})",
                  file=sys.stderr)
            cleanup_warn()
            return 1
        print("workflow_dispatch triggered")

        deadline = time.time() + 600
        ok = False
        while time.time() < deadline:
            time.sleep(20)
            status, runs = api(
                "GET", f"{API}/repos/{slug}/actions/runs?per_page=5", token)
            if status != 200:
                print(f"runs poll: {status}, retrying...")
                continue
            items = runs.get("workflow_runs") or []
            if not items:
                print("no workflow runs yet, waiting...")
                continue
            run = items[0]
            print(f"run {run.get('id')}: "
                  f"{run.get('status')}/{run.get('conclusion')}")
            if run.get("status") == "completed":
                if run.get("conclusion") == "success":
                    ok = True
                else:
                    print(f"SMOKE FAIL: workflow concluded "
                          f"{run.get('conclusion')}", file=sys.stderr)
                    cleanup_warn()
                    return 1
                break
        if not ok:
            print("SMOKE FAIL: workflow did not succeed within 10 min",
                  file=sys.stderr)
            cleanup_warn()
            return 1

        # >=1 day commit (conventional preferred, legacy accepted)
        status, commits = api(
            "GET", f"{API}/repos/{slug}/commits?per_page=30&sha=main", token)
        messages = [c.get("commit", {}).get("message", "")
                    for c in (commits if isinstance(commits, list) else [])]
        print("recent commits:", messages[:5])
        if not any("drip day" in m.lower() or DAY_MSG.search(m)
                   for m in messages):
            print(f"SMOKE FAIL: no day commit on remote: {messages}",
                  file=sys.stderr)
            cleanup_warn()
            return 1

        # >=1 gitdrip-report issue
        status, issues = api(
            "GET",
            f"{API}/repos/{slug}/issues?labels=gitdrip-report&state=all&per_page=10",
            token)
        if status != 200 or not (issues if isinstance(issues, list) else []):
            print(f"SMOKE FAIL: no gitdrip-report issue ({status}): {issues}",
                  file=sys.stderr)
            cleanup_warn()
            return 1
        print(f"report issues: {len(issues)}")

        # day-1 report present via contents API
        status, content = api(
            "GET",
            f"{API}/repos/{slug}/contents/.gitdrip/reports/day-1.md?ref=main",
            token)
        if status != 200 or not content.get("content"):
            print(f"SMOKE FAIL: day-1 report missing ({status})",
                  file=sys.stderr)
            cleanup_warn()
            return 1
        print("day-1 report present via contents API")

        cleanup_warn()
        print("\nCLOUD SMOKE PASSED")
        return 0
    except Exception as exc:
        print(f"SMOKE FAIL: {exc}", file=sys.stderr)
        try:
            cleanup_warn()
        except Exception:
            print(f"WARN: manual cleanup: https://github.com/{slug}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
