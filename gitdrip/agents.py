from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from gitdrip import gitops
from gitdrip.config import Config, GitdripError, gitdrip_dir, load_config
from gitdrip.llm import LLMClient, extract_json, client_for, load_secrets, load_settings
from gitdrip.plan import DOC_SNAPSHOT, PLAN_NAME, STATE_NAME, load_plan, load_state, save_state
from gitdrip.queue import _iter_files

REPORTS_DIR = "reports"
MAX_CONTEXT_FILES = 15
MAX_CONTEXT_CHARS = 24_000
VALIDATION_TIMEOUT = 180

IMPLEMENT_SYSTEM = """You are the Implementer Agent in an automated daily-build system.
Implement exactly the requested phase of the plan in the given repository.

Respond with ONLY a JSON object, no prose, no markdown fence:
{"files": {"relative/path.ext": "FULL file content"}, "reasoning": "short explanation"}

Rules:
- Return complete file contents only: never diffs, never placeholders, never "...".
- Only create or modify files required by this phase (at most 4 files per reply).
- Keep each file focused and reasonably sized.
- Match the existing project's style, imports and structure from the repository context.
- Use forward slashes in paths, no absolute paths, no "..".
- If the phase cannot be implemented as specified, return the closest safe implementation
  and explain the limitation in "reasoning"."""

REPAIR_SYSTEM = """You are the Implementer Agent performing a repair round.
Your previous files failed validation. Fix ONLY the reported problems and return the
full corrected content of the affected files.

Respond with ONLY a JSON object, no prose, no markdown fence:
{"files": {"relative/path.ext": "FULL corrected file content"}, "reasoning": "what you fixed"}"""

