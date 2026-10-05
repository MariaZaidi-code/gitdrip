from __future__ import annotations

import base64
import webbrowser
from pathlib import Path

from gitdrip import cloud, scheduler
from gitdrip.config import GitdripError, find_project, gitdrip_dir, load_config, save_config, validate_time
from gitdrip.docparse import extract_bytes, truncate
from gitdrip.emailer import send_email
from gitdrip.llm import (
    PRESETS,
    client_for,
    load_secrets,
    load_settings,
    save_secrets,
    save_settings,
)
from gitdrip.plan import (
    DOC_SNAPSHOT,
    doc_snapshot_text,
    load_plan,
    load_state,
    make_plan,
    plan_path,
    save_doc_snapshot,
    state_path,
)
from gitdrip.queue import load_queue, pending_batches, stage_paths
from gitdrip.runner import run_once

STATIC_DIR = Path(__file__).parent / "static"


def create_app(project: Path):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="gitdrip", docs_url=None, redoc_url=None)

    def fail(message: str, code: int = 400):
        return JSONResponse({"ok": False, "error": message}, status_code=code)

    def ok(**kwargs):
        return {"ok": True, **kwargs}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status():
        cfg = load_config(project)
        settings = load_settings(project)
        secrets = load_secrets(project)
        plan = load_plan(project)
        state = load_state(project)
        queue = load_queue(project)
        target = Path(cfg.target_repo)
        llm = settings.get("llm", {})
        email = settings.get("email", {})
        return ok(
            project=str(project),
            target=str(target),
            branch=cfg.branch,
            remote=cfg.remote,
            push_time=cfg.push_time,
            batch_size=cfg.batch_size,
            queue={
                "pending": len(pending_batches(queue)),
                "completed": len(queue.get("batches", [])) - len(pending_batches(queue)),
            },
            schedule=scheduler.schedule_status(cfg),
            plan={
                "exists": bool(plan),
                "title": (plan or {}).get("title", ""),
                "total": len((plan or {}).get("days", [])),
                "done": state.get("days_done", 0),
                "provider": (plan or {}).get("provider", ""),
                "warning": (plan or {}).get("warning", ""),
            },
            llm={
                "provider": llm.get("provider", "auto"),
                "model": llm.get("model", ""),
                "base_url": llm.get("base_url", ""),
                "has_key": bool(secrets.get("llm_key")),
            },
            email={
                "configured": bool(email.get("smtp_host")),
                "has_password": bool(secrets.get("smtp_password")),
                "to": email.get("email_to", ""),
            },
            doc={
                "exists": (gitdrip_dir(project) / DOC_SNAPSHOT).is_file(),
                "chars": len(doc_snapshot_text(project)),
            },
            cloud={"workflow": (target / cloud.WORKFLOW_REL).is_file()},
            history=state.get("history", []),
        )

    @app.get("/api/config")
    def get_config():
        return ok(
            settings=load_settings(project),
            has_llm_key=bool(load_secrets(project).get("llm_key")),
            has_smtp_password=bool(load_secrets(project).get("smtp_password")),
            providers=["auto", "free", *PRESETS.keys(), "custom"],
        )

    @app.post("/api/config")
    def post_config(body: dict):
        settings = load_settings(project)
        for section in ("llm", "email"):
            if section in body:
                merged = settings.get(section, {})
                merged.update({k: v for k, v in body[section].items() if v is not None})
                settings[section] = merged
        if "push_time" in body:
            settings["push_time"] = validate_time(str(body["push_time"]))
            cfg = load_config(project)
            cfg.push_time = settings["push_time"]
            save_config(project, cfg)
        save_settings(project, settings)
        return ok(settings=settings)

    @app.post("/api/secret")
    def post_secret(body: dict):
        name = body.get("name", "")
        if name not in ("llm_key", "smtp_password"):
            return fail("unknown secret name")
        secrets = load_secrets(project)
        value = str(body.get("value", "") or "").strip()
        if value:
            secrets[name] = value
        else:
            secrets.pop(name, None)
        save_secrets(project, secrets)
        return ok()

    @app.post("/api/upload")
    def upload(body: dict):
        name = str(body.get("name", "document.txt"))
        if body.get("content") is not None:
            data = base64.b64decode(body["content"])
        else:
            data = str(body.get("text", "")).encode("utf-8")
            name = str(body.get("name") or name)
        text = extract_bytes(name, data)
        if not text.strip():
            return fail("document is empty")
        save_doc_snapshot(project, text)
        return ok(name=name, chars=len(text), preview=truncate(text, 800))

    @app.get("/api/doc")
    def get_doc():
        text = doc_snapshot_text(project)
        return ok(name=DOC_SNAPSHOT, chars=len(text), text=truncate(text, 200_000))

    @app.post("/api/plan")
    def post_plan(body: dict):
        from gitdrip.llm import client_for as _client_for

        days = int(body.get("days", 7))
        settings = load_settings(project)
        secrets = load_secrets(project)
        client = _client_for(project, settings, secrets)
        plan = make_plan(project, client, days)
        return ok(plan=plan)

    @app.get("/api/plan")
    def get_plan():
        return ok(plan=load_plan(project), state=load_state(project))

    @app.post("/api/plan/reset")
    def reset_plan():
        for path in (plan_path(project), state_path(project)):
            if path.is_file():
                path.unlink()
        return ok()

    @app.post("/api/run")
    def run(body: dict):
        from gitdrip.agents import run_day

        day = body.get("day")
        result = run_day(project, day=int(day) if day else None, force=bool(body.get("force")))
        return ok(result=result)

    @app.get("/api/reports")
    def reports():
        state = load_state(project)
        reports_dir = gitdrip_dir(project) / "reports"
        items = []
        for entry in reversed(state.get("history", [])):
            day = entry.get("day")
            path = reports_dir / f"day-{day}.md"
            items.append({
                "day": day,
                "phase": entry.get("phase", ""),
                "verdict": entry.get("verdict", ""),
                "time": entry.get("time", ""),
                "sha": entry.get("sha", ""),
                "exists": path.is_file(),
            })
        return ok(reports=items)

    @app.get("/api/report/{day}")
    def report(day: int):
        path = gitdrip_dir(project) / "reports" / f"day-{day}.md"
        if not path.is_file():
            return fail("report not found", 404)
        return ok(day=day, markdown=path.read_text(encoding="utf-8"))

    @app.post("/api/schedule")
    def post_schedule(body: dict):
        cfg = load_config(project)
        if body.get("time"):
            cfg.push_time = validate_time(str(body["time"]))
            save_config(project, cfg)
        message = scheduler.schedule(cfg, project)
        return ok(message=message)

    @app.delete("/api/schedule")
    def delete_schedule():
        cfg = load_config(project)
        return ok(message=scheduler.unschedule(cfg))

    @app.post("/api/deploy")
    def deploy_route(body: dict):
        messages = cloud.deploy(
            project,
            sync_llm_key=bool(body.get("sync_llm_key")),
            sync_smtp=bool(body.get("sync_smtp")),
        )
        return ok(messages=messages)

    @app.post("/api/test-llm")
    def test_llm():
        settings = load_settings(project)
        secrets = load_secrets(project)
        client = client_for(project, settings, secrets)
        try:
            reply = client.chat(
                "Reply with exactly: gitdrip OK",
                system="You are a connectivity test. Follow instructions exactly.",
                timeout=60,
            )
            return ok(provider=client.used, reply=reply.strip()[:200], attempts=client.attempts)
        except GitdripError as exc:
            return fail(str(exc), 502)

    @app.post("/api/test-email")
    def test_email():
        settings = load_settings(project)
        secrets = load_secrets(project)
        try:
            send_email(settings, secrets, "[gitdrip] test email", "If you can read this, email delivery works.")
            return ok()
        except Exception as exc:
            return fail(str(exc), 502)

    @app.post("/api/batch/stage")
    def batch_stage(body: dict):
        cfg = load_config(project)
        paths = body.get("paths")
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(",") if p.strip()]
        if not paths:
            return fail("no paths given")
        created = stage_paths(project, cfg, paths, body.get("message"))
        return ok(batches=[{"id": b.id, "message": b.message, "files": len(b.files)} for b in created])

    @app.post("/api/batch/run")
    def batch_run(body: dict):
        cfg = load_config(project)
        results = run_once(project, cfg, dry_run=bool(body.get("dry_run")), run_all=bool(body.get("all")))
        return ok(lines=results)

    return app


def serve(project: Path, port: int = 7788, open_browser: bool = True) -> None:
    import uvicorn

    app = create_app(project)
    url = f"http://127.0.0.1:{port}"
    print(f"gitdrip dashboard: {url}  (project: {project})")
    print("press ctrl+c to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
