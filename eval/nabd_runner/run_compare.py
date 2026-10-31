"""Run Cloud-OpsBench's reference ReAct agent and a Nabd-method variant on the
same pre-registered cases, with the same model, tools, prompt and step budget.

The Nabd variant changes exactly two things, both taken from Nabd:
  1. Validity gate on the diagnosis: every prediction must name a root cause from
     the benchmark's list, and the fault object's kind must be the kind that root
     cause requires (NODE / APP / NAMESPACE) with a valid name. A mismatch is sent
     back to the agent to fix, like Nabd's gate refuses an action that cannot
     undo the diagnosed cause on the diagnosed component.
  2. Skeptic review: before a submission is accepted, a separate model call tries
     to refute rank 1 from the evidence trail. If it is refuted or a key check is
     missing, the agent gets the critique and keeps investigating with its own
     tool steps (counted against the same budget). At most two reviews per case.

    python run_compare.py --agent reference --workers 4
    python run_compare.py --agent nabd --workers 4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = ROOT / "cloud-opsbench"
AGENT_DIR = BENCH_ROOT / "agents" / "cloudops_agent"
sys.path.insert(0, str(AGENT_DIR))

from harness.context import ContextBuilder  # noqa: E402
from harness.harness import CloudOpsHarness  # noqa: E402
from runtime.contracts import (SYSTEM_VALID_NAMESPACES, SYSTEM_VALID_SERVICES, VALID_NODES,  # noqa: E402
                               build_expected_output, root_cause_list_str)
from runtime.core import OutputParser, StepRecord, ToolExecutor, TraceLogger, init_case_state  # noqa: E402
from runtime.llm import ModelRunner  # noqa: E402
from tools.cloudops import build_tool_registry, create_k8s_tools, render_tools_description  # noqa: E402

TOOL_SYSTEM = {"boutique": "boutique", "trainticket": "train-ticket"}
REQUIRED_KIND = {m.group(1): m.group(2).lower()
                 for m in re.finditer(r"- (\w+) \(Requires Target: (\w+)\)", root_cause_list_str)}


def load_key() -> str:
    for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("NABD_LLM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("NABD_LLM_API_KEY not found in Nabd/.env")


# ---------------------------------------------------------------- Nabd: validity gate

def validity_problems(payload: dict, tool_system: str) -> list[str]:
    valid_names = {"node": set(VALID_NODES), "app": set(SYSTEM_VALID_SERVICES[tool_system]),
                   "namespace": set(SYSTEM_VALID_NAMESPACES[tool_system])}
    problems = []
    for pred in payload.get("top_3_predictions", []):
        rank, cause, obj = pred.get("rank"), str(pred.get("root_cause", "")), str(pred.get("fault_object", ""))
        if cause not in REQUIRED_KIND:
            problems.append(f"rank {rank}: '{cause}' is not in List A")
            continue
        kind, _, name = obj.partition("/")
        need = REQUIRED_KIND[cause]
        if kind.lower() != need:
            problems.append(f"rank {rank}: {cause} requires a {need.upper()} target, but fault_object is '{obj}'")
        elif name not in valid_names[need]:
            problems.append(f"rank {rank}: '{name}' is not a valid {need} name from List B")
    return problems


# ---------------------------------------------------------------- Nabd: skeptic

SKEPTIC_PROMPT = """You are the skeptic on an SRE incident team. Another engineer investigated a Kubernetes
incident with read-only diagnostic tools and wants to submit the diagnosis below. Before it is accepted,
try to refute rank 1 using only the evidence trail.

Rules:
- Name the strongest alternative explanation (root cause + object) that is consistent with the evidence.
- "upheld": tool output directly supports rank 1 and contradicts the alternative.
- "refuted": tool output contradicts rank 1.
- "uncertain": a decisive check is missing. Then name exactly one tool call that would settle it.
- Be strict but fair: do not ask for checks that the evidence already answers.

