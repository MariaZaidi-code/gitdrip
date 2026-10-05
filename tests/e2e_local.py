import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:
    raise SystemExit("needs the web extra: pip install -e '.[web]'")

SPEC = """# TaskBoard - Project Specification

## Overview
A lightweight task board CLI + web app in Python for tracking work items.

## Core features
- Create, list and complete tasks
- Persist tasks in a JSON file
- Small HTTP API to read tasks

## Data model
Task: id (int), title (str), done (bool), created_at (iso str).

## Validation
Python project: compileall must pass.

## Deliverables
Working code, README usage section.
"""


def sh(*args, cwd=None, check=True):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr or proc.stdout}")
    return proc


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="gitdrip-e2e-"))
    root = base / "project"
    remote = base / "remote.git"
    root.mkdir()
    sh("git", "init", "--bare", str(remote))
    sh("git", "init", "-b", "main", str(root))
    sh("git", "-C", str(root), "config", "user.email", "test@example.com")
    sh("git", "-C", str(root), "config", "user.name", "gitdrip-test")

    proc = sh("gitdrip", "init", "--project", str(root), "--target", str(root),
              "--remote-url", str(remote), "--time", "09:00", check=False)
    print("init:", proc.stdout.strip())

    from gitdrip.web import create_app

    client = TestClient(create_app(root))

    r = client.get("/")
    assert r.status_code == 200 and "gitdrip" in r.text, "index page failed"
    print("GET / -> 200, html ok")

    r = client.get("/api/status")
    assert r.status_code == 200 and r.json()["ok"]
    print("status:", r.json()["plan"], r.json()["queue"])

    r = client.post("/api/upload", json={"name": "spec.md", "text": SPEC})
    assert r.status_code == 200, r.text
    print("upload:", r.json()["chars"], "chars")

    r = client.post("/api/config", json={"llm": {"provider": "auto"}})
    assert r.status_code == 200, r.text

    r = client.post("/api/plan", json={"days": 3})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert len(plan["days"]) == 3, f"expected 3 days, got {len(plan['days'])}"
    for d in plan["days"]:
        assert d["phase"] and d["reasoning"] and d["tasks"], f"day {d['day']} incomplete: {d}"
    print(f"plan: '{plan['title']}' via {plan['provider']}")
    for d in plan["days"]:
        print(f"   day {d['day']}: {d['phase']} | tasks={len(d['tasks'])} | validation={d['validation']}")

    print("running agent day 1 (LLM can take a minute)...")
    r = client.post("/api/run", json={})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    print("day1:", {k: result.get(k) for k in ("status", "day", "files", "commit", "delivery", "provider", "error")})
    print("validation:", result["validation"])
    assert result["day"] == 1
    assert result["status"] in ("confirmed", "needs_review", "blocked"), result["status"]
    assert Path(result["report_path"]).is_file(), "report missing"

    log = sh("git", "-C", str(root), "log", "--oneline")
    print("target log:\n" + log.stdout)
    assert "drip day 1" in log.stdout, "day 1 commit missing"
    remote_log = sh("git", "-C", str(remote), "log", "--oneline", "main")
    assert "drip day 1" in remote_log.stdout, "push missing"

    status = client.get("/api/status").json()
    assert status["plan"]["done"] == 1, status["plan"]
    assert status["doc"]["exists"] and status["doc"]["chars"] > 100

    r = client.get("/api/reports")
    assert r.json()["reports"] and r.json()["reports"][0]["day"] == 1
    r = client.get("/api/report/1")
    assert "Verdict" in r.json()["markdown"], "verdict missing in report"

    r = client.post("/api/run", json={})
    again = r.json()["result"]
    assert again["status"] in ("already_done", "complete") or again["day"] != 1, again
    print("idempotency check:", again.get("status", again.get("day")))

    print("\nALL LOCAL E2E CHECKS PASSED")
    print(f"workspace kept at: {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
