from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from collections.abc import Callable
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

MAX_FILES_PER_ROUND = 3
MAX_ROUNDS = 8

IMPLEMENT_SYSTEM = """You are the Implementer Agent in an automated daily-build system.
Implement exactly the requested phase of the plan in the given repository.

Respond with ONLY a JSON object, no prose, no markdown fence:
{"files": {"relative/path.ext": "FULL file content"}, "reasoning": "short explanation", "done": false}

Rules:
- Return complete file contents only: never diffs, never placeholders, never "...".
- Return AT MOST 3 files per reply. If the phase needs more files, set "done" to false
  and I will ask for the next batch. When nothing more is needed, reply with
  {"files": {}, "reasoning": "...", "done": true}.
- Only create or modify files required by this phase.
- Keep each file focused and reasonably sized.
- Match the existing project's style, imports and structure from the repository context.
- Use forward slashes in paths, no absolute paths, no "..".
- If the phase cannot be implemented as specified, return the closest safe implementation
  and explain the limitation in "reasoning"."""

REPAIR_SYSTEM = """You are the Implementer Agent performing a repair round.
Your previous files failed validation. Fix ONLY the reported problems and return the
full corrected content of the affected files.

Respond with ONLY a JSON object, no prose, no markdown fence:
{"files": {"relative/path.ext": "FULL corrected file content"}, "reasoning": "what you fixed", "done": true}
Return AT MOST 3 files per reply; set "done" to false if more rounds are needed."""

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


def _alog(project: Path, event: str, day: int | None, detail: str = "") -> None:
    try:
        path = gitdrip_dir(project) / "agent-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"t": _now(), "event": event, "day": day, "detail": str(detail)[:500]}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _commit_message(day: int, phase_text: str) -> str:
    ctype = gitops.commit_type_for(phase_text or "")
    lowered = phase_text[:1].lower() + phase_text[1:] if phase_text else ""
    return f"{ctype}(day {day}): {lowered}"


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


def _chat_json(client: LLMClient, prompt: str, system: str) -> tuple[dict, str]:
    """One JSON-returning round with parse retries. Returns (data, last_note)."""
    last_error: Exception = GitdripError("implementer not attempted")
    for _ in range(3):
        try:
            data = extract_json(client.chat(prompt, system=system))
            if not isinstance(data, dict):
                raise GitdripError("implementer reply is not a JSON object")
            return data, ""
        except Exception as exc:
            last_error = exc
            prompt = (
                f"{prompt}\n\nYour previous reply could not be used ({exc}). "
                "Reply again with ONLY the JSON object exactly as specified."
            )
    raise last_error


def implement_phase(client: LLMClient, phase: dict, context: str,
                    errors: list[str] | None = None,
                    on_round: "Callable[[dict[str, str]], str | None] | None" = None) -> dict:
    """Implement a phase in small file batches (fits small-model output budgets).

    on_round, when given, receives each accepted batch and may return a
    refreshed repository context for the next round.
    """
    phase_json = json.dumps(phase, indent=2)
    if errors:
        system = REPAIR_SYSTEM
        base_prompt = (
            f"Phase that failed:\n{phase_json}\n\nRepository context:\n{context}\n\n"
            f"Validation errors:\n" + "\n".join(f"- {e[:1500]}" for e in errors)
        )
    else:
        system = IMPLEMENT_SYSTEM
        base_prompt = f"Phase to implement:\n{phase_json}\n\nRepository context:\n{context}"
    produced: dict[str, str] = {}
    reasoning: list[str] = []
    prompt = base_prompt
    round_error: Exception | None = None
    small_mode = False
    for round_no in range(1, MAX_ROUNDS + 1):
        try:
            data, _ = _chat_json(client, prompt, system)
            small_mode = False
        except Exception as exc:
            round_error = exc
            if "unterminated" in str(exc) and not small_mode:
                # Truncated reply: the output budget was exceeded. Retry the
                # same round asking for a single short file.
                small_mode = True
                prompt = (
                    f"{base_prompt}\n\nIMPORTANT: your previous reply was cut off. "
                    "Return ONLY 1 file (the single most important one for this phase), "
                    "keep it SHORT (under 80 lines). "
                    "Reply with {\"files\": {\"path\": \"content\"}, \"reasoning\": \"...\", \"done\": false} "
                    "if more files remain after this one."
                )
                try:
                    data, _ = _chat_json(client, prompt, system)
                except Exception as exc2:
                    round_error = exc2
                    break
            else:
                break
        raw_files = data.get("files")
        if raw_files and not isinstance(raw_files, dict):
            round_error = GitdripError("implementer returned files in an unexpected shape")
            break
        batch: dict[str, str] = {}
        for key, value in list((raw_files or {}).items())[:MAX_FILES_PER_ROUND]:
            batch[sanitize_relpath(key)] = str(value)
        produced.update(batch)
        if data.get("reasoning"):
            reasoning.append(str(data["reasoning"])[:800])
        if batch and on_round is not None:
            try:
                refreshed = on_round(batch)
                if refreshed:
                    context = refreshed
            except Exception as exc:
                round_error = exc
                break
        if data.get("done") or not raw_files:
            break
        remaining = sorted(set(produced))
        prompt = (
            f"Phase (continued):\n{phase_json}\n\nRepository context:\n{context}\n\n"
            f"Files already produced this run: {remaining or '(none yet)'}.\n"
            f"Produce the NEXT batch (at most {MAX_FILES_PER_ROUND} files not yet produced), "
            "or reply with {\"files\": {}, \"reasoning\": \"...\", \"done\": true} "
            "if the phase is fully covered."
        )
    if not produced:
        raise round_error or GitdripError("implementer returned no files")
    return {"files": produced, "reasoning": " | ".join(reasoning)[:2000]}


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


