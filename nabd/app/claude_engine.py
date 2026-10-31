"""The deep path: Claude investigates with the same read-only tools, submits
a structured diagnosis, and a second Claude pass plays the skeptic. Claude
only proposes; the safety gate decides and executes.

Any API problem (no key, network, refusal) raises EngineError and the
orchestrator falls back to the core engine, so the demo never stalls."""

import asyncio
import json
import time

import anthropic

from . import actions, settings, tools, topology
from .settings import EFFORT, MODEL

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

CATEGORIES = ["cache_memory_exhaustion", "bad_config_deploy", "network_degradation", "process_crash",
              "cpu_saturation", "memory_leak", "db_connection_exhaustion", "db_lock_contention",
              "poison_message", "silent_data_loss", "orphaned_payments", "lost_events", "corrupted_data",
              "dependency_failure", "other"]
ACTION_NAMES = [*actions.CATALOG, "none"]


class EngineError(Exception):
    pass


def _problems(tool_name: str, data) -> list[str]:
    """Validate a final submission against the schema in code (strict mode only holds on Anthropic)."""
    if not isinstance(data, dict):
        return ["input must be an object"]
    out: list[str] = []
    if tool_name == "submit_diagnosis":
        causes = data.get("root_causes")
        if not isinstance(causes, list):
            return ["root_causes must be a list"]
        for i, rc in enumerate(causes):
            if not isinstance(rc, dict):
                out.append(f"root_causes[{i}] must be an object")
                continue
            for key in ("node", "category", "title", "explanation", "action"):
                if not isinstance(rc.get(key), str) or not rc.get(key):
                    out.append(f"root_causes[{i}].{key} is missing")
            if rc.get("node") and rc["node"] not in topology.NODES:
                out.append(f"root_causes[{i}].node must be one of {sorted(topology.NODES)}")
            if rc.get("category") and rc["category"] not in CATEGORIES:
                out.append(f"root_causes[{i}].category must be one of {CATEGORIES}")
            if rc.get("action") and rc["action"] not in ACTION_NAMES:
                out.append(f"root_causes[{i}].action must be one of {ACTION_NAMES}")
            if not isinstance(rc.get("action_params", {}), dict):
                out.append(f"root_causes[{i}].action_params must be an object")
            if not isinstance(rc.get("evidence", []), list):
                out.append(f"root_causes[{i}].evidence must be a list of strings")
    elif tool_name == "submit_review":
        verdicts = data.get("verdicts")
        if not isinstance(verdicts, list):
            return ["verdicts must be a list"]
        for i, v in enumerate(verdicts):
            if not isinstance(v, dict) or v.get("verdict") not in ("upheld", "refuted", "uncertain") \
                    or not v.get("hypothesis_id"):
                out.append(f"verdicts[{i}] needs hypothesis_id and verdict upheld|refuted|uncertain")
    return out


def _strict(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SUBMIT_DIAGNOSIS = {
    "name": "submit_diagnosis",
    "description": "Submit your final diagnosis: one entry per independent root cause, each with the single "
                   "catalog action that fixes it. Call this exactly once, when the evidence is in.",
    "strict": True,
    "input_schema": _strict({
        "summary": {"type": "string", "description": "Two sentences for the incident channel."},
        "root_causes": {"type": "array", "items": _strict({
            "node": {"type": "string", "enum": list(topology.NODES)},
            "category": {"type": "string", "enum": CATEGORIES},
            "title": {"type": "string"},
            "explanation": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "Concrete facts from tool results: numbers, log templates, deploy ids."},
            "confidence": {"type": "number", "description": "0 to 1, calibrated."},
            "action": {"type": "string", "enum": ACTION_NAMES},
            "action_params": _strict({
                "service": {"type": "string"}, "prefix": {"type": "string"},
                "route": {"type": "string"}, "application_name": {"type": "string"},
            }),
        })},
        "ruled_out": {"type": "array", "items": _strict({"item": {"type": "string"}, "reason": {"type": "string"}})},
    }),
}

SUBMIT_REVIEW = {
    "name": "submit_review",
    "description": "Submit your verdict on every hypothesis. Call exactly once.",
    "strict": True,
    "input_schema": _strict({
        "verdicts": {"type": "array", "items": _strict({
            "hypothesis_id": {"type": "string"},
            "challenge": {"type": "string", "description": "The strongest alternative explanation you tested."},
            "verdict": {"type": "string", "enum": ["upheld", "refuted", "uncertain"]},
            "reasoning": {"type": "string"},
        })},
    }),
}

CATALOG_TEXT = "\n".join(
    f"- {name}({', '.join(spec['params'])}): {spec['summary']}" for name, spec in actions.CATALOG.items())

