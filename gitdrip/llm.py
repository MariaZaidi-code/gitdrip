from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from gitdrip.config import GitdripError, gitdrip_dir

PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-3-5-haiku-latest"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "free": ("https://text.pollinations.ai/openai", "openai"),
}

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Anonymous tier allows roughly one request every 15s; pace free calls to
# stay inside the quota instead of tripping 402 rate-limit responses.
FREE_MIN_INTERVAL = 16.0
_free_last_call: float = 0.0


def _pace_free() -> None:
    global _free_last_call
    wait = FREE_MIN_INTERVAL - (time.monotonic() - _free_last_call)
    if wait > 0:
        time.sleep(wait)
    _free_last_call = time.monotonic()


@dataclass
class LLMConfig:
    provider: str = "auto"
    model: str = ""
    base_url: str = ""


@dataclass
class LLMClient:
    cfg: LLMConfig
    api_key: str = ""
    used: str = ""
    _attempts: list[str] = field(default_factory=list)

    def _candidates(self) -> list[tuple[str, str, str]]:
        provider = self.cfg.provider
        if provider == "heuristic":
            return []
        chain: list[tuple[str, str, str]] = []

        def key_for(name: str) -> str:
            return self.api_key or os.environ.get("GITDRIP_LLM_KEY", "") or os.environ.get(ENV_KEYS.get(name, ""), "")

        if provider == "custom":
            key = key_for("custom")
            if key and self.cfg.base_url:
                chain.append(("user:custom", self.cfg.base_url.rstrip("/"), key))
        elif provider in PRESETS and provider != "free":
            key = key_for(provider)
            if key:
                chain.append((f"user:{provider}", PRESETS[provider][0], key))
        elif provider == "auto":
            if self.api_key and self.cfg.base_url:
                chain.append(("user:custom", self.cfg.base_url.rstrip("/"), self.api_key))
            elif self.api_key:
                for name in PRESETS:
                    if name != "free":
                        chain.append((f"user:{name}", PRESETS[name][0], self.api_key))
            else:
                for name in ENV_KEYS:
                    env_key = os.environ.get(ENV_KEYS[name], "")
                    if env_key:
                        chain.append((f"user:{name}", PRESETS[name][0], env_key))
        chain.append(("free", PRESETS["free"][0], ""))
        return chain

    def _model_for(self, tag: str) -> str:
        if tag.startswith("user:custom"):
            return self.cfg.model or "default"
        if tag.startswith("user:"):
            provider = tag.split(":", 1)[1]
            return self.cfg.model or PRESETS[provider][1]
        return self.cfg.model if (self.cfg.provider == "free" and self.cfg.model) else PRESETS["free"][1]

    @property
    def attempts(self) -> list[str]:
        return self._attempts

    @property
    def available(self) -> bool:
        return bool(self._candidates()) or self.cfg.provider == "heuristic"

    def chat(self, prompt: str, system: str = "", timeout: int = 180) -> str:
        if self.cfg.provider == "heuristic":
            raise GitdripError("LLM disabled (heuristic mode)")
        errors = []
        for tag, base, key in self._candidates():
            try:
                reply = self._call(base, key, self._model_for(tag), prompt, system, timeout)
                self.used = tag
                return reply
            except Exception as exc:
                errors.append(f"{tag}: {exc}")
                self._attempts.append(f"{tag} failed: {exc}")
        raise GitdripError("all LLM providers failed -> " + "; ".join(errors))

    def _call_free(self, prompt: str, system: str, timeout: int) -> str:
        """Keyless free provider: POST chat API first, GET text API on 402."""
        base = PRESETS["free"][0]
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({"model": PRESETS["free"][1], "messages": messages, "temperature": 0.4}).encode("utf-8")
        _pace_free()
        try:
            return self._call_post(f"{base}/chat/completions", body, "", timeout)
        except RuntimeError as exc:
            if "HTTP 402" not in str(exc) and "HTTP 429" not in str(exc):
                raise
        return self._call_free_get(prompt, system, timeout)

    def _call_free_get(self, prompt: str, system: str, timeout: int) -> str:
        import random
        import urllib.parse

        base = PRESETS["free"][0]
        last_error: Exception = RuntimeError("free GET transport failed")
        for attempt in range(3):
            _pace_free()
            params = {"model": "openai", "seed": str(random.randint(1, 10**9))}
            if system:
                params["system"] = system[:2000]
            url = f"{base}/{urllib.parse.quote(prompt[:8000])}?{urllib.parse.urlencode(params)}"
            try:
                req = urllib.request.Request(url, method="GET", headers={"User-Agent": "gitdrip"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", "replace").strip()
                if text:
                    return text
                last_error = RuntimeError("empty reply from free provider")
            except urllib.error.HTTPError as exc:
                last_error = RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
                if exc.code in (401, 403, 404):
                    break
            except Exception as exc:
                last_error = exc
            time.sleep(20 * (attempt + 1))
        raise last_error

    def _call_post(self, url: str, body: bytes, key: str, timeout: int) -> str:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}" if key else "Bearer ",
            },
        )
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = _content_of(data)
                if content:
                    return content
                raise RuntimeError(f"empty content in reply: {json.dumps(data)[:300]}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code in (401, 402, 403, 404):
                    break
                if exc.code == 429:
                    time.sleep(8 * (attempt + 1))
                    continue
            except Exception as exc:
                last_error = exc
            time.sleep(3 * (attempt + 1))
        raise last_error or RuntimeError("unknown LLM error")

    def _call(self, base: str, key: str, model: str, prompt: str, system: str, timeout: int) -> str:
        if base.rstrip("/") == PRESETS["free"][0] and not key:
            return self._call_free(prompt, system, timeout)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({"model": model, "messages": messages, "temperature": 0.4}).encode("utf-8")
        return self._call_post(f"{base}/chat/completions", body, key, timeout)


def _content_of(data: dict) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected reply shape: {json.dumps(data)[:300]}") from exc
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        joined = "".join(parts).strip()
        if joined:
            return joined
    if message.get("refusal"):
        raise RuntimeError(f"model refused: {message['refusal']}")
    finish = data.get("choices", [{}])[0].get("finish_reason")
    raise RuntimeError(f"no text content (finish_reason={finish})")


def extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise GitdripError("LLM reply contains no JSON object")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise GitdripError("unterminated JSON object in LLM reply")


def client_for(project: Path, settings: dict, secrets: dict) -> LLMClient:
    llm = settings.get("llm", {})
    return LLMClient(
        cfg=LLMConfig(
            provider=llm.get("provider", "auto"),
            model=llm.get("model", ""),
            base_url=llm.get("base_url", ""),
        ),
        api_key=secrets.get("llm_key", ""),
    )


def load_settings(project: Path) -> dict:
    path = gitdrip_dir(project) / "settings.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_settings(project: Path, data: dict) -> None:
    path = gitdrip_dir(project) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_secrets(project: Path) -> dict:
    path = gitdrip_dir(project) / "secrets.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_secrets(project: Path, data: dict) -> None:
    path = gitdrip_dir(project) / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
