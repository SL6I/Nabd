"""One command for every number in the README.

    python eval/run_all.py --set ablation     # silent faults + a benign flash sale, five variants (~2 h)
    python eval/run_all.py --set quick        # the same faults, full Nabd only (~25 min)
    python eval/run_all.py --set blind        # the teammate's blind faults, Nabd vs the runbook
    python eval/run_all.py --set full         # every fault and scenario, Nabd vs the runbook (~3 h)

Needs the stack running (docker compose up -d --build) and Mizan's learned laws in place (the War
Room's band shows "Laws it learned itself"). Writes eval/results/<set>-<time>.json and .md."""

import argparse
import json
import os
import statistics
import sys
import time

import httpx

BASE = os.getenv("NABD_URL", "http://localhost:7000")
SILENT = ["silent_order_drop", "queue_drop_under_load", "workflow_hang", "catalog_price_drift", "stock_not_decremented"]
SETS = {
    "ablation": {"scenarios": SILENT + ["flash_sale", "payments_bad_config"],
                 "variants": ["nabd", "nabd-no-learned", "nabd-no-books", "nabd-no-mizan", "baseline"]},
    "quick": {"scenarios": SILENT + ["flash_sale"], "variants": ["nabd"]},
    "blind": {"scenarios": ["blind"], "variants": ["nabd", "baseline"]},
    "full": {"scenarios": None, "variants": ["nabd", "baseline"]},
    # after the blind run: the two blind faults Nabd missed, re-run once the gaps were closed (no longer
    # blind), plus the quick set to check nothing else moved
    "retest": {"scenarios": ["blind_2", "blind_3"] + SILENT + ["flash_sale"], "variants": ["nabd"]},
}
LABEL = {"nabd": "Nabd (full)", "nabd-no-learned": "without learned laws", "nabd-no-books": "without hand-written books",
         "nabd-no-mizan": "without Mizan", "baseline": "restart runbook"}


def wait_ready(c: httpx.Client) -> dict:
    for _ in range(90):
        try:
            st = c.get("/api/state").json()
            return st
        except httpx.HTTPError:
            time.sleep(2)
    sys.exit("Nabd is not answering on " + BASE)


def report(name: str, results: dict) -> str:
    rows = results["rows"]
    variants = list(dict.fromkeys(r.get("variant", r["engine"]) for r in rows))
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows))
    out = [f"# Nabd evaluation: {name}", "",
           f"{len(rows)} runs, finished {time.strftime('%Y-%m-%d %H:%M', time.localtime(results.get('finished_at') or time.time()))}.",
           "Every run injects a real fault into the live store, lets the agent handle it end to end with approvals "
           "granted automatically, scores it against the ground truth, and resets.", ""]
    out += ["| | " + " | ".join(LABEL.get(v, v) for v in variants) + " |", "|---|" + "---|" * len(variants)]

    def per(v, pred):
        return [r for r in rows if r.get("variant", r["engine"]) == v and pred(r)]

    def line(title, fn):
        out.append(f"| {title} | " + " | ".join(fn(v) for v in variants) + " |")

    faulty = lambda r: r.get("group") != "benign"  # noqa: E731
    line("Correct outcome", lambda v: f"{sum(r['outcome_ok'] for r in per(v, faulty))} / {len(per(v, faulty))}")
    line("Root cause right", lambda v: f"{sum(r['rca_correct'] for r in per(v, faulty))} / {len(per(v, faulty))}")
    line("Detected", lambda v: f"{sum(bool(r['detected']) for r in per(v, faulty))} / {len(per(v, faulty))}")

    def med(v, key):
        vals = [r[key] for r in per(v, faulty) if r.get(key)]
        return f"{statistics.median(vals):.0f} s" if vals else "n/a"
    line("Median time to detect", lambda v: med(v, "detect_s"))
    line("Median time to recover", lambda v: med(v, "mttr_s"))
    line("Wrong actions", lambda v: str(sum(r.get("wrong_actions", 0) for r in per(v, faulty))))
    line("Books balanced at the end", lambda v: f"{sum(bool(r.get('books_balanced')) for r in per(v, faulty))} / {len(per(v, faulty))}")
    line("SAR left charged with no order", lambda v: f"{sum(r.get('money_left_sar') or 0 for r in per(v, faulty)):,.0f}")
    line("False alarms on a benign flash sale", lambda v: (
        f"{sum(not r['outcome_ok'] for r in per(v, lambda r: r.get('group') == 'benign'))} / "
        f"{len(per(v, lambda r: r.get('group') == 'benign'))}"))
    out += ["", "## Run by run", "", "| fault | " + " | ".join(LABEL.get(v, v) for v in variants) + " |",
            "|---|" + "---|" * len(variants)]
    for s in scenarios:
        cells = []
        for v in variants:
            rs = per(v, lambda r, s=s: r["scenario"] == s)
            if not rs:
                cells.append("–")
                continue
            r = rs[-1]
            mark = "✓" if r["outcome_ok"] else "✗"
            extra = f" {r['mttr_s']:.0f} s" if r.get("mttr_s") else f" ({r.get('status')})"
            cells.append(mark + extra)
        out.append(f"| {s} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="quick", choices=SETS)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--engine", default="core", help="core (no tokens) or auto")
    ap.add_argument("--attach", action="store_true", help="wait for a benchmark that is already running")
    args = ap.parse_args()
    spec = SETS[args.set]
    c = httpx.Client(base_url=BASE, timeout=30)
    if args.attach:
        return finish(c, args.set)
    st = wait_ready(c)
    learned = (st.get("telemetry") or {}).get("learned") or {}
    if learned.get("state") != "watching":
        sys.exit("Mizan is still learning the store's laws; run again when the band shows them.")
    c.post("/api/settings", json={"engine": args.engine, "mode": "autopilot",
                                  "business_invariants": True, "learned_invariants": True})
    resp = c.post("/api/benchmark/start", json={"scenarios": spec["scenarios"], "runs": args.runs,
                                                "variants": spec["variants"]})
    if resp.status_code != 200:
        sys.exit(f"could not start: {resp.status_code} {resp.text[:200]}")
    total = resp.json()["status"]["total"]
    print(f"{args.set}: {total} runs started", flush=True)
    finish(c, args.set)


def finish(c: httpx.Client, set_name: str) -> None:
    last = -1
    while True:
        time.sleep(20)
        try:
            b = c.get("/api/benchmark").json()
        except httpx.HTTPError:
            continue
        s = b["status"]
        if s["done"] != last:
            last = s["done"]
            cur = s.get("current") or {}
            print(f"  {s['done']}/{s['total']}  {cur.get('engine', '')} {cur.get('scenario', '')}", flush=True)
        if not s["running"]:
            break
    results = b["results"]
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    base = os.path.join(os.path.dirname(__file__), "results", f"{set_name}-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    md = report(set_name, results)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    print(f"saved {base}.json and .md")


if __name__ == "__main__":
    main()
