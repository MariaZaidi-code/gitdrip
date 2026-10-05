"""Run gitdrip E2E suites sequentially and summarize.

Batch first, then local agent suite; cloud smoke only when
GITDRIP_SMOKE == "1". Exit nonzero if any executed suite fails.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

SUITES = [
    ("batch", HERE / "e2e_batch.py", 300),
    ("local", HERE / "e2e_local.py", 1500),
]
CLOUD = ("cloud", HERE / "cloud_smoke.py", 900)


def run_suite(name, script, timeout):
    print(f"\n===== suite: {name} ({script.name}) =====")
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        elapsed = time.time() - start
        out = (proc.stdout or "") + (proc.stderr or "")
        print(out[-6000:])
        if proc.returncode == 0 and "SKIP" in out:
            return "SKIP", elapsed
        return ("PASS" if proc.returncode == 0 else "FAIL"), elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        print(f"suite {name} timed out after {timeout}s")
        return "FAIL", elapsed


def main() -> int:
    total_start = time.time()
    suites = list(SUITES)
    if os.environ.get("GITDRIP_SMOKE") == "1":
        suites.append(CLOUD)
    else:
        print("cloud smoke gated off (set GITDRIP_SMOKE=1 to run it)")

    results = {}
    for name, script, timeout in suites:
        if not script.is_file():
            print(f"suite {name}: script missing: {script}")
            results[name] = "FAIL"
            continue
        status, elapsed = run_suite(name, script, timeout)
        results[name] = status
        print(f"--> {name}: {status} ({elapsed:.1f}s)")

    total = time.time() - total_start
    print("\n===== SUMMARY =====")
    for name in results:
        print(f"{name}: {results[name]}")
    print(f"total runtime: {total:.1f}s")

    if any(v == "FAIL" for v in results.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
