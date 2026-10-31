"""Score agent runs on the pre-registered cases with Cloud-OpsBench's own
evaluation code, and add what the benchmark doesn't report: model calls,
tokens and skeptic verdicts.

    python score.py reference nabd_v2 reference_r2

The first run named is the baseline for the paired comparison. A case without
a trace counts as missing; a trace with no accepted answer counts as wrong.
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = ROOT / "cloud-opsbench"
sys.path.insert(0, str(BENCH_ROOT / "agents" / "cloudops_agent"))

from evaluation import evaluate_case  # noqa: E402

MODEL = "deepseek-flash"
METRICS = ["CA", "FA", "JRA", "MC", "EOC", "ECR", "EE", "steps", "RAR", "invalid_actions"]
MISSING = {"missing_process_label", "missing_agent_case_json"}


def usage(trace_dir: Path, case_id: str) -> dict:
    trace = json.loads((trace_dir / f"{case_id}.json").read_text(encoding="utf-8"))
    steps = trace.get("steps", [])
    out = {"llm_calls": len(steps), "tokens_in": sum(s.get("input_tokens") or 0 for s in steps),
           "tokens_out": sum(s.get("output_tokens") or 0 for s in steps), "verdicts": []}
    review_path = trace_dir / "nabd_review.json"
    if review_path.exists():
        extra = json.loads(review_path.read_text(encoding="utf-8"))
        for review in extra["reviews"]:
            out["llm_calls"] += max(1, len(review.get("raw", [])))
            out["tokens_in"] += review.get("input_tokens") or 0
            out["tokens_out"] += review.get("output_tokens") or 0
            out["verdicts"].append(review.get("verdict"))
        out["gate"] = len(extra["gate_rejections"])
    return out


def score_run(tag: str, cases: list[dict]) -> dict:
    rows = []
    for item in cases:
        label_root = BENCH_ROOT / "process-label" / item["system"] / item["category"]
        agent_root = ROOT / "runs" / tag / item["system"] / MODEL / item["category"]
        detail = evaluate_case(item["case"], label_root, agent_root)
        row = {"case": f"{item['category']}/{item['case']}", **detail["metrics"],
               "missing": detail.get("error") in MISSING, "no_answer": detail.get("error") == "unparsed_final_answer"}
        if not row["missing"]:
            row.update(usage(agent_root / item["case"], item["case"]))
        rows.append(row)
    done = [r for r in rows if not r["missing"]]
    summary = {m: round(statistics.mean(r[m] for r in done), 3) for m in METRICS}
    summary.update(
        cases_scored=len(done), correct=int(sum(r["JRA"] for r in done)), no_answer=sum(r["no_answer"] for r in done),
        llm_calls_mean=round(statistics.mean(r["llm_calls"] for r in done), 1),
        tokens_in_mean=round(statistics.mean(r["tokens_in"] for r in done)),
        tokens_out_mean=round(statistics.mean(r["tokens_out"] for r in done)),
        verdicts=dict(collections.Counter(v for r in done for v in r.get("verdicts", []))),
        gate_rejections=sum(r.get("gate", 0) for r in done),
    )
    return {"summary": summary, "rows": rows}


def main() -> None:
    tags = sys.argv[1:] or ["reference", "nabd"]
    cases = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))["cases"]
    report = {tag: score_run(tag, cases) for tag in tags}
    base = {r["case"]: r for r in report[tags[0]]["rows"] if not r["missing"]}
    report["paired_vs_" + tags[0]] = {}
    for tag in tags[1:]:
        other = {r["case"]: r for r in report[tag]["rows"] if not r["missing"]}
        both = sorted(set(base) & set(other))
        report["paired_vs_" + tags[0]][tag] = {
            "cases_in_both": len(both),
            "only_this_right": [c for c in both if other[c]["JRA"] > base[c]["JRA"]],
            "only_baseline_right": [c for c in both if base[c]["JRA"] > other[c]["JRA"]],
            "ECR_gained": [c for c in both if other[c]["ECR"] > base[c]["ECR"]],
            "ECR_lost": [c for c in both if base[c]["ECR"] > other[c]["ECR"]],
        }
    out = ROOT / "runs" / f"score-{'-'.join(tags)}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for tag in tags:
        print(tag, json.dumps(report[tag]["summary"]))
    print(json.dumps(report["paired_vs_" + tags[0]], indent=1))


if __name__ == "__main__":
    main()