SYSTEM = f"""You are Nabd, the on-call SRE agent for Dukkan, a Saudi online store.

Dukkan's components and what each depends on:
- gateway (public API): calls auth, inventory, orders and payments for every checkout
- auth: issues and verifies session tokens; no dependencies
- inventory: product catalog in Postgres (cached in Redis) and stock reservations whose holds live in Redis
- orders: writes orders to Postgres over the "primary" network link (through a proxy); a "standby" link goes direct
- payments: idempotency keys in Redis, then charges the external PSP; its PSP timeout comes from versioned config
- psp: the external payment provider, with its own settlement ledger
- orders also publishes an order event to the "events" queue (a Redis list); orders' write mode and queue cap
  come from versioned config
- fulfillment: a worker that consumes the events queue and books each order for shipping; pallet orders first
  need a quote from the external freight "carrier"
- catalog-sync: a background job that keeps product prices in line with the external "supplier" catalog; its
  mode comes from versioned config. The products table records which client last wrote each price.
- redis (maxmemory with noeviction) and postgres (the store services connect as an ordinary user)

Some failures are silent: every request succeeds and no error is logged, yet money, orders or messages go
missing. Mizan, Nabd's invariant checker, reconciles independent records (PSP ledger, orders table, queue,
fulfillments, supplier catalog). check_invariants shows the books; trace_item follows one payment or order through every hop
and tells where it was lost. A loss inside a component points at the change that configures it. Repairing
the damaged records (replaying or refunding) is done separately and deterministically: diagnose the cause.

An incident is open. Several independent faults can be active at once, and one fault usually produces
symptoms in many services. Your job is to find every independent root cause, tell them apart from the
symptoms they cause, and name the one catalog action that fixes each root cause.

Work like a senior SRE: start broad (alerts, health), then test the hypotheses that would explain the
most symptoms. Prefer evidence that separates competing explanations, such as a probe that compares two
network paths, a deploy diff whose timing matches the first errors, or a client application holding
database resources. Recent deploys are not guilty by default; rule them out when the evidence says so.
Call independent tools in parallel. Ten tool calls is usually enough.

Remediation catalog (a separate safety gate validates, rates risk and executes; you never execute):
{CATALOG_TEXT}
Fill action_params with the fields the action needs and empty strings for the rest. Use action "none"
when no catalog action is safe; say why in the explanation.

Finish by calling submit_diagnosis once. Every evidence item must be a concrete fact from a tool result.
Do not include internal or system XML tags in your response."""

SKEPTIC_SYSTEM = """You are the skeptic on Nabd's incident team. Another agent diagnosed a production incident
in Dukkan, a Saudi online store (gateway, auth, inventory, orders, payments, psp, redis, postgres).
Before anything is executed, try to refute each hypothesis: name the strongest alternative explanation,
test it with the read-only tools, and give a verdict. Refute only when the evidence contradicts the
hypothesis; use "uncertain" when it is merely incomplete. Keep each reasoning to two sentences.
Finish by calling submit_review once. Do not include internal or system XML tags in your response."""


