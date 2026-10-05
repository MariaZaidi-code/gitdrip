from gitdrip.llm import LLMClient, LLMConfig, extract_json

c = LLMClient(cfg=LLMConfig(provider="free"))
print("candidates:", [(t, b) for t, b, k in c._candidates()])
r = c.chat(
    'Return a JSON object: {"ok": true}',
    system="You are a JSON API. Respond with JSON only.",
)
print("reply:", r[:100])
print("parsed:", extract_json(r))
print("used:", c.used)

c2 = LLMClient(cfg=LLMConfig(provider="auto"))
print("auto candidates:", [(t, b) for t, b, k in c2._candidates()])

c3 = LLMClient(cfg=LLMConfig(provider="heuristic"))
print("heuristic available:", c3.available, c3._candidates())
