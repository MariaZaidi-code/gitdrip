"""Batch-mode E2E regression for gitdrip (no LLM calls).

Temp project + local bare remote; exercises init/stage/run/status
via subprocess CLI calls. Exit 0 + "BATCH E2E PASSED" or nonzero.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def sh(*args, cwd=None, check=True):
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr or proc.stdout}")
    return proc


def gitdrip_base():
    exe = shutil.which("gitdrip")
    if exe:
        return [exe]
    return [sys.executable, "-m", "gitdrip"]


def gd(*args, check=True):
    return sh(*gitdrip_base(), *args, check=check)


def fail(msg):
    print(f"BATCH E2E FAILED: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="gitdrip-batch-e2e-"))
    root = base / "project"
    remote = base / "remote.git"
    root.mkdir(parents=True)
    try:
        sh("git", "init", "--bare", str(remote))
        sh("git", "init", "-b", "main", str(root))
        sh("git", "-C", str(root), "config", "user.email", "test@example.com")
        sh("git", "-C", str(root), "config", "user.name", "gitdrip-test")

        proc = gd("init", "--project", str(root), "--target", str(root),
                  "--remote-url", str(remote), "--time", "09:00",
                  "--batch-size", "2", check=False)
        print("init:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"init failed: {proc.stderr or proc.stdout}")

        # a few files -> with batch-size 2 this yields 2 batches (2 + 1)
        src = root / "src"
        src.mkdir()
        (src / "a.txt").write_text("alpha\n", encoding="utf-8")
        (src / "b.txt").write_text("beta\n", encoding="utf-8")
        (src / "c.txt").write_text("gamma\n", encoding="utf-8")

        proc = gd("stage", str(src / "a.txt"), str(src / "b.txt"), str(src / "c.txt"),
                  "-m", "batch e2e file", "--project", str(root), check=False)
        print("stage:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"stage failed: {proc.stderr or proc.stdout}")
        if "2 batch" not in (proc.stdout or ""):
            return fail(f"expected 2 batches from stage, got: {proc.stdout}")

        proc = gd("run", "--project", str(root), "--dry-run", check=False)
        print("dry-run:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"dry-run failed: {proc.stderr or proc.stdout}")
        if "[dry-run] batch 1" not in (proc.stdout or ""):
            return fail(f"dry-run did not show queue: {proc.stdout}")
        if "(1/2)" not in (proc.stdout or "") or "(2/2)" not in (proc.stdout or ""):
            return fail(f"dry-run missing batch parts: {proc.stdout}")

        # batch 1
        proc = gd("run", "--project", str(root), check=False)
        print("run1:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"run 1 failed: {proc.stderr or proc.stdout}")
        log = sh("git", "-C", str(root), "log", "--oneline")
        print("target log after run1:\n" + log.stdout)
        if "(1/2)" not in log.stdout:
            return fail("batch 1 commit missing in target log")
        remote_log = sh("git", "-C", str(remote), "log", "--oneline", "main")
        if "(1/2)" not in remote_log.stdout:
            return fail("batch 1 push did not reach bare remote")

        # batch 2
        proc = gd("run", "--project", str(root), check=False)
        print("run2:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"run 2 failed: {proc.stderr or proc.stdout}")
        log = sh("git", "-C", str(root), "log", "--oneline")
        print("target log after run2:\n" + log.stdout)
        if "(2/2)" not in log.stdout:
            return fail("batch 2 commit missing in target log")
        remote_log = sh("git", "-C", str(remote), "log", "--oneline", "main")
        if "(2/2)" not in remote_log.stdout:
            return fail("batch 2 push did not reach bare remote")

        # queue empties
        proc = gd("run", "--project", str(root), check=False)
        if "empty" not in ((proc.stdout or "") + (proc.stderr or "")).lower():
            return fail(f"queue should be empty now, got: {proc.stdout}")
        proc = gd("status", "--project", str(root), check=False)
        print("status:", (proc.stdout or "").strip())
        if proc.returncode != 0:
            return fail(f"status failed: {proc.stderr or proc.stdout}")
        if "0 pending" not in (proc.stdout or ""):
            return fail(f"status should show 0 pending: {proc.stdout}")

        # scheduler must not throw; on non-Windows this is a stub string
        try:
            from gitdrip.config import load_config
            from gitdrip.scheduler import schedule_status
            s = schedule_status(load_config(root))
        except Exception as exc:
            return fail(f"schedule_status threw: {exc}")
        if not isinstance(s, str) or not s:
            return fail(f"schedule_status did not return a string: {s!r}")
        print(f"scheduler status: {s}")

        print("\nBATCH E2E PASSED")
        print(f"workspace kept at: {base}")
        return 0
    except Exception as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