OUTPUT_SYSTEM = """You are the Output Agent in an automated daily-build system.
You confirm what happened today and write the user's daily report in markdown.

Respond with ONLY markdown (no JSON, no fences around the whole reply) with sections:
## Day N - <phase>
### What was implemented
(bullets, including the implementer's reasoning)
### Files changed
(bullets)
### Validation
(every command, PASS/FAIL, brief output evidence)
### Verdict
One line exactly: "Verdict: CONFIRMED" if every validation passed and the phase goals were
met, "Verdict: NEEDS_REVIEW" if something is partial or unverified, or
"Verdict: BLOCKED" if implementation failed or validations could not pass.
### Notes for tomorrow
(1-2 sentences the next day's run should know)
Be factual: never claim work that is not in the evidence you were given."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def build_repo_context(target: Path) -> str:
    try:
        files = _iter_files(target)
    except GitdripError:
        files = []
    lines = []
    for file in files[:400]:
        try:
            size = file.stat().st_size
        except OSError:
            continue
        lines.append(f"{file.relative_to(target).as_posix()} ({size}B)")
    tree = "\n".join(lines) or "(empty repository)"
    budget = MAX_CONTEXT_CHARS
    snippets = []
    count = 0
    for file in files:
        if count >= MAX_CONTEXT_FILES or budget <= 0:
            break
        try:
            if file.stat().st_size > 6000:
                continue
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = file.relative_to(target).as_posix()
        snippets.append(f"--- {rel} ---\n{text[:budget]}")
        budget -= len(text)
        count += 1
    body = f"REPOSITORY TREE:\n{tree}\n\nKEY FILE CONTENTS:\n" + ("\n\n".join(snippets) or "(no files yet)")
    return body[:MAX_CONTEXT_CHARS + 2000]


def sanitize_relpath(raw: str) -> str:
    path = raw.strip().replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in path.split("/"):
        raise GitdripError(f"unsafe path from implementer: {raw}")
    return path


def implement_phase(client: LLMClient, phase: dict, context: str, errors: list[str] | None = None) -> dict:
    phase_json = json.dumps(phase, indent=2)
    if errors:
        system = REPAIR_SYSTEM
        prompt = (
            f"Phase that failed:\n{phase_json}\n\nRepository context:\n{context}\n\n"
            f"Validation errors:\n" + "\n".join(f"- {e[:1500]}" for e in errors)
        )
    else:
        system = IMPLEMENT_SYSTEM
        prompt = f"Phase to implement:\n{phase_json}\n\nRepository context:\n{context}"
    last_error: Exception = GitdripError("implementer not attempted")
    for attempt in range(3):
        try:
            reply = client.chat(prompt, system=system)
            data = extract_json(reply)
            files = data.get("files")
            if not isinstance(files, dict) or not files:
                raise GitdripError("implementer returned no files")
            clean = {sanitize_relpath(k): str(v) for k, v in files.items()}
            return {"files": clean, "reasoning": str(data.get("reasoning", ""))[:2000]}
        except Exception as exc:
            last_error = exc
            prompt = (
                f"{prompt}\n\nYour previous reply could not be used ({exc}). "
                "Reply again with ONLY the JSON object exactly as specified."
            )
    raise last_error


def write_files(target: Path, files: dict[str, str]) -> list[str]:
    written = []
    for rel, content in files.items():
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        written.append(rel)
    return written


UNSAFE_COMMAND = re.compile(
    r"\b(rm\s+-rf|mkfs|format\s+[a-z]:|shutdown|del\s+/[sf]|curl\b|wget\b|Invoke-WebRequest|iex\b|rm\s+-r\s+)",
    re.IGNORECASE,
)


def run_validations(target: Path, commands: list[str]) -> list[dict]:
    results = []
    for cmd in commands:
        if UNSAFE_COMMAND.search(cmd):
            results.append({"cmd": cmd, "ok": False, "output": "blocked by gitdrip safety filter"})
            continue
        try:
            proc = subprocess.run(
                cmd, cwd=str(target), shell=True, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=VALIDATION_TIMEOUT,
            )
            output = (proc.stdout + proc.stderr).strip()
            results.append({"cmd": cmd, "ok": proc.returncode == 0, "output": output[-4000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "ok": False, "output": f"timed out after {VALIDATION_TIMEOUT}s"})
        except Exception as exc:
            results.append({"cmd": cmd, "ok": False, "output": f"could not run: {exc}"})
    return results


def default_validations(written: list[str]) -> list[str]:
    if any(f.endswith(".py") for f in written):
        return ["python -m compileall -q ."]
    return []


def output_report(client: LLMClient | None, phase: dict, implemented: dict | None,
                  written: list[str], validation: list[dict], day: int) -> str:
    verdict_evidence = {
        "implemented": bool(written),
        "validation": validation,
        "implementer_reasoning": (implemented or {}).get("reasoning", ""),
        "llm_error": (implemented or {}).get("error", ""),
        "report_expectation": phase.get("report_expectation", ""),
    }
    if client is not None:
        try:
            reply = client.chat(
                f"Day: {day}\nPhase:\n{json.dumps(phase, indent=2)}\n\n"
                f"Evidence:\n{json.dumps(verdict_evidence, indent=2)}\n\n"
                f"Files now changed: {written}\n"
                f"Full validation output:\n{json.dumps(validation, indent=2)}",
                system=OUTPUT_SYSTEM,
            )
            if re.search(r"Verdict:\s*\w+", reply):
                return reply.strip()
        except Exception:
            pass
    all_ok = bool(written) and all(v["ok"] for v in validation)
    verdict = "CONFIRMED" if all_ok else ("BLOCKED" if not written else "NEEDS_REVIEW")
    file_lines = [f"- {f}" for f in written] or ["- (none)"]
    validation_lines = [f"- {'PASS' if v['ok'] else 'FAIL'}: `{v['cmd']}`" for v in validation] or ["- (no validation commands)"]
    impl_line = (implemented or {}).get("reasoning") or (implemented or {}).get("error") or "No implementation (offline report)"
    lines = [
        f"## Day {day} - {phase.get('phase', '')}",
        "### What was implemented",
        f"- {impl_line}",
        "### Files changed",
        *file_lines,
        "### Validation",
        *validation_lines,
        "### Verdict",
        f"Verdict: {verdict}",
        "### Notes for tomorrow",
        f"- Next day should continue from phase {day + 1}.",
    ]
    return "\n\n".join(lines)


def verdict_of(report_md: str, validation: list[dict], written: list[str]) -> str:
    match = re.search(r"Verdict:\s*\*{0,2}\s*(CONFIRMED|NEEDS_REVIEW|BLOCKED)", report_md, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if written and all(v["ok"] for v in validation):
        return "CONFIRMED"
    return "NEEDS_REVIEW" if written else "BLOCKED"


def run_day(project: Path, day: int | None = None, force: bool = False, cfg: Config | None = None) -> dict:
    cfg = cfg or load_config(project)
    plan = load_plan(project)
    if not plan:
        raise GitdripError("no plan yet - create one from the document first")
    state = load_state(project)
    total = len(plan["days"])
    day = day or state["days_done"] + 1
    if day > total:
        return {"status": "complete", "message": f"all {total} days are done"}
    if any(h.get("day") == day for h in state.get("history", [])) and not force:
        return {"status": "already_done", "message": f"day {day} already ran"}
    phase = plan["days"][day - 1]
    target = Path(cfg.target_repo).resolve()
    gitops.ensure_repo(target)

    settings = load_settings(project)
    secrets = load_secrets(project)
    client = client_for(project, settings, secrets)
    llm_ok = client.cfg.provider != "heuristic"
    context = build_repo_context(target)

    implemented: dict | None = None
    written: list[str] = []
    impl_error = ""
    if llm_ok:
        try:
            implemented = implement_phase(client, phase, context)
            written = write_files(target, implemented["files"])
        except Exception as exc:
            impl_error = str(exc)
    else:
        impl_error = "LLM disabled (heuristic mode)"

    commands = list(phase.get("validation") or []) or default_validations(written)
    validation = run_validations(target, commands) if commands else []

    if llm_ok and validation and not all(v["ok"] for v in validation):
        try:
            repair = implement_phase(
                client, phase, context,
                errors=[f"{v['cmd']}\n{v['output']}" for v in validation if not v["ok"]],
            )
            written += write_files(target, repair["files"])
            implemented = {**(implemented or {}), **repair, "reasoning": (repair.get("reasoning") or "")}
            validation = run_validations(target, commands)
        except Exception as exc:
            impl_error = (impl_error + " | " if impl_error else "") + f"repair failed: {exc}"

    if impl_error and implemented is None:
        implemented = {"files": {}, "reasoning": "", "error": impl_error}

    report_md = output_report(client if llm_ok else None, phase, implemented, written, validation, day)
    report_path = gitdrip_dir(project) / REPORTS_DIR / f"day-{day}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md + "\n", encoding="utf-8")

    state["days_done"] = max(state.get("days_done", 0), day)
    state.setdefault("history", []).append({
        "day": day,
        "phase": phase.get("phase", ""),
        "verdict": verdict_of(report_md, validation, written),
        "files": written,
        "provider": client.used or "none",
        "time": _now(),
        **({"error": impl_error} if impl_error else {}),
    })
    save_state(project, state)

    sha = ""
    commit_note = ""
    commit_paths = list(written)
    if project.resolve() == target:
        for extra in (PLAN_NAME, STATE_NAME, DOC_SNAPSHOT, f"{REPORTS_DIR}/day-{day}.md"):
            if (gitdrip_dir(project) / extra).is_file():
                commit_paths.append(f".gitdrip/{extra}")
    commit_paths = [p for p in dict.fromkeys(commit_paths)]
    if commit_paths:
        changed, sha_or_note = gitops.commit_paths(target, sorted(commit_paths), f"drip day {day}: {phase.get('phase','')}")
        if changed:
            sha = sha_or_note
            if gitops.git(target, "remote", "get-url", cfg.remote, check=False).returncode == 0:
                try:
                    gitops.push(target, cfg.remote, cfg.branch,
                                set_upstream=not gitops.has_upstream(target, cfg.remote, cfg.branch))
                    commit_note = "pushed"
                except GitdripError as exc:
                    commit_note = f"local commit only ({exc})"
            else:
                commit_note = "local commit (no remote)"
        else:
            commit_note = "no repository changes to commit"
    else:
        commit_note = "report only (outside target repo)"

    if sha:
        state["history"][-1]["sha"] = sha
        save_state(project, state)

    delivery = deliver_report(project, cfg, day, phase, report_md, target)
    return {
        "status": state["history"][-1]["verdict"].lower(),
        "day": day,
        "total": total,
        "phase": phase.get("phase", ""),
        "files": written,
        "validation": [{"cmd": v["cmd"], "ok": v["ok"]} for v in validation],
        "report": report_md,
        "report_path": str(report_path),
        "sha": sha,
        "commit": commit_note,
        "delivery": delivery,
        "provider": client.used or "none",
        "error": impl_error,
    }


def deliver_report(project: Path, cfg: Config, day: int, phase: dict, report_md: str, target: Path) -> str:
    from gitdrip.emailer import send_email, create_issue

    settings = load_settings(project)
    secrets = load_secrets(project)
    subject = f"[gitdrip] Day {day}: {phase.get('phase', '')}"
    if settings.get("email", {}).get("smtp_host") and secrets.get("smtp_password"):
        try:
            send_email(settings, secrets, subject, report_md)
            return "emailed"
        except Exception as exc:
            note = f"email failed: {exc}"
            issue = create_issue(target, subject, report_md)
            return f"{note}; {issue}"
    issue = create_issue(target, subject, report_md)
    return issue