class ClaudeEngine:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.LLM_API_KEY or None, base_url=settings.LLM_BASE_URL, max_retries=2, timeout=120,
        ) if settings.CLAUDE_AVAILABLE else None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def _create(self, system: str, tool_list: list, messages: list, effort: str) -> object:
        kwargs = dict(
            model=MODEL, max_tokens=16000, system=system, messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        if tool_list:
            kwargs["tools"] = tool_list
        if MODEL in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            return await self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise EngineError("the API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise EngineError("rate limited") from exc
        except anthropic.BadRequestError as exc:
            raise EngineError(f"bad request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise EngineError(f"API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise EngineError(f"cannot reach the {settings.LLM_NAME} API") from exc

    def _account(self, inc, resp) -> None:
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        inc.tokens["input"] += u.input_tokens + cache_write
        inc.tokens["output"] += u.output_tokens
        inc.tokens["cache_read"] += cache_read
        inc.llm_calls += 1
        inc.cost = settings.token_cost(inc.tokens["input"], inc.tokens["output"], inc.tokens["cache_read"])

    async def _loop(self, inc, emit, system: str, messages: list, final_tool: dict, max_turns: int,
                    effort: str, role: str) -> dict:
        tool_list = [*tools.TOOL_SCHEMAS, final_tool]
        for turn in range(max_turns):
            started = time.time()
            resp = await self._create(system, tool_list, messages, effort)
            self._account(inc, resp)
            emit("llm_call", role=role, model=getattr(resp, "model", MODEL),
                 ms=round((time.time() - started) * 1000),
                 tokens={"in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
                         "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0},
                 cost=round(inc.cost, 4))
            if resp.stop_reason == "refusal":
                raise EngineError("the model declined this request")
            for block in resp.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    emit("thinking", role=role, text=block.thinking)
                elif block.type == "text" and block.text.strip():
                    emit("llm_text", role=role, text=block.text.strip())
            if resp.stop_reason == "max_tokens":
                raise EngineError("response hit max_tokens")
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                messages.append({"role": "user", "content": f"Call {final_tool['name']} now with your conclusions."})
                continue

            async def run(block):
                if block.name == final_tool["name"]:
                    return block, None
                args = dict(block.input)
                emit("tool_call", tool=block.name, args=args, engine="claude", role=role)
                t0 = time.time()
                result = await tools.call(block.name, args)
                emit("tool_result", tool=block.name, args=args, engine="claude", role=role,
                     brief=tools.brief(block.name, result), ms=round((time.time() - t0) * 1000))
                return block, result

            final_input = None
            results = []
            for block, result in await asyncio.gather(*(run(b) for b in uses)):
                if result is None:
                    # strict schemas are guaranteed on Anthropic only; other providers may send anything
                    problems = _problems(final_tool["name"], block.input)
                    if problems:
                        emit("info", title=f"Rejected a malformed {final_tool['name']}: {problems[0]}")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                        "content": "Invalid input, fix and call again: " + "; ".join(problems[:6])})
                        continue
                    final_input = dict(block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": "Received. The safety gate will review the proposed actions."})
                else:
                    item = {"type": "tool_result", "tool_use_id": block.id, "content": tools.to_text(result)}
                    if "error" in result and len(result) <= 2:
                        item["is_error"] = True
                    results.append(item)
            if final_input is None and turn == max_turns - 2:
                results.append({"type": "text", "text": f"One turn left: call {final_tool['name']} next with "
                                                        f"what you have."})
            messages.append({"role": "user", "content": results})
            if final_input is not None:
                return final_input
        raise EngineError(f"no {final_tool['name']} within {max_turns} turns")

    # ---------------------------------------------------------- public

    async def investigate(self, inc, alerts: list[dict], health: dict, emit, feedback: str | None = None) -> dict:
        if not self.available:
            raise EngineError("no API key configured")
        if inc.claude_messages is None or feedback is None:
            brief = {
                "incident": inc.id, "kind": inc.kind,
                "opened": time.strftime("%H:%M:%S", time.gmtime(inc.opened_at)) + " UTC",
                "firing_alerts": [{k: a[k] for k in ("name", "service", "dependency", "summary")} for a in alerts],
                "node_health": health,
            }
            inc.claude_messages = [{"role": "user", "content":
                                    "Investigate this incident and submit your diagnosis.\n\n" + json.dumps(brief, ensure_ascii=False)}]
        else:
            inc.claude_messages.append({"role": "user", "content": feedback})
        raw = await self._loop(inc, emit, SYSTEM, inc.claude_messages, SUBMIT_DIAGNOSIS, 12, EFFORT, "investigator")
        hyps = []
        for i, rc in enumerate(raw.get("root_causes", []), 1):
            params = {k: v for k, v in (rc.get("action_params") or {}).items() if isinstance(v, str) and v}
            try:
                confidence = max(0.0, min(1.0, float(rc.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            hyps.append({
                "id": f"H{i}", "node": rc["node"], "category": rc["category"], "title": rc["title"],
                "explanation": rc["explanation"], "evidence": [str(e) for e in rc.get("evidence", [])],
                "confidence": confidence, "action": {"name": rc["action"], "params": params}, "source": "claude",
            })
        return {"summary": raw.get("summary", ""), "hypotheses": hyps, "ruled_out": raw.get("ruled_out", [])}

    async def review(self, inc, hyps: list[dict], emit) -> dict[str, dict]:
        if not self.available:
            raise EngineError("no API key configured")
        payload = [{k: h[k] for k in ("id", "node", "category", "title", "explanation", "evidence", "action")}
                   for h in hyps]
        messages = [{"role": "user", "content": "Review these hypotheses before any action runs.\n\n"
                                                + json.dumps(payload, ensure_ascii=False)}]
        raw = await self._loop(inc, emit, SKEPTIC_SYSTEM, messages, SUBMIT_REVIEW, 6, "low", "skeptic")
        return {v["hypothesis_id"]: {"challenge": str(v.get("challenge", "")), "verdict": v["verdict"],
                                     "reasoning": str(v.get("reasoning", ""))}
                for v in raw.get("verdicts", [])}

    async def narrative(self, inc, facts: dict) -> dict | None:
        """Two short paragraphs for the postmortem, English and Arabic. Optional."""
        if not self.available:
            return None
        prompt = ("Write the summary section of a blameless postmortem from these facts. Return JSON with keys "
                  "'en' and 'ar' (Modern Standard Arabic), each one paragraph of at most 90 words, plain text.\n\n"
                  + json.dumps(facts, ensure_ascii=False, default=str))
        try:
            resp = await self._create("You write clear, blameless incident postmortems.", [],
                                      [{"role": "user", "content": prompt}], "low")
        except EngineError:
            return None
        self._account(inc, resp)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1])
            return {"en": str(data.get("en", "")), "ar": str(data.get("ar", ""))}
        except (ValueError, json.JSONDecodeError):
            return None


engine = ClaudeEngine()
