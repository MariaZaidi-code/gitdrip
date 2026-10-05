import json
import subprocess
import sys
import time
import urllib.request

SLUG = "MariaZaidi-code/gitdrip-cloudbuild-test"
BASE = f"https://api.github.com/repos/{SLUG}"


def get_token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=10,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line[9:]
    raise RuntimeError("no token")


TOKEN = get_token()


def gh(method: str, url: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitdrip-cloud-test",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


print("triggering workflow_dispatch...")
gh("POST", f"{BASE}/actions/workflows/gitdrip-daily.yml/dispatches", {"ref": "main"})

deadline = time.time() + 600
run_info = {}
while time.time() < deadline:
    time.sleep(20)
    runs = gh("GET", f"{BASE}/actions/runs?per_page=3")
    if not runs.get("workflow_runs"):
        continue
    run_info = runs["workflow_runs"][0]
    print(f"  run #{run_info['run_number']}: {run_info['status']} {run_info.get('conclusion') or ''}")
    if run_info["status"] == "completed":
        break

if run_info.get("status") != "completed":
    print("TIMEOUT")
    sys.exit(1)

print("conclusion:", run_info.get("conclusion"))
jobs = gh("GET", f"{BASE}/actions/runs/{run_info['id']}/jobs")
for job in jobs.get("jobs", []):
    for step in job.get("steps", []):
        print(f"    {step['name']}: {step['conclusion']}")

commits = gh("GET", f"{BASE}/commits?per_page=10")
messages = [c["commit"]["message"].split("\n")[0] for c in commits]
day_commits = [m for m in messages if m.startswith("drip day")]
print("remote commits:", messages)
print("drip commits:", day_commits)

issues = gh("GET", f"{BASE}/issues?per_page=5")
report_issues = [i for i in issues
                 if i.get("labels") and any(l["name"] == "gitdrip-report" for l in i["labels"])]
print(f"report issues: {len(report_issues)}" + (f" -> #{report_issues[0]['number']}" if report_issues else ""))
if report_issues:
    print("issue title:", report_issues[0]["title"])
    print("issue body head:", report_issues[0]["body"][:400])

try:
    files = gh("GET", f"{BASE}/contents/.gitdrip/reports")
    names = [f["name"] for f in files] if isinstance(files, list) else []
except Exception:
    names = []
print("cloud reports:", names)

ok = (run_info.get("conclusion") == "success" and day_commits
      and report_issues and any(n.startswith("day-1") for n in names))
print("\nCLOUD E2E: " + ("PASSED" if ok else "FAILED"))
sys.exit(0 if ok else 1)
