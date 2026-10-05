from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gitdrip.config import gitdrip_dir

MEMORY_NAME = "memory.json"
MEMORY_CAP = 3000


def memory_path(project: Path) -> Path:
    return gitdrip_dir(project) / MEMORY_NAME


def _defaults() -> dict:
    return {
        "requirements": {},
        "architecture": {},
        "tech_stack": [],
        "completed_days": [],
        "pending_days": [],
        "known_issues": [],
        "decisions": [],
        "last_test_results": [],
    }


def load_memory(project: Path) -> dict:
    base = _defaults()
    try:
        path = memory_path(project)
        if not path.is_file():
            return base
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return base
        for key, default in base.items():
            value = data.get(key, default)
            if isinstance(default, dict):
                base[key] = value if isinstance(value, dict) else {}
            elif isinstance(default, list):
                base[key] = value if isinstance(value, list) else []
            else:
                base[key] = value
        return base
    except Exception:
        return _defaults()


def save_memory(project: Path, memory: dict) -> None:
    try:
        path = memory_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path):
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def memory_text(project: Path) -> str:
    """Compact prompt-ready rendering of project memory (capped at ~3000 chars)."""
    try:
        mem = load_memory(project)
        plan = _read_json(gitdrip_dir(project) / "plan.json") or {}
        plan_reqs = plan.get("requirements") if isinstance(plan, dict) else None
        if not isinstance(plan_reqs, list):
            plan_reqs = []

        mem_reqs = mem.get("requirements") if isinstance(mem.get("requirements"), dict) else {}
        lines: list[str] = []
        total = len(plan_reqs) if plan_reqs else len(mem_reqs)
        lines.append(f"Project memory ({total} requirements):")

        if plan_reqs:
            for req in plan_reqs[:50]:
                if not isinstance(req, dict):
                    continue
                rid = str(req.get("id", "?"))
                status = str((mem_reqs.get(rid) or {}).get("status", "open"))
                text = str(req.get("text", ""))[:120]
                rtype = str(req.get("type", ""))
                lines.append(f"- {rid} [{status}/{rtype}]: {text}")
            for rid, info in list(mem_reqs.items())[:20]:
                if any(isinstance(r, dict) and r.get("id") == rid for r in plan_reqs):
                    continue
                info = info if isinstance(info, dict) else {}
                lines.append(f"- {rid} [{info.get('status', 'open')}]: {str(info.get('text', ''))[:120]}")
        elif mem_reqs:
            for rid, info in list(mem_reqs.items())[:50]:
                info = info if isinstance(info, dict) else {}
                lines.append(f"- {rid} [{info.get('status', 'open')}]: {str(info.get('text', ''))[:120]}")
        else:
            lines.append("- (no requirements tracked yet)")

        arch = mem.get("architecture") if isinstance(mem.get("architecture"), dict) else {}
        if not arch and isinstance(plan.get("architecture"), dict):
            arch = plan["architecture"]
        stack = mem.get("tech_stack") if isinstance(mem.get("tech_stack"), list) else []
        if not stack and isinstance(arch, dict) and isinstance(arch.get("stack"), list):
            stack = arch["stack"]
        decisions = list(arch.get("decisions", []) if isinstance(arch, dict) else [])[:10]
        if not decisions and isinstance(mem.get("decisions"), list):
            decisions = [str(d)[:200] if not isinstance(d, dict) else str(d.get("text", d))[:200]
                         for d in mem["decisions"][:10]]
        lines.append(f"Tech stack: {', '.join(str(s)[:60] for s in stack[:10]) or '(unknown)'}")
        if decisions:
            lines.append("Architecture decisions:")
            for d in decisions:
                lines.append(f"- {str(d)[:200]}")
        else:
            lines.append("Architecture decisions: (none recorded)")

        completed = mem.get("completed_days") if isinstance(mem.get("completed_days"), list) else []
        pending = mem.get("pending_days") if isinstance(mem.get("pending_days"), list) else []
        compacts = []
        for c in completed[-15:]:
            if isinstance(c, dict):
                compacts.append(f"day {c.get('day')}:{c.get('phase', '')} [{c.get('verdict', '')}]")
            else:
                compacts.append(f"day {c}")
        lines.append(f"Completed ({len(completed)}): {'; '.join(compacts) or '(none)'}")
        lines.append(f"Pending ({len(pending)}): {', '.join(str(p)[:80] for p in pending[:15]) or '(none)'}")

        issues = mem.get("known_issues") if isinstance(mem.get("known_issues"), list) else []
        lines.append(f"Known issues ({len(issues)}):")
        if issues:
            for issue in issues[-10:]:
                if isinstance(issue, dict):
                    lines.append(
                        f"- day {issue.get('day', '?')} [{issue.get('verdict', '')}]: "
                        f"{str(issue.get('error', ''))[:200]}"
                    )
                else:
                    lines.append(f"- {str(issue)[:200]}")
        else:
            lines.append("- (none)")

        tests = mem.get("last_test_results") if isinstance(mem.get("last_test_results"), list) else []
        lines.append(f"Last test results ({len(tests)}):")
        if tests:
            for t in tests[-10:]:
                if isinstance(t, dict):
                    lines.append(f"- {'PASS' if t.get('ok') else 'FAIL'}: {str(t.get('cmd', ''))[:150]}")
                else:
                    lines.append(f"- {str(t)[:150]}")
        else:
            lines.append("- (no test results yet)")

        text = "\n".join(lines)
        if len(text) > MEMORY_CAP:
            text = text[:MEMORY_CAP] + "\n...[memory truncated]..."
        return text
    except Exception:
        return "Project memory: (unavailable)"


