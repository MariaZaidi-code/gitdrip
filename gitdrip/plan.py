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
{"title": str, "summary": str, "days": [{
  "day": int, "phase": str, "reasoning": str, "goal": str,
  "tasks": [str], "files": [str],
  "validation": [str], "report_expectation": str}]}

Rules:
- Exactly the requested number of days, day numbered 1..N in execution order.
- reasoning: why this phase comes now and what it unlocks for later phases (2-3 sentences).
- tasks: 2-6 concrete, independently verifiable tasks.
- files: concrete repository paths this phase creates or modifies.
- validation: shell commands that verify the phase, runnable offline in the repo
  (prefer "python -m compileall -q ." for Python; keep each under 120 seconds; never use the network).
- report_expectation: one sentence describing what the daily report must confirm.
- Each day must leave the repository in a coherent, committable state.
- First day: project scaffolding/essentials. Last day: polish, docs, final validation."""


def plan_path(project: Path) -> Path:
    return gitdrip_dir(project) / PLAN_NAME


def state_path(project: Path) -> Path:
    return gitdrip_dir(project) / STATE_NAME


def load_plan(project: Path) -> dict | None:
    path = plan_path(project)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_plan(project: Path, plan: dict) -> None:
    path = plan_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(project: Path) -> dict:
    path = state_path(project)
    if not path.is_file():
        return {"days_done": 0, "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


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

    return {
        "day": index,
        "phase": str(raw.get("phase") or f"Phase {index}")[:200],
        "reasoning": str(raw.get("reasoning") or "")[:2000],
        "goal": str(raw.get("goal") or raw.get("phase") or "")[:500],
        "tasks": [t[:300] for t in as_list(raw.get("tasks"))][:8],
        "files": [f[:200] for f in as_list(raw.get("files"))][:30],
        "validation": [v[:300] for v in as_list(raw.get("validation"))][:6],
        "report_expectation": str(raw.get("report_expectation") or "")[:500],
    }


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
        })
    return {
        "version": 1,
        "title": "Project plan (offline structural planner)",
        "summary": "LLM unavailable; plan derived from document headings and structure.",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "provider": "heuristic",
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
    save_plan(project, plan)
    return plan
