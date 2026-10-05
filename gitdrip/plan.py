from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from gitdrip.config import GitdripError, gitdrip_dir
from gitdrip.docparse import truncate
from gitdrip.llm import LLMClient, extract_json

PLAN_NAME = "plan.json"
STATE_NAME = "plan_state.json"
DOC_SNAPSHOT = "project-doc.md"

PLANNER_SYSTEM = """You are the Planner Agent in an automated daily-build system.
Read the project document and produce a day-by-day implementation plan.
Respond with ONLY a JSON object, no prose, no markdown fence.

Schema:
{"title": str, "summary": str,
 "requirements": [{"id": "REQ-001", "text": str, "type": "functional|technical|non-functional"}],
 "architecture": {"stack": [str], "decisions": [str with brief rationale]},
 "days": [{
   "day": int, "phase": str, "reasoning": str, "goal": str,
   "tasks": [str], "files": [str],
   "validation": [str], "report_expectation": str,
   "reqs": ["REQ-001", ...]}]}

Rules:
- Exactly the requested number of days, day numbered 1..N in execution order.
- reasoning: why this phase comes now and what it unlocks for later phases (2-3 sentences).
- tasks: 2-6 concrete, independently verifiable tasks.
- files: concrete repository paths this phase creates or modifies.
- validation: shell commands that verify the phase, runnable offline in the repo
  (prefer "python -m compileall -q ." for Python; keep each under 120 seconds; never use the network).
- report_expectation: one sentence describing what the daily report must confirm.
- Each day must leave the repository in a coherent, committable state.
- First day: project scaffolding/essentials. Last day: polish, docs, final validation.
- requirements: numbered REQ-001, REQ-002, ... covering the whole document;
  each has "id", "text" (one verifiable requirement), "type" which must be one of
  "functional", "technical" or "non-functional".
- architecture: "stack" lists the key technologies, "decisions" lists architecture
  decisions each with a brief rationale.
- each day: "reqs" lists the requirement ids (e.g. ["REQ-001"]) that day covers;
  every requirement must be covered by at least one day."""


def plan_path(project: Path) -> Path:
    return gitdrip_dir(project) / PLAN_NAME


def state_path(project: Path) -> Path:
    return gitdrip_dir(project) / STATE_NAME