def record_day(
    project: Path,
    day: int,
    phase: str = "",
    reqs=None,
    written=None,
    validation=None,
    verdict: str = "",
    error: str | None = "",
) -> dict:
    """Update project memory after a day's run; never raises on file IO."""
    try:
        mem = load_memory(project)
    except Exception:
        mem = _defaults()
    try:
        reqs = list(reqs or [])
        written = list(written or [])
        validation = list(validation or [])
        verdict_norm = str(verdict or "").upper()
        error_str = str(error or "")

        new_status = "done" if verdict_norm == "CONFIRMED" else "partial"
        req_map = mem.get("requirements")
        if not isinstance(req_map, dict):
            req_map = {}
            mem["requirements"] = req_map
        for rid in reqs:
            rid = str(rid).strip().upper()
            if not rid:
                continue
            existing = req_map.get(rid) if isinstance(req_map.get(rid), dict) else {}
            if existing.get("status") == "done":
                continue
            req_map[rid] = {
                "text": str(existing.get("text", ""))[:500],
                "type": str(existing.get("type", "functional"))[:20],
                "status": new_status,
                "day": day,
                "updated": _now(),
            }

        completed = mem.get("completed_days")
        if not isinstance(completed, list):
            completed = []
            mem["completed_days"] = completed
        entry = {
            "day": day,
            "phase": str(phase or "")[:200],
            "verdict": verdict_norm or str(verdict or ""),
            "reqs": [str(r).strip().upper() for r in reqs][:20],
            "files": [str(f)[:200] for f in written][:30],
            "time": _now(),
        }
        completed = [c for c in completed if not (isinstance(c, dict) and c.get("day") == day)]
        completed.append(entry)
        mem["completed_days"] = completed[-100:]

        if error_str or verdict_norm in ("BLOCKED", "NEEDS_REVIEW"):
            issues = mem.get("known_issues")
            if not isinstance(issues, list):
                issues = []
                mem["known_issues"] = issues
            issues.append({
                "day": day,
                "phase": str(phase or "")[:200],
                "verdict": verdict_norm or str(verdict or ""),
                "error": error_str[:1000] or verdict_norm,
                "time": _now(),
            })
            mem["known_issues"] = issues[-100:]

        results: list[dict] = []
        for v in validation:
            if isinstance(v, dict):
                results.append({"cmd": str(v.get("cmd", ""))[:300], "ok": bool(v.get("ok"))})
            else:
                results.append({"cmd": str(v)[:300], "ok": False})
        mem["last_test_results"] = results[:20]

        # Sync tech stack / architecture / pending days from plan when available.
        plan = _read_json(gitdrip_dir(project) / "plan.json")
        if isinstance(plan, dict):
            arch = plan.get("architecture")
            if isinstance(arch, dict):
                if not mem.get("architecture"):
                    mem["architecture"] = {k: v for k, v in arch.items() if k in ("stack", "decisions")}
                if not mem.get("tech_stack") and isinstance(arch.get("stack"), list):
                    mem["tech_stack"] = [str(s)[:200] for s in arch["stack"]][:20]
            # Seed requirement texts/types from the plan for ids we track.
            plan_reqs = plan.get("requirements")
            if isinstance(plan_reqs, list):
                for req in plan_reqs:
                    if not isinstance(req, dict):
                        continue
                    rid = str(req.get("id", "")).strip().upper()
                    if rid and rid in req_map and not req_map[rid].get("text"):
                        req_map[rid]["text"] = str(req.get("text", ""))[:500]
                        req_map[rid]["type"] = str(req.get("type", "functional"))[:20]
            # Pending = plan days not yet completed.
            try:
                days = plan.get("days", [])
                if isinstance(days, list) and days:
                    done_days = {c.get("day") for c in mem["completed_days"]
                                 if isinstance(c, dict) and isinstance(c.get("day"), int)}
                    mem["pending_days"] = [d.get("day") for d in days
                                           if isinstance(d, dict) and d.get("day") not in done_days][:90]
            except Exception:
                pass

        save_memory(project, mem)
        return mem
    except Exception:
        try:
            return load_memory(project)
        except Exception:
            return _defaults()


