"""Probe which Messages API features the configured provider accepts.
Run inside the nabd container (it reads ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL):

    docker compose exec -T nabd python /srv/tests/provider_probe.py

Prints one line per feature: accepted, or the error the provider returned."""

import os
import sys

import anthropic

sys.path.insert(0, "/srv")
from app.settings import LLM_API_KEY, LLM_BASE_URL, MODEL  # noqa: E402  (same resolution as Nabd itself)

client = anthropic.Anthropic(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, max_retries=0, timeout=90)
TOOL = {"name": "get_weather", "description": "Weather for a city.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["city"],
                         "properties": {"city": {"type": "string"}}}}
ASK = [{"role": "user", "content": "What's the weather in Riyadh? Use the tool."}]


def attempt(label, fn):
    try:
        resp = fn()
        kinds = [b.type for b in resp.content]
        print(f"OK   {label}: model={resp.model} stop={resp.stop_reason} blocks={kinds} "
              f"in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return resp
    except anthropic.APIStatusError as exc:
        print(f"FAIL {label}: HTTP {exc.status_code} {str(exc.message)[:160]}")
    except anthropic.APIConnectionError as exc:
        print(f"FAIL {label}: connection {exc}")
    return None


print("base:", LLM_BASE_URL or "Anthropic default", "| model:", MODEL, "| key set:", bool(LLM_API_KEY))
if not LLM_BASE_URL and os.getenv("NABD_LLM_API_KEY"):
    sys.exit("refusing to run: a non-Anthropic key is set without NABD_LLM_BASE_URL")
attempt("minimal", lambda: client.messages.create(model=MODEL, max_tokens=60,
                                                    messages=[{"role": "user", "content": "Reply with OK."}]))
first = attempt("tools", lambda: client.messages.create(model=MODEL, max_tokens=800, tools=[TOOL], messages=ASK))
attempt("tools+strict", lambda: client.messages.create(model=MODEL, max_tokens=800, tools=[{**TOOL, "strict": True}],
                                                         messages=ASK))
attempt("thinking enabled", lambda: client.messages.create(
    model=MODEL, max_tokens=4000, thinking={"type": "enabled", "budget_tokens": 1024}, tools=[TOOL], messages=ASK))
attempt("thinking adaptive+display", lambda: client.messages.create(
    model=MODEL, max_tokens=4000, thinking={"type": "adaptive", "display": "summarized"}, tools=[TOOL], messages=ASK))
attempt("output_config effort", lambda: client.messages.create(
    model=MODEL, max_tokens=800, output_config={"effort": "medium"}, tools=[TOOL], messages=ASK))
attempt("top-level cache_control", lambda: client.messages.create(
    model=MODEL, max_tokens=800, cache_control={"type": "ephemeral"}, tools=[TOOL], messages=ASK))
attempt("beta endpoint + fallbacks", lambda: client.beta.messages.create(
    model=MODEL, max_tokens=800, betas=["server-side-fallback-2026-07-01"], fallbacks="default", tools=[TOOL], messages=ASK))

if first is not None:
    use = next((b for b in first.content if b.type == "tool_use"), None)
    if use:
        follow = ASK + [{"role": "assistant", "content": first.content},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id,
                                                      "content": "38°C, clear sky"}]}]
        attempt("tool_result round trip", lambda: client.messages.create(model=MODEL, max_tokens=400, tools=[TOOL],
                                                                         messages=follow))