def load_plan(project: Path) -> dict | None:
    path = plan_path(project)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_plan(project: Path, plan: dict) -> None:
    path = plan_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(project: Path) -> dict:
    path = state_path(project)
    if not path.is_file():
        return {"days_done": 0, "history": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(project: Path, state: dict) -> None:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def doc_snapshot_text(project: Path) -> str:
    path = gitdrip_dir(project) / DOC_SNAPSHOT
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def save_doc_snapshot(project: Path, text: str) -> None:
    path = gitdrip_dir(project) / DOC_SNAPSHOT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _norm_day(raw: dict, index: int) -> dict:
    def as_list(value) -> list[str]:
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value or []]

    def as_reqs(value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        out: list[str] = []
        for v in value or []:
            rid = str(v).strip().upper()[:20]
            if rid:
                out.append(rid)
        return out[:20]

    return {
        "day": index,
        "phase": str(raw.get("phase") or f"Phase {index}")[:200],
        "reasoning": str(raw.get("reasoning") or "")[:2000],
        "goal": str(raw.get("goal") or raw.get("phase") or "")[:500],
        "tasks": [t[:300] for t in as_list(raw.get("tasks"))][:8],
        "files": [f[:200] for f in as_list(raw.get("files"))][:30],
        "validation": [v[:300] for v in as_list(raw.get("validation"))][:6],
        "report_expectation": str(raw.get("report_expectation") or "")[:500],
        "reqs": as_reqs(raw.get("reqs")),
    }


REQ_TYPES = {"functional", "technical", "non-functional"}


def _norm_requirements(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(raw[:100], start=1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or f"REQ-{i:03d}").strip().upper()[:20]
        if not re.match(r"^REQ-\d{3,}$", rid):
            rid = f"REQ-{i:03d}"
        rtype = str(item.get("type") or "functional").strip().lower()[:20]
        if rtype not in REQ_TYPES:
            rtype = "functional"
        out.append({
            "id": rid,
            "text": str(item.get("text") or "")[:500],
            "type": rtype,
        })
    return out


def _norm_architecture(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    stack = raw.get("stack")
    decisions = raw.get("decisions")
    if stack is None and decisions is None:
        return dict(raw) if raw else {}
    norm: dict = {}
    if isinstance(stack, str):
        stack = [stack]
    norm["stack"] = [str(s)[:200] for s in (stack or [])][:20]
    if isinstance(decisions, str):
        decisions = [decisions]
    norm["decisions"] = [str(d)[:500] for d in (decisions or [])][:20]
    return norm


def _llm_plan(client: LLMClient, doc: str, days: int) -> dict:
    prompt = (
        f"Create a plan with exactly {days} days for the following project document.\n\n"
        f"--- DOCUMENT START ---\n{doc}\n--- DOCUMENT END ---"
    )
    reply = client.chat(prompt, system=PLANNER_SYSTEM)
    data = extract_json(reply)
    raw_days = data.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        raise GitdripError("planner returned no days")
    if len(raw_days) > days:
        raw_days = raw_days[:days]
    plan = {
        "version": 1,
        "title": str(data.get("title") or "Project plan")[:200],
        "summary": str(data.get("summary") or "")[:1500],
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "provider": client.used,
        "requirements": _norm_requirements(data.get("requirements")),
        "architecture": _norm_architecture(data.get("architecture")),
        "days": [_norm_day(day, i) for i, day in enumerate(raw_days, start=1)],
    }
    return plan


def heuristic_plan(doc: str, days: int) -> dict:
    headings = re.findall(r"^(?:#{1,4}\s+|\d+[.)]\s+|\*\s+)(.+)$", doc, re.MULTILINE)
    sections = [h.strip()[:120] for h in headings if len(h.strip()) > 3]
    if not sections:
        sections = [s.strip()[:120] for s in re.split(r"\n\s*\n", doc) if len(s.strip()) > 40][:days * 2]
    if not sections:
        sections = ["Project foundation", "Core implementation", "Finalization"][:days]
    chunks: list[list[str]] = [[] for _ in range(days)]
    for i, section in enumerate(sections):
        chunks[i % days].append(section)
    order = []
    for i, group in enumerate(chunks, start=1):
        order.append(group if group else [f"Continue phase {i}"])
    plan_days = []
    for i, group in enumerate(order, start=1):
        plan_days.append({
            "day": i,
            "phase": f"Phase {i}: {group[0]}"[:200],
            "reasoning": (
                "Generated offline by the structural planner: document section(s) "
                f"{', '.join(group)} scheduled for day {i} to keep each day's scope "
                "small and independently committable."
            )[:2000],
            "goal": f"Complete: {', '.join(group)}"[:500],
            "tasks": [f"Implement: {g}" for g in group][:8],
            "files": [],
            "validation": ["python -m compileall -q ."] if ".py" in doc or "python" in doc.lower() else ["git status --short"],
            "report_expectation": "Confirm the scheduled document sections were implemented and validated.",
            "reqs": [],
        })
    return {
        "version": 1,
        "title": "Project plan (offline structural planner)",
        "summary": "LLM unavailable; plan derived from document headings and structure.",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "provider": "heuristic",
        "requirements": [],
        "architecture": {},
        "days": plan_days,
    }


def make_plan(project: Path, client: LLMClient, days: int, doc: str | None = None) -> dict:
    if days < 1 or days > 90:
        raise GitdripError("days must be between 1 and 90")
    text = doc if doc is not None else doc_snapshot_text(project)
    if not text.strip():
        raise GitdripError("no project document yet - upload one first")
    text = truncate(text)
    if client.cfg.provider == "heuristic":
        plan = heuristic_plan(text, days)
    else:
        try:
            plan = _llm_plan(client, text, days)
        except Exception:
            plan = heuristic_plan(text, days)
            plan["warning"] = "LLM planning failed; structural fallback used. Attempts: " + " | ".join(client.attempts)
    plan.setdefault("requirements", [])
    if not isinstance(plan.get("requirements"), list):
        plan["requirements"] = []
    plan.setdefault("architecture", {})
    if not isinstance(plan.get("architecture"), dict):
        plan["architecture"] = {}
    for day in plan.get("days", []):
        if isinstance(day, dict):
            day.setdefault("reqs", [])
            if not isinstance(day.get("reqs"), list):
                day["reqs"] = []
    if client.attempts:
        plan["attempts"] = list(client.attempts)
    save_plan(project, plan)
    return plan


def replan_remaining(project: Path, client: LLMClient, days: int | None = None, note: str = "") -> dict:
    """Revise the plan for the remaining (not yet executed) days.

    Completed days are preserved untouched; future days are replaced by a fresh
    LLM plan built from the current plan + project memory + state progress.
    On LLM failure raises GitdripError (no silent fallback).
    """
    from gitdrip.docparse import truncate as _truncate

    plan = load_plan(project)
    if not plan:
        raise GitdripError("no plan yet - create one from the document first")
    state = load_state(project)
    try:
        days_done = int(state.get("days_done", 0))
    except (TypeError, ValueError):
        days_done = 0
    total = len(plan.get("days", []))
    remaining = total - days_done
    if remaining <= 0:
        raise GitdripError("no remaining days to replan (all days are done)")
    wanted = remaining if days is None else days
    if wanted < 1 or wanted > 90:
        raise GitdripError("days must be between 1 and 90")

    try:
        from gitdrip.memory import memory_text as _memory_text
        memory = _memory_text(project)
    except Exception:
        memory = "(project memory unavailable)"
    history = state.get("history", [])
    progress_lines = [
        f"days_done={days_done}/{total}",
        f"history={json.dumps(history)[-4000:]}" if history else "history=(none)",
    ]
    doc = _truncate(doc_snapshot_text(project) or "", 8000)
    prompt = (
        f"Revise the plan for the REMAINING {wanted} days only "
        f"(days {days_done + 1}..{days_done + wanted}; {days_done} day(s) already completed and immutable).\n\n"
        f"CURRENT PLAN:\n{json.dumps(plan, indent=2)[:12000]}\n\n"
        f"PROJECT MEMORY:\n{memory[:6000]}\n\n"
        f"PROGRESS:\n" + "\n".join(progress_lines) + "\n\n"
        + (f"USER NOTE:\n{note[:2000]}\n\n" if note else "")
        + (f"ORIGINAL DOCUMENT (excerpt):\n{doc}\n\n" if doc else "")
        + f"Respond with the same JSON schema as the planner (title, summary, requirements, "
        f"architecture, days) with exactly {wanted} days for the remaining work, "
        f"preserving requirement ids (REQ-001, ...) where possible and covering "
        f"unfinished requirements via each day's reqs."
    )
    try:
        reply = client.chat(prompt, system=PLANNER_SYSTEM)
        data = extract_json(reply)
        raw_days = data.get("days")
        if not isinstance(raw_days, list) or not raw_days:
            raise GitdripError("planner returned no days")
        if len(raw_days) > wanted:
            raw_days = raw_days[:wanted]
        new_days = [_norm_day(day, i) for i, day in enumerate(raw_days, start=days_done + 1)]
        requirements = _norm_requirements(data.get("requirements"))
        if not requirements and isinstance(plan.get("requirements"), list):
            requirements = plan["requirements"]
        architecture = _norm_architecture(data.get("architecture"))
        if not architecture and isinstance(plan.get("architecture"), dict):
            architecture = plan["architecture"]
        merged = {
            "version": plan.get("version", 1),
            "title": str(data.get("title") or plan.get("title") or "Project plan")[:200],
            "summary": str(data.get("summary") or plan.get("summary") or "")[:1500],
            "created": plan.get("created", dt.datetime.now().isoformat(timespec="seconds")),
            "replanned": dt.datetime.now().isoformat(timespec="seconds"),
            "replan_note": note[:2000] if note else "",
            "provider": client.used or plan.get("provider", ""),
            "requirements": requirements,
            "architecture": architecture,
            "days": list(plan.get("days", [])[:days_done]) + new_days,
        }
        if client.attempts:
            merged["attempts"] = list(client.attempts)
        save_plan(project, merged)
        return merged
    except GitdripError:
        raise
    except Exception as exc:
        raise GitdripError(f"replan failed: {exc}") from exc