def _report_footer(client: LLMClient | None, day: int) -> str:
    provider = (client.used or "none") if client else "none"
    footer = f"\n\n---\n_gitdrip day {day} - provider: {provider}"
    try:
        attempts = list(client.attempts) if client else []
    except Exception:
        attempts = []
    if attempts:
        footer += f" - fallbacks: {(' ; '.join(attempts))[:600]}"
    footer += "_"
    return footer


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
                return reply.strip() + _report_footer(client, day)
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
    return "\n\n".join(lines) + _report_footer(client, day)


def verdict_of(report_md: str, validation: list[dict], written: list[str]) -> str:
    match = re.search(r"Verdict:\s*\*{0,2}\s*(CONFIRMED|NEEDS_REVIEW|BLOCKED)", report_md, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if written and all(v["ok"] for v in validation):
        return "CONFIRMED"
    return "NEEDS_REVIEW" if written else "BLOCKED"


def run_day(project: Path, day: int | None = None, force: bool = False, cfg: Config | None = None) -> dict:
    try:
        _early_settings = load_settings(project)
    except Exception:
        _early_settings = {}
    if _early_settings.get("paused"):
        _paused_day = day
        if _paused_day is None:
            try:
                _paused_day = load_state(project).get("days_done", 0) + 1
            except Exception:
                _paused_day = 1
        _alog(project, "paused", _paused_day, "project is paused")
        return {"status": "paused", "day": _paused_day, "message": "project is paused (gitdrip resume to continue)"}
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

    settings = _early_settings
    secrets = load_secrets(project)
    client = client_for(project, settings, secrets)
    llm_ok = client.cfg.provider != "heuristic"
    _alog(project, "run_start", day, phase.get("phase", ""))

    def _refresh_context() -> str:
        base = build_repo_context(target)
        try:
            from gitdrip.memory import memory_text
            mem = memory_text(project)
        except Exception:
            mem = ""
        return base + ("\n\nPROJECT MEMORY:\n" + mem if mem else "")

    context = _refresh_context()

    implemented: dict | None = None
    written: list[str] = []
    impl_error = ""

    def _apply_round(batch: dict[str, str]) -> str:
        for rel in write_files(target, batch):
            if rel not in written:
                written.append(rel)
        _alog(project, "round_complete", day, f"{len(batch)} file(s)")
        return _refresh_context()

    if llm_ok:
        try:
            implemented = implement_phase(client, phase, context, on_round=_apply_round)
            _alog(project, "implemented", day, f"{len(written)} file(s)")
        except Exception as exc:
            impl_error = str(exc)
            _alog(project, "error", day, impl_error[:500])
    else:
        impl_error = "LLM disabled (heuristic mode)"
        _alog(project, "implemented", day, impl_error)

    commands = list(phase.get("validation") or []) or default_validations(written)
    validation = run_validations(target, commands) if commands else []
    _alog(project, "validation", day, f"{sum(1 for v in validation if v['ok'])}/{len(validation)} passed")

    if llm_ok and validation and not all(v["ok"] for v in validation):
        try:
            repair = implement_phase(
                client, phase, _refresh_context(),
                errors=[f"{v['cmd']}\n{v['output']}" for v in validation if not v["ok"]],
                on_round=_apply_round,
            )
            implemented = {**(implemented or {}), **repair, "reasoning": (repair.get("reasoning") or "")}
            validation = run_validations(target, commands)
            _alog(project, "repair", day, "repair attempted")
        except Exception as exc:
            impl_error = (impl_error + " | " if impl_error else "") + f"repair failed: {exc}"
            _alog(project, "repair", day, f"repair failed: {exc}"[:500])
    if impl_error:
        _alog(project, "error", day, impl_error[:500])

    if impl_error and implemented is None:
        implemented = {"files": {}, "reasoning": "", "error": impl_error}

    # BLOCKED-DAY PATH: error and nothing written -> report + notify, no state/commit.
    if impl_error and not written:
        report_md = output_report(client if llm_ok else None, phase, implemented, written, validation, day)
        report_path = gitdrip_dir(project) / REPORTS_DIR / f"day-{day}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md + "\n", encoding="utf-8")
        _alog(project, "report", day, str(report_path))
        delivery = deliver_report(project, cfg, day, phase, report_md, target)
        _alog(project, "delivery", day, delivery[:500])
        _alog(project, "blocked", day, impl_error[:500])
        try:
            from gitdrip.memory import record_day as _record_day_blocked
            _record_day_blocked(project, day, phase.get("phase", ""), phase.get("reqs"),
                                [], validation, "BLOCKED", impl_error)
        except Exception:
            pass
        return {
            "status": "blocked",
            "day": day,
            "total": total,
            "phase": phase.get("phase", ""),
            "files": [],
            "validation": [{"cmd": v["cmd"], "ok": v["ok"]} for v in validation],
            "report": report_md,
            "report_path": str(report_path),
            "sha": "",
            "commit": "not committed (blocked)",
            "delivery": delivery,
            "provider": client.used or "none",
            "error": impl_error,
            **({"attempts": client.attempts} if client.attempts else {}),
        }

    # REVIEW MODE: report as normal, then stash for manual approval.
    if settings.get("mode") == "review":
        if state.get("pending_approval"):
            raise GitdripError("pending approval already exists - approve or clear it first")
        if any(h.get("day") == day for h in state.get("history", [])):
            raise GitdripError(f"day {day} already ran")
        report_md = output_report(client if llm_ok else None, phase, implemented, written, validation, day)
        report_path = gitdrip_dir(project) / REPORTS_DIR / f"day-{day}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md + "\n", encoding="utf-8")
        _alog(project, "report", day, str(report_path))
        state["pending_approval"] = {
            "day": day,
            "phase": phase.get("phase", ""),
            "files": written,
            "report_path": str(report_path),
            "validation": [{"cmd": v["cmd"], "ok": v["ok"]} for v in validation],
            "time": _now(),
        }
        save_state(project, state)
        _alog(project, "needs_approval", day, f"{len(written)} file(s) awaiting approval")
        delivery = deliver_report(
            project, cfg, day, phase,
            report_md + "\n\n> Approval needed: review the report and run approve to commit these changes.",
            target,
        )
        _alog(project, "delivery", day, delivery[:500])
        return {
            "status": "needs_approval",
            "day": day,
            "total": total,
            "phase": phase.get("phase", ""),
            "files": written,
            "validation": [{"cmd": v["cmd"], "ok": v["ok"]} for v in validation],
            "report": report_md,
            "report_path": str(report_path),
            "sha": "",
            "commit": "awaiting approval (review mode)",
            "delivery": delivery,
            "provider": client.used or "none",
            "error": impl_error,
            **({"attempts": client.attempts} if client.attempts else {}),
        }

    report_md = output_report(client if llm_ok else None, phase, implemented, written, validation, day)
    report_path = gitdrip_dir(project) / REPORTS_DIR / f"day-{day}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md + "\n", encoding="utf-8")
    _alog(project, "report", day, str(report_path))

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
    try:
        from gitdrip.memory import record_day as _record_day
        _record_day(project, day, phase.get("phase", ""), phase.get("reqs"),
                    written, validation, state["history"][-1]["verdict"], impl_error)
    except Exception:
        pass

    sha = ""
    commit_note = ""
    commit_paths = list(written)
    if project.resolve() == target:
        for extra in (PLAN_NAME, STATE_NAME, DOC_SNAPSHOT, "memory.json", f"{REPORTS_DIR}/day-{day}.md"):
            if (gitdrip_dir(project) / extra).is_file():
                commit_paths.append(f".gitdrip/{extra}")
    commit_paths = [p for p in dict.fromkeys(commit_paths)]
    if commit_paths:
        changed, sha_or_note = gitops.commit_paths(target, sorted(commit_paths), _commit_message(day, phase.get('phase','')))
        _alog(project, "commit", day, sha_or_note[:500])
        if changed:
            sha = sha_or_note
            if gitops.git(target, "remote", "get-url", cfg.remote, check=False).returncode == 0:
                try:
                    gitops.push(target, cfg.remote, cfg.branch,
                                set_upstream=not gitops.has_upstream(target, cfg.remote, cfg.branch))
                    commit_note = "pushed"
                    _alog(project, "push", day, commit_note)
                except GitdripError as exc:
                    commit_note = f"local commit only ({exc})"
                    _alog(project, "push", day, commit_note[:500])
            else:
                commit_note = "local commit (no remote)"
                _alog(project, "push", day, commit_note)
        else:
            commit_note = "no repository changes to commit"
    else:
        commit_note = "report only (outside target repo)"

    if sha:
        state["history"][-1]["sha"] = sha
        save_state(project, state)

    delivery = deliver_report(project, cfg, day, phase, report_md, target)
    _alog(project, "delivery", day, delivery[:500])
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
        **({"attempts": client.attempts} if client.attempts else {}),
    }


