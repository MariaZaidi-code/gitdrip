from __future__ import annotations

import base64
import datetime as dt
import json
import subprocess
from pathlib import Path

from gitdrip import gitops
from gitdrip.config import Config, GitdripError, gitdrip_dir, load_config
from gitdrip.emailer import _github_repo_slug, _github_token
from gitdrip.plan import DOC_SNAPSHOT, PLAN_NAME, STATE_NAME, load_plan
from gitdrip.llm import load_secrets, load_settings

WORKFLOW_REL = ".github/workflows/gitdrip-daily.yml"


def _utc_cron(push_time: str) -> str:
    hour, minute = (int(p) for p in push_time.split(":"))
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    local = dt.datetime(2000, 1, 1, hour, minute) - offset
    return f"{local.minute} {local.hour} * * *"


def workflow_yaml(cfg: Config) -> str:
    cron = _utc_cron(cfg.push_time)
    return f"""name: gitdrip daily agent

on:
  schedule:
    - cron: '{cron}'
  workflow_dispatch:

permissions:
  contents: write
  issues: write

concurrency:
  group: gitdrip-daily
  cancel-in-progress: false

jobs:
  drip:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install gitdrip
        run: pip install --quiet git+https://github.com/MariaZaidi-code/gitdrip.git

      - name: Run today's phase (implement, validate, report)
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          GITDRIP_LLM_KEY: ${{{{ secrets.GITDRIP_LLM_KEY }}}}
          GITDRIP_SMTP_PASSWORD: ${{{{ secrets.GITDRIP_SMTP_PASSWORD }}}}
        run: python -m gitdrip cloud-run
"""


def _sync_secret(slug: str, name: str, value: str, token: str) -> str:
    try:
        import nacl.public
    except ImportError:
        return f"set {name} manually: https://github.com/{slug}/settings/secrets/actions (pip install pynacl to automate)"

    import urllib.request

    def api(url: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            method="GET" if payload is None else "PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gitdrip",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    key_data = api(f"https://api.github.com/repos/{slug}/actions/secrets/public-key")
    public_key = nacl.public.PublicKey(base64.b64decode(key_data["key"]))
    encrypted = base64.b64encode(nacl.public.SealedBox(public_key).encrypt(value.encode())).decode()
    api(
        f"https://api.github.com/repos/{slug}/actions/secrets/{name}",
        {"encrypted_value": encrypted, "key_id": key_data["key_id"]},
    )
    return f"secret {name} synced to GitHub"


def deploy(project: Path, sync_llm_key: bool = False, sync_smtp: bool = False) -> list[str]:
    cfg = load_config(project)
    target = Path(cfg.target_repo).resolve()
    if target != project.resolve():
        raise GitdripError(
            "cloud deploy needs the gitdrip project inside the repository "
            "(run 'gitdrip init' from the repo root so plan files are committed)"
        )
    if not load_plan(project):
        raise GitdripError("no plan yet - create one before deploying")
    gitops.ensure_repo(target)

    messages: list[str] = []
    workflow = target / WORKFLOW_REL
    workflow.parent.mkdir(parents=True, exist_ok=True)
    with open(workflow, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(workflow_yaml(cfg))
    messages.append(f"workflow written: {WORKFLOW_REL} (daily at {cfg.push_time}, workflow_dispatch enabled)")

    commit_paths = [WORKFLOW_REL]
    for name in (PLAN_NAME, STATE_NAME, DOC_SNAPSHOT, "settings.json"):
        if (gitdrip_dir(project) / name).is_file():
            commit_paths.append(f".gitdrip/{name}")

    changed, sha_or_note = gitops.commit_paths(target, commit_paths, "gitdrip: enable daily cloud agent")
    if changed:
        messages.append(f"committed {sha_or_note}")
    else:
        messages.append("cloud files already committed")

    slug = _github_repo_slug(target)
    token = _github_token()
    if not slug or not token:
        messages.append("push skipped: no GitHub remote/token detected")
        return messages
    try:
        gitops.push(target, cfg.remote, cfg.branch,
                    set_upstream=not gitops.has_upstream(target, cfg.remote, cfg.branch))
        messages.append("pushed to GitHub - scheduled workflow is armed")
    except GitdripError as exc:
        messages.append(f"push failed: {exc}")
        return messages

    settings = load_settings(project)
    secrets = load_secrets(project)
    if sync_llm_key and secrets.get("llm_key") and settings.get("llm", {}).get("provider", "auto") not in ("free", "heuristic"):
        messages.append(_sync_secret(slug, "GITDRIP_LLM_KEY", secrets["llm_key"], token))
    elif not secrets.get("llm_key"):
        messages.append("LLM key: using the free keyless provider (no secret needed)")
    else:
        messages.append("LLM key left local (enable 'sync key to cloud' to use your own key in Actions)")
    if sync_smtp and secrets.get("smtp_password"):
        messages.append(_sync_secret(slug, "GITDRIP_SMTP_PASSWORD", secrets["smtp_password"], token))
    messages.append(
        "laptop-off runs: GitHub Actions executes the workflow daily; "
        "enable Actions on the repo if prompted (repo Settings > Actions)"
    )
    return messages
