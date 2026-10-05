import json
import subprocess
import sys
import urllib.request


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


token = get_token()
slug = "MariaZaidi-code/gitdrip-cloudbuild-test"
base = f"https://api.github.com/repos/{slug}"


def gh(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "gitdrip-debug",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


runs = gh(f"{base}/actions/runs?per_page=1")
run = runs["workflow_runs"][0]
print("run:", run["id"], run["conclusion"])
jobs = gh(f"{base}/actions/runs/{run['id']}/jobs")
for job in jobs["jobs"]:
    print("job:", job["name"], job["conclusion"], "id:", job["id"])
    req = urllib.request.Request(
        f"{base}/actions/jobs/{job['id']}/logs",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "gitdrip-debug"},
    )

    class StripAuthRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
                new.remove_header("Authorization")
            return new

    try:
        opener = urllib.request.build_opener(StripAuthRedirect)
        with opener.open(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
        print("---- LOG TAIL ----")
        print(text[-6000:])
    except Exception as exc:
        print("log fetch failed:", exc)
