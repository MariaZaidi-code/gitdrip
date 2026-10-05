from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from gitdrip.config import GitdripError


def send_email(settings: dict, secrets: dict, subject: str, body: str) -> None:
    email = settings.get("email", {})
    host = email.get("smtp_host", "")
    if not host:
        raise GitdripError("SMTP is not configured")
    password = secrets.get("smtp_password", "")
    if not password:
        raise GitdripError("SMTP password is not set")
    port = int(email.get("smtp_port", 465))
    user = email.get("smtp_user", "")
    sender = email.get("smtp_from") or user or "gitdrip@localhost"
    recipient = email.get("email_to") or user
    if not recipient:
        raise GitdripError("no recipient email configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    if port == 587:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as server:
            if user:
                server.login(user, password)
            server.send_message(msg)


def _github_repo_slug(target: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(target), "remote", "get-url", "origin"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return ""
    url = proc.stdout.strip()
    if "github.com" not in url:
        return ""
    if url.startswith("git@"):
        path = url.split(":", 1)[-1]
    else:
        path = url.split("github.com/", 1)[-1]
    path = path.removesuffix(".git").strip("/")
    return path if path.count("/") == 1 else ""


def _github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line[len("password="):]
    except Exception:
        pass
    return ""


def create_issue(target: Path, title: str, body: str) -> str:
    slug = _github_repo_slug(target)
    token = _github_token()
    if not slug or not token:
        return "report saved locally (no GitHub token for issue fallback)"
    payload = json.dumps({
        "title": title[:250],
        "body": body,
        "labels": ["gitdrip-report"],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/issues",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitdrip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return f"reported as GitHub issue #{data.get('number')} (GitHub notifies you by email)"
    except Exception as exc:
        return f"report saved locally (issue fallback failed: {exc})"