Valid root causes and the target kind each requires:
{causes}

Question: {question}

Evidence trail:
{trail}

Proposed diagnosis:
{proposal}

Think briefly, then end your reply with one JSON object:
{{"verdict": "upheld|refuted|uncertain", "alternative": "<cause on kind/name>", "reasoning": "<two sentences>",
 "suggested_check": "<ToolName {{json input}}> or empty"}}"""
RETRY_SUFFIX = "\n\nYour previous reply could not be parsed. Reply with the JSON object only, no other text."


def evidence_trail(state, limit: int = 1800) -> str:
    lines = []
    for step in state.history:
        if step.action_type not in ("tool", "submit"):
            continue
        obs = (step.observation or step.error or "")
        obs = obs if len(obs) <= limit else obs[:limit] + " …(truncated)"
        lines.append(f"[{step.step_id}] {step.action_name} {json.dumps(step.action_input, ensure_ascii=False)}\n"
                     f"thought: {step.thought or ''}\nobservation: {obs}")
    return "\n\n".join(lines) or "(no tool calls yet)"


def parse_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class NabdHarness(CloudOpsHarness):
    """Cloud-OpsBench's ReAct loop plus Nabd's validity gate and skeptic review on Submit."""

    def __init__(self, *, skeptic: ModelRunner, tool_system: str, max_reviews: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.skeptic = skeptic
        self.tool_system = tool_system
        self.max_reviews = max_reviews
        self.reviews: list[dict] = []
        self.gate_rejections: list[str] = []

    def _run_step(self, state) -> StepRecord:
        step = super()._run_step(state)
        if step.action_type != "submit" or step.error:
            return step
        payload = step.action_input or {}

        problems = validity_problems(payload, self.tool_system)
        if problems:
            self.gate_rejections.append("; ".join(problems))
            return self._reject(step, "Validity gate: " + "; ".join(problems)
                                + ". Fix the diagnosis so each root cause targets the right kind, then Submit again.")

        steps_left = state.max_steps - (state.current_step + 1)
        if len(self.reviews) >= self.max_reviews or steps_left < 2:
            return step
        rank1 = (payload.get("top_3_predictions") or [{}])[0]
        prompt = SKEPTIC_PROMPT.format(
            causes=root_cause_list_str.strip(), question=state.question, trail=evidence_trail(state),
            proposal=json.dumps({"rank_1": rank1, "evidence_summary": payload.get("key_evidence_summary")},
                                ensure_ascii=False))
        verdict, tokens_in, tokens_out, raw = None, 0, 0, []
        for attempt in (prompt, prompt + RETRY_SUFFIX):
            try:
                generated = self.skeptic.generate(attempt)
            except Exception as exc:  # a failed review never blocks the agent
                raw.append(f"error: {exc}")
                continue
            tokens_in += generated.get("input_tokens") or 0
            tokens_out += generated.get("output_tokens") or 0
            raw.append(generated.get("text", "")[-3000:])
            verdict = parse_json(generated.get("text", ""))
            if verdict and verdict.get("verdict") in ("upheld", "refuted", "uncertain"):
                break
            verdict = None
        # an unreadable review is recorded as such and the submission stands; it is never counted as "upheld"
        verdict = verdict or {"verdict": "review_failed", "reasoning": "no parseable verdict after a retry"}
        verdict.update(input_tokens=tokens_in, output_tokens=tokens_out, raw=raw)
        verdict["at_step"] = step.step_id
        verdict["rank_1"] = rank1
        self.reviews.append(verdict)
        if verdict.get("verdict") in ("refuted", "uncertain"):
            check = verdict.get("suggested_check") or ""
            return self._reject(step, f"Skeptic review ({verdict.get('verdict')}): {verdict.get('reasoning', '')} "
                                      f"Strongest alternative: {verdict.get('alternative', '')}. "
                                      + (f"Suggested check: {check}. " if check else "")
                                      + "Investigate further, then Submit again.")
        return step

    @staticmethod
    def _reject(step: StepRecord, message: str) -> StepRecord:
        step.error = message
        step.final_answer = None
        step.observation = json.dumps({"error": message}, ensure_ascii=False)
        return step


# ---------------------------------------------------------------- runner

def run_case(agent: str, tag: str, item: dict, model: str, runner: ModelRunner, skeptic: ModelRunner,
             out_root: Path, max_steps: int) -> dict:
    system, category, case_id = item["system"], item["category"], item["case"]
    case_path = BENCH_ROOT / "benchmark" / system / category / case_id
    trace_dir = out_root / tag / system / model / category / case_id
    trace_path = trace_dir / f"{case_id}.json"
    if trace_path.exists() and json.loads(trace_path.read_text(encoding="utf-8")).get("stop_reason"):
        return {"case": f"{category}/{case_id}", "skipped": True}

    metadata = json.loads((case_path / "metadata.json").read_text(encoding="utf-8"))
    namespace, query = metadata.get("namespace") or system, metadata.get("query") or ""  # result is never read
    tool_system = TOOL_SYSTEM[system]
    registry = build_tool_registry(create_k8s_tools(str(case_path), system=tool_system, fault_category=category))
    common = dict(
        context_builder=ContextBuilder(tools_description=render_tools_description(registry),
                                       expected_output=build_expected_output(tool_system)),
        model_runner=runner, output_parser=OutputParser(), tool_executor=ToolExecutor(registry),
        trace_logger=TraceLogger(trace_dir))
    harness = (NabdHarness(skeptic=skeptic, tool_system=tool_system, **common) if agent == "nabd"
               else CloudOpsHarness(**common))
    state = init_case_state(
        case_id=case_id, system_name=system,
        question=(f"The Kubernetes environment in namespace `{namespace}` is experiencing a fault. "
                  f"A high-level symptom has been reported: '{query}'. Diagnose the root cause of this incident."),
        case_path=str(case_path), max_steps=max_steps,
        metadata={"namespace": namespace, "query": query, "fault_category": category, "model_name": model})
    started = time.time()
    final = harness.run_case(state)
    extra = {}
    if agent == "nabd":
        extra = {"reviews": harness.reviews, "gate_rejections": harness.gate_rejections}
        (trace_dir / "nabd_review.json").write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"case": f"{category}/{case_id}", "finished": final.finished, "steps": final.current_step,
            "seconds": round(time.time() - started, 1),
            "reviews": len(extra.get("reviews", [])), "gate": len(extra.get("gate_rejections", []))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["reference", "nabd"], required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--api-base", default="https://api.deepseek.com")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="only the first N cases (smoke test)")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--out", default=str(ROOT / "runs"))
    parser.add_argument("--tag", default="", help="output folder name; defaults to the agent name")
    args = parser.parse_args()
    tag = args.tag or args.agent

    cases = json.loads((Path(__file__).with_name("cases.json")).read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[:args.limit]
    # identical settings to the benchmark's default config: temperature 0, 4096 max tokens
    runner = ModelRunner(model_name=args.model, provider="openai_compatible", api_base=args.api_base,
                         api_key=load_key(), temperature=0.0, max_tokens=4096, timeout=120)
    # the skeptic needs room: the model's own reasoning counts against max_tokens before the verdict
    skeptic = ModelRunner(model_name=args.model, provider="openai_compatible", api_base=args.api_base,
                          api_key=load_key(), temperature=0.0, max_tokens=12000, timeout=180)
    out_root = Path(args.out)
    print(f"[PLAN] agent={args.agent} tag={tag} model={args.model} cases={len(cases)} workers={args.workers}", flush=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, args.agent, tag, c, args.model, runner, skeptic, out_root, args.max_steps): c
                   for c in cases}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"case": f"{item['category']}/{item['case']}", "error": f"{type(exc).__name__}: {exc}"}
            with lock:
                print("[DONE]", json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