def approve_pending(project: Path) -> dict:
    state = load_state(project)
    pending = state.get("pending_approval")
    if not pending:
        raise GitdripError("no pending approval")
    day = pending["day"]
    if any(h.get("day") == day for h in state.get("history", [])):
        raise GitdripError(f"day {day} already ran")
    cfg = load_config(project)
    target = Path(cfg.target_repo).resolve()
    gitops.ensure_repo(target)
    phase_text = pending.get("phase", "")
    written = list(pending.get("files", []))
    commit_paths = list(written)
    if project.resolve() == target:
        for extra in (PLAN_NAME, STATE_NAME, DOC_SNAPSHOT, "memory.json", f"{REPORTS_DIR}/day-{day}.md"):
            if (gitdrip_dir(project) / extra).is_file():
                commit_paths.append(f".gitdrip/{extra}")
    commit_paths = [p for p in dict.fromkeys(commit_paths)]
    sha = ""
    commit_note = ""
    if commit_paths:
        changed, sha_or_note = gitops.commit_paths(target, sorted(commit_paths), _commit_message(day, phase_text))
        _alog(project, "commit", day, sha_or_note[:500])
        if changed:
            sha = sha_or_note
            if gitops.git(target, "remote", "get-url", cfg.remote, check=False).returncode == 0:
                try:
                    gitops.push(target, cfg.remote, cfg.branch,
                                set_upstream=not gitops.has_upstream(target, cfg.remote, cfg.branch))
                    commit_note = "pushed"
                    _alog(project, "push", day, commit_note)
                except GitdripError as exc:
                    commit_note = f"local commit only ({exc})"
                    _alog(project, "push", day, commit_note[:500])
            else:
                commit_note = "local commit (no remote)"
                _alog(project, "push", day, commit_note)
        else:
            commit_note = "no repository changes to commit"
    else:
        commit_note = "report only (outside target repo)"
    try:
        saved_report = Path(pending.get("report_path", "")).read_text(encoding="utf-8") if pending.get("report_path") else ""
    except OSError:
        saved_report = ""
    saved_validation = pending.get("validation", []) or []
    verdict = verdict_of(saved_report, saved_validation, written)
    state["days_done"] = max(state.get("days_done", 0), day)
    state.setdefault("history", []).append({
        "day": day,
        "phase": phase_text,
        "verdict": verdict,
        "files": written,
        "provider": "manual-approval",
        "time": _now(),
        "sha": sha,
    })
    state.pop("pending_approval", None)
    save_state(project, state)
    try:
        from gitdrip.memory import record_day as _record_day_approve
        plan_reqs = [d.get("reqs", []) for d in (load_plan(project) or {}).get("days", []) if d.get("day") == day]
        _record_day_approve(project, day, phase_text, plan_reqs[0] if plan_reqs else None,
                            written, saved_validation, verdict, "")
    except Exception:
        pass
    _alog(project, "approved", day, sha or commit_note[:500])
    return {"status": "approved", "day": day, "sha": sha, "commit": commit_note}


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
