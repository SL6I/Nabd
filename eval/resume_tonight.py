"""Finish tonight's runs in order, surviving a closed terminal: the rest of an interrupted ablation,
the quick set on the fixed version, then the blind faults. Writes eval/results/*.json/.md like
run_all.py and a progress log at eval/results/tonight.log.

    python eval/resume_tonight.py eval/results/ablation-partial-29.json
"""

import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "results", "tonight.log")


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%H:%M:%S ") + msg + "\n")


def run(c: httpx.Client, scenarios: list[str], variants: list[str]) -> dict:
    while c.get("/api/benchmark").json()["status"]["running"]:
        time.sleep(20)
    resp = c.post("/api/benchmark/start", json={"scenarios": scenarios, "runs": 1, "variants": variants})
    if resp.status_code != 200:
        log(f"could not start: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    log(f"started {resp.json()['status']['total']} runs: {variants} x {scenarios}")
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
            log(f"  {s['done']}/{s['total']} {cur.get('engine', '')} {cur.get('scenario', '')}")
        if not s["running"]:
            return b["results"]


def save(name: str, results: dict) -> None:
    base = os.path.join(HERE, "results", f"{name}-{time.strftime('%Y%m%d-%H%M')}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(run_all.report(name, results))
    log(f"saved {base}.json and .md")


def main() -> None:
    partial = json.load(open(sys.argv[1], encoding="utf-8"))
    c = httpx.Client(base_url=run_all.BASE, timeout=30)
    run_all.wait_ready(c)
    for _ in range(60):  # the learned model reloads from disk; wait until it is judging again
        learned = (c.get("/api/state").json().get("telemetry") or {}).get("learned") or {}
        if learned.get("state") == "watching":
            break
        time.sleep(10)
    c.post("/api/settings", json={"engine": "core", "mode": "autopilot",
                                  "business_invariants": True, "learned_invariants": True})
    spec = run_all.SETS["ablation"]
    done = {(r.get("variant"), r.get("scenario")) for r in partial["rows"]}
    rest = [s for s in spec["scenarios"] if ("baseline", s) not in done]
    if rest:
        more = run(c, rest, ["baseline"])
        merged = {**more, "rows": partial["rows"] + more["rows"]}
        save("ablation", merged)
    save("quick", run(c, run_all.SETS["quick"]["scenarios"], ["nabd"]))
    save("blind", run(c, ["blind"], run_all.SETS["blind"]["variants"]))
    log("all done")


if __name__ == "__main__":
    main()