def coverage(project: Path) -> dict:
    """Requirement coverage from plan.json + memory statuses + state history."""
    empty = {"total": 0, "done": 0, "partial": 0, "open": 0, "missing": []}
    try:
        plan = _read_json(gitdrip_dir(project) / "plan.json")
        if not isinstance(plan, dict):
            return dict(empty)
        plan_reqs = plan.get("requirements")
        if not isinstance(plan_reqs, list) or not plan_reqs:
            return dict(empty)
        ids: list[str] = []
        for req in plan_reqs:
            if isinstance(req, dict) and req.get("id"):
                rid = str(req["id"]).strip().upper()
                if rid and rid not in ids:
                    ids.append(rid)
        if not ids:
            return dict(empty)

        mem = load_memory(project)
        mem_reqs = mem.get("requirements") if isinstance(mem.get("requirements"), dict) else {}

        state = _read_json(gitdrip_dir(project) / "plan_state.json") or {}
        done_days: set = set()
        try:
            days_done = int(state.get("days_done", 0)) if isinstance(state, dict) else 0
            done_days = set(range(1, days_done + 1))
            for h in (state.get("history", []) if isinstance(state, dict) else []):
                if isinstance(h, dict) and isinstance(h.get("day"), int):
                    done_days.add(h["day"])
        except Exception:
            pass

        # Map each requirement to the plan days covering it.
        day_reqs: dict[str, list[int]] = {rid: [] for rid in ids}
        try:
            for d in plan.get("days", []):
                if not isinstance(d, dict):
                    continue
                dnum = d.get("day")
                for r in (d.get("reqs") or []):
                    rid = str(r).strip().upper()
                    if rid in day_reqs and isinstance(dnum, int):
                        day_reqs[rid].append(dnum)
        except Exception:
            pass

        done = partial = open_n = 0
        missing: list[str] = []
        for rid in ids:
            info = mem_reqs.get(rid) if isinstance(mem_reqs.get(rid), dict) else {}
            status = str(info.get("status", "open")).lower()
            if status not in ("done", "partial", "open"):
                status = "open"
            if status == "open":
                covering = day_reqs.get(rid, [])
                if covering and all(d in done_days for d in covering):
                    status = "partial"
                elif covering and any(d in done_days for d in covering):
                    status = "partial"
            if status == "done":
                done += 1
            elif status == "partial":
                partial += 1
            else:
                open_n += 1
                missing.append(rid)
        return {"total": len(ids), "done": done, "partial": partial, "open": open_n, "missing": missing}
    except Exception:
        return dict(empty)
