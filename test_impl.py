import sys
import time

from gitdrip.agents import implement_phase
from gitdrip.llm import LLMClient, LLMConfig

phase = {
    "day": 1,
    "phase": "Project Scaffolding",
    "reasoning": "scaffold first",
    "goal": "create app skeleton",
    "tasks": ["create app.py with a hello function", "create README.md"],
    "files": ["app.py", "README.md"],
    "validation": ["python -m compileall -q ."],
    "report_expectation": "files exist and compile",
}

client = LLMClient(cfg=LLMConfig(provider="free"))
t0 = time.time()

from gitdrip.agents import IMPLEMENT_SYSTEM

prompt = f"Phase to implement:\n{__import__('json').dumps(phase, indent=2)}\n\nRepository context:\nREPOSITORY TREE:\n(empty repository)"
try:
    reply = client.chat(prompt, system=IMPLEMENT_SYSTEM)
    print("chat OK in %.1fs via %s" % (time.time() - t0, client.used))
    print("--- raw reply (%d chars) ---" % len(reply))
    print(reply[:2000])
except Exception as exc:
    print("CHAT FAILED in %.1fs: %r" % (time.time() - t0, exc))
    print("attempts:", client.attempts)
    sys.exit(1)
