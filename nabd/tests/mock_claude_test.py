"""Offline test of the Claude engine's agent loop against a local mock of the
Messages API. Run inside the nabd container:

    docker compose exec -T nabd python /srv/tests/mock_claude_test.py

It checks that the loop runs tools, parses submit_diagnosis and submit_review,
accounts tokens, and that every request carries the parameters we rely on."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Point Nabd at the local mock, whatever provider the container is configured for.
os.environ["NABD_LLM_API_KEY"] = "test-key"
os.environ["NABD_LLM_BASE_URL"] = "http://127.0.0.1:8999"
os.environ["NABD_MODEL"] = "claude-opus-5-5"
sys.path.insert(0, "/srv")

REQUESTS: list[dict] = []


def _message(content, stop="tool_use"):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 0}}


DIAGNOSIS = {
    "summary": "Redis is full because of a key leak.",
    "root_causes": [{
        "node": "redis", "category": "cache_memory_exhaustion", "title": "Redis full",
        "explanation": "Leak under tmp:session-dump:", "evidence": ["used 55/48 MB"], "confidence": 0.9,
        "action": "flush_redis_keys",
        "action_params": {"service": "", "prefix": "tmp:session-dump:", "route": "", "application_name": ""},
    }],
    "ruled_out": [{"item": "inventory deploy", "reason": "unrelated"}],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        body["_beta_header"] = self.headers.get("anthropic-beta")
        REQUESTS.append(body)
        names = {t["name"] for t in body.get("tools", [])}
        last = body["messages"][-1]
        answered = isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"])
        thinking = {"type": "thinking", "thinking": "Checking Redis first.", "signature": "sig"}
        if "submit_review" in names:
            content = [thinking, {"type": "tool_use", "id": "tu_r", "name": "submit_review", "input": {"verdicts": [
                {"hypothesis_id": "H1", "challenge": "network?", "verdict": "upheld", "reasoning": "INFO agrees."}]}}]
        elif not answered:
            content = [thinking,
                       {"type": "tool_use", "id": "tu_1", "name": "inspect_redis", "input": {}},
                       {"type": "tool_use", "id": "tu_2", "name": "list_alerts", "input": {}}]
        else:
            content = [{"type": "text", "text": "Diagnosis ready."},
                       {"type": "tool_use", "id": "tu_3", "name": "submit_diagnosis", "input": DIAGNOSIS}]
        payload = json.dumps(_message(content)).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeIncident:
    id, kind, opened_at = "INC-TEST", "reactive", 0.0
    claude_messages = None
    tokens = {"input": 0, "output": 0, "cache_read": 0}
    llm_calls, cost = 0, 0.0


async def main() -> int:
    from app.claude_engine import ClaudeEngine
    events = []
    emit = lambda t, **d: events.append((t, d))  # noqa: E731
    engine = ClaudeEngine()
    inc = FakeIncident()
    diag = await engine.investigate(inc, [], {"redis": "crit"}, emit)
    verdicts = await engine.review(inc, diag["hypotheses"], emit)

    failures = []
    h = diag["hypotheses"][0]
    if h["action"] != {"name": "flush_redis_keys", "params": {"prefix": "tmp:session-dump:"}}:
        failures.append(f"action parsed wrong: {h['action']}")
    if verdicts.get("H1", {}).get("verdict") != "upheld":
        failures.append(f"review parsed wrong: {verdicts}")
    if inc.llm_calls != 3:
        failures.append(f"expected 3 calls, got {inc.llm_calls}")
    if not any(t == "tool_result" and d["tool"] == "inspect_redis" for t, d in events):
        failures.append("inspect_redis never ran")
    if not any(t == "thinking" for t, _ in events):
        failures.append("thinking summary not surfaced")
    for req in REQUESTS:
        if req.get("thinking") != {"type": "adaptive", "display": "summarized"}:
            failures.append(f"thinking wrong: {req.get('thinking')}")
        if req.get("fallbacks") != "default" or "server-side-fallback-2026-07-01" not in (req["_beta_header"] or ""):
            failures.append("fallbacks or beta header missing")
        if not all(t.get("strict") for t in req["tools"]):
            failures.append("a tool is not strict")
        if req.get("output_config", {}).get("effort") not in ("low", "medium", "high"):
            failures.append(f"effort missing: {req.get('output_config')}")
        if req.get("cache_control") != {"type": "ephemeral"}:
            failures.append("cache_control missing")
    tool_results = [b for r in REQUESTS for m in r["messages"] if isinstance(m["content"], list)
                    for b in m["content"] if b.get("type") == "tool_result"]
    second = REQUESTS[1]["messages"][-1]["content"]
    if len([b for b in second if b.get("type") == "tool_result"]) != 2:
        failures.append("parallel tool results were not returned in one user message")
    print(json.dumps({"calls": inc.llm_calls, "tokens": inc.tokens, "cost": round(inc.cost, 5),
                      "tool_results_sent": len(tool_results), "model": REQUESTS[0]["model"]}))
    if failures:
        print("FAIL\n- " + "\n- ".join(dict.fromkeys(failures)))
        return 1
    print("PASS: Claude engine loop, parsing and request shape")
    return 0


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8999), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.exit(asyncio.run(main()))
