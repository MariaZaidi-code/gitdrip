import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_NAME = "gitdrip-cloudbuild-test"
SPEC = """# NotesHub - small note-taking app
## Overview
A minimal Python note manager with file storage.
## Features
- add, list, search notes
- JSON file persistence
- README with usage
## Validation
Python: compileall must pass.
"""


def gh(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitdrip-cloud-test",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from None


def get_token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=10,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line[9:]
    raise RuntimeError("no github credential")


def sh(*args, check=True):
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr or proc.stdout}")
    return proc


def main() -> int:
    token = get_token()
    slug = f"MariaZaidi-code/{REPO_NAME}"
    base = f"https://api.github.com/repos/{slug}"

    print("creating private test repo...")
    gh("POST", "https://api.github.com/user/repos", token,
       {"name": REPO_NAME, "private": True, "description": "temporary gitdrip cloud test - auto-deleted"})
    try:
        work = Path(tempfile.mkdtemp(prefix="gitdrip-cloud-"))
        root = work / "project"
        root.mkdir()
        sh("git", "init", "-b", "main", str(root))
        sh("git", "-C", str(root), "config", "user.email", "test@example.com")
        sh("git", "-C", str(root), "config", "user.name", "gitdrip-test")
        sh("gitdrip", "init", "--project", str(root), "--target", str(root),
           "--remote-url", f"https://github.com/{slug}.git", "--time", "09:15")

        from fastapi.testclient import TestClient
        from gitdrip.web import create_app

        client = TestClient(create_app(root))
        assert client.post("/api/upload", json={"name": "spec.md", "text": SPEC}).status_code == 200
        assert client.post("/api/config", json={"llm": {"provider": "free"}}).status_code == 200
        r = client.post("/api/plan", json={"days": 2})
        assert r.status_code == 200, r.text
        print("plan:", r.json()["plan"]["title"], "via", r.json()["plan"]["provider"])

        print("deploying (workflow + plan push)...")
        r = client.post("/api/deploy", json={"sync_llm_key": False, "sync_smtp": False})
        assert r.status_code == 200, r.text
        for m in r.json()["messages"]:
            print("  -", m)

        print("triggering workflow_dispatch...")
        gh("POST", f"{base}/actions/workflows/gitdrip-daily.yml/dispatches", token, {"ref": "main"})

        deadline = time.time() + 600
        run_info = {}
        while time.time() < deadline:
            time.sleep(20)
            runs = gh("GET", f"{base}/actions/runs?per_page=3", token)
            if not runs.get("workflow_runs"):
                continue
            run_info = runs["workflow_runs"][0]
            print(f"  run #{run_info['run_number']}: {run_info['status']} {run_info.get('conclusion') or ''}")
            if run_info["status"] == "completed":
                break

        if run_info.get("status") != "completed":
            print("TIMEOUT waiting for workflow")
            return 1
        print("workflow conclusion:", run_info.get("conclusion"))
        log = gh("GET", f"{base}/actions/runs/{run_info['id']}/jobs", token)
        for job in log.get("jobs", []):
            for step in job.get("steps", []):
                print(f"    step {step['name']}: {step['conclusion']}")

        commits = gh("GET", f"{base}/commits?per_page=10", token)
        messages = [c["commit"]["message"].split("\n")[0] for c in commits]
        print("remote commits:")
        for m in messages:
            print("   ", m)
        day_commits = [m for m in messages if m.startswith("drip day")]

        issues = gh("GET", f"{base}/issues?per_page=5", token)
        report_issues = [i for i in issues if i.get("labels") and any(l["name"] == "gitdrip-report" for l in i["labels"])]
        print(f"report issues: {len(report_issues)}" + (f" -> #{report_issues[0]['number']}" if report_issues else ""))

        files = gh("GET", f"{base}/contents/.gitdrip/reports", token)
        names = [f["name"] for f in files] if isinstance(files, list) else []
        print("cloud reports:", names)

        ok = (
            run_info.get("conclusion") == "success"
            and day_commits
            and report_issues
            and any(n.startswith("day-1") for n in names)
        )
        if ok:
            print("\nCLOUD E2E PASSED: laptop-off run implemented, validated, committed, reported")
        else:
            print(f"\nCLOUD E2E FAILED: conclusion={run_info.get('conclusion')} day_commits={day_commits} "
                  f"issues={len(report_issues)} reports={names}")
            logs = gh("GET", f"{base}/actions/runs/{run_info['id']}/logs", token) if False else None
        return 0 if ok else 1
    finally:
        print(f"deleting {slug}...")
        try:
            gh("DELETE", base, token)
        except Exception as exc:
            print("cleanup failed (delete manually):", exc)


if __name__ == "__main__":
    sys.exit(main())
