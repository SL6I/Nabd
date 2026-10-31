"""Benchmark: inject each scenario for real, let Nabd handle it end to end,
and score detection, root-cause accuracy, recovery, MTTR, wrong actions and
cost against the ground truth. The baseline is the classic runbook:
restart every alerting service."""

import asyncio
import json
import os
import statistics
import time

from . import verify
from .agent import agent
from .bus import bus
from .chaos import BENIGN, FAULTS, HELD_OUT, KNOWN, SCENARIOS, SILENT, chaos, load_blind
from .learned import learner
from .mizan import mizan
from .settings import DATA_DIR, runtime
from .telemetry import telemetry

_PATH = os.path.join(DATA_DIR, "benchmark.json")
DEFAULT_SET = KNOWN + list(SCENARIOS) + HELD_OUT + SILENT + BENIGN
GROUPS = ("known", "held_out", "silent", "benign", "blind")
QUIET_WAIT_S = 200  # a benign run passes if no incident opens in this long
# Ablation: the same agent with parts of Mizan switched off. "baseline" is the restart-everything runbook.
VARIANTS = {
    "nabd": {"business_invariants": True, "learned_invariants": True},
    "nabd-no-learned": {"business_invariants": True, "learned_invariants": False},
    "nabd-no-books": {"business_invariants": False, "learned_invariants": True},
    "nabd-no-mizan": {"business_invariants": False, "learned_invariants": False},
    "baseline": {"business_invariants": True, "learned_invariants": True},
}
# Mizan's repairs only ever touch records the books proved broken (their dry run refuses otherwise),
# so they are scored as repairs, not as wrong actions
REPAIRS = {"replay_orphan_payments", "refund_failed_payments", "replay_lost_events", "restore_catalog_prices"}


def _action_key(a: dict) -> str:
    p = a.get("params", {})
    return f"{a['name']}:{p.get('service') or p.get('application_name') or ''}".rstrip(":")


def score(summary: dict, truths: list[dict], started: float) -> dict:
    kept = [h for h in summary["hypotheses"] if h.get("kept", True)]
    matched = sum(1 for t in truths if any(h["node"] == t["node"] and h["category"] == t["category"] for h in kept))
    fixes = {f for t in truths for f in t["fixes"]}
    done = [a for a in summary["actions"] if a["status"] in ("done", "reverted")]
    repairs = [a for a in done if a["name"] in REPAIRS]
    wrong = [a for a in done if a not in repairs and _action_key(a) not in fixes and a["name"] not in fixes]
    books = (mizan.latest or {}).get("invariants", {})
    recovered = summary["status"] == "resolved"
    # a fault nobody inside Dukkan can fix is handled correctly by escalating without touching anything
    if truths and all(t.get("expect") == "escalate" for t in truths):
        outcome_ok = summary["status"] == "escalated" and not wrong
    else:
        outcome_ok = recovered
    return {
        "detected": summary["opened_at"] >= started,
        "detect_s": round(summary["opened_at"] - started, 1),
        "rca_correct": matched == len(truths),
        "rca_fraction": round(matched / len(truths), 2) if truths else None,
        "recovered": recovered,
        "outcome_ok": outcome_ok,
        "status": summary["status"],
        "gated": sum(1 for a in summary["actions"] if a["status"] == "blocked" or a.get("corrected_from")),
        "mttr_s": summary["mttr_s"],
        "actions": len(done),
        "wrong_actions": len(wrong),
        "wrong_detail": [a["description"] for a in wrong],
        "repairs": [f"{a['name']}: {a.get('count') or 0}" + (f" ({round(a['amount_sar'])} SAR)" if a.get("amount_sar") else "")
                    for a in repairs],
        "repaired_items": sum(a.get("count") or 0 for a in repairs),
        "handoffs": len(summary.get("handoffs") or []),
        # the books when the incident closed: what a "recovered" store still owed its customers
        "books_balanced": all(v.get("ok") is not False for v in books.values()),
        "money_left_sar": (books.get("money") or {}).get("amount_sar") or 0,
        "route": summary["route"],
        "llm_calls": summary["llm_calls"],
        "tokens": summary["tokens"],
        "cost_usd": summary["cost"],
        "rounds": summary["iterations"] + 1,
        "incident": summary["id"],
    }


def aggregate(rows: list[dict]) -> dict:
    benign = [r for r in rows if r.get("group") == "benign"]
    rows = [r for r in rows if r.get("group") != "benign"]
    if not rows:
        return {"runs": 0, "benign_runs": len(benign), "false_alarms": sum(1 for r in benign if not r["outcome_ok"])}
    mttrs = [r["mttr_s"] for r in rows if r.get("mttr_s")]
    return {
        "runs": len(rows),
        "rca_accuracy": round(sum(r["rca_correct"] for r in rows) / len(rows), 3),
        "recovery_rate": round(sum(r["recovered"] for r in rows) / len(rows), 3),
        "outcome_rate": round(sum(r.get("outcome_ok", r["recovered"]) for r in rows) / len(rows), 3),
        "gated_actions": sum(r.get("gated", 0) for r in rows),
        "median_mttr_s": round(statistics.median(mttrs), 1) if mttrs else None,
        "median_detect_s": round(statistics.median([r["detect_s"] for r in rows if r["detected"]] or [0]), 1),
        "wrong_actions": sum(r["wrong_actions"] for r in rows),
        "books_balanced_rate": round(sum(r.get("books_balanced", True) for r in rows) / len(rows), 3),
        "money_left_sar": round(sum(r.get("money_left_sar") or 0 for r in rows), 2),
        "repaired_items": sum(r.get("repaired_items") or 0 for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "benign_runs": len(benign),
        "false_alarms": sum(1 for r in benign if not r["outcome_ok"]),
    }


class Benchmark:
    def __init__(self) -> None:
        self.status = {"running": False, "current": None, "done": 0, "total": 0, "started_at": None}
        self.results = self._load()
        self._task: asyncio.Task | None = None

    def _load(self) -> dict:
        try:
            with open(_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"rows": [], "summary": {}, "finished_at": None}

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as fh:
            json.dump(self.results, fh, ensure_ascii=False, indent=1)

    def view(self) -> dict:
        return {"status": self.status, "results": self.results, "catalog": DEFAULT_SET}

    def start(self, scenarios: list[str] | None, runs: int, baseline: bool, variants: list[str] | None = None) -> bool:
        if self.status["running"]:
            return False
        blind = load_blind()
        items = [s for s in (scenarios or DEFAULT_SET) for s in ((blind if s == "blind" else [s]))
                 if s in FAULTS or s in SCENARIOS]
        engines = [v for v in (variants or []) if v in VARIANTS] or (["nabd"] + (["baseline"] if baseline else []))
        plan = [(e, s, r) for e in engines for r in range(max(1, min(runs, 5))) for s in items]
        self.results = {"rows": [], "summary": {}, "finished_at": None, "plan": len(plan)}
        self.status = {"running": True, "current": None, "done": 0, "total": len(plan), "started_at": time.time()}
        self._task = asyncio.create_task(self._run(plan))
        return True

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _wait_healthy(self, timeout_s: int = 300) -> bool:
        deadline = time.time() + timeout_s
        streak = 0
        while time.time() < deadline:
            snap = telemetry.latest or {}
            backlog = ((mizan.latest or {}).get("invariants", {}).get("progress") or {}).get("pending") or 0
            # a backlog left by the previous run would delay this run's fault, so start from a drained queue
            healthy = (not snap.get("alerts") and all(v == "ok" for v in snap.get("health", {}).values())
                       and agent.incident is None and backlog < 20
                       and not any(law.status in ("broken", "stalled") for law in learner.laws))
            if healthy and (await verify.journey())["ok"]:
                streak += 1
                if streak >= 3:
                    return True
            else:
                streak = 0
            await asyncio.sleep(2)
        return False

    async def _wait_idle(self, timeout_s: int = 900) -> None:
        """Never inject while Nabd is still busy with an earlier incident."""
        deadline = time.time() + timeout_s
        while agent.incident is not None and time.time() < deadline:
            await asyncio.sleep(2)

    async def _await_incident(self, started: float, timeout_s: int = 900) -> dict | None:
        """The incident this run caused, or None if none opened within timeout_s."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for summary in reversed(agent.closed):
                if summary["opened_at"] >= started - 1:
                    return summary
            await asyncio.sleep(2)
        return None

    async def _run(self, plan: list[tuple[str, str, int]]) -> None:
        previous_engine = runtime.engine
        previous_flags = {k: getattr(runtime, k) for k in VARIANTS["nabd"]}
        runtime.auto_approve = True
        try:
            for engine_label, item, run in plan:
                self.status["current"] = {"engine": engine_label, "scenario": item, "run": run + 1}
                bus.publish("benchmark", status=self.status, title=f"Benchmark: {engine_label} · {item} · run {run + 1}")
                runtime.engine = "baseline" if engine_label == "baseline" else previous_engine
                for key, value in VARIANTS.get(engine_label, VARIANTS["nabd"]).items():
                    setattr(runtime, key, value)
                await self._wait_idle()
                await chaos.reset(announce=False)
                if not await self._wait_healthy():
                    await chaos.reset(announce=False)  # one more clean slate before giving up on this run
                    await self._wait_idle()
                    await self._wait_healthy()
                started = time.time()
                if item in SCENARIOS:
                    await chaos.scenario(item)
                else:
                    await chaos.inject(item)
                truths = chaos.truths()
                quiet = bool(truths) and all(t.get("expect") == "quiet" for t in truths)
                silent = bool(truths) and all(t["silent"] or t.get("blind") for t in truths)
                # a silent fault nobody notices within five minutes is a miss, not a slow detection
                summary = await self._await_incident(started, QUIET_WAIT_S if quiet else 300 if silent else 900)
                row = {"engine": engine_label, "scenario": item, "run": run + 1,
                       "faults": [t["fault"] for t in truths],
                       "group": "benign" if quiet else
                       "blind" if truths and all(t.get("blind") for t in truths) else
                       "held_out" if truths and all(t["held_out"] for t in truths) else
                       "silent" if truths and all(t["silent"] for t in truths) else "known"}
                if quiet:
                    # nothing is broken: the only right outcome is that no incident opens at all
                    row.update({"detected": summary is not None, "rca_correct": summary is None,
                                "recovered": summary is None, "outcome_ok": summary is None,
                                "status": "quiet" if summary is None else f"false alarm ({summary['status']})",
                                "mttr_s": None, "detect_s": None, "wrong_actions": len([
                                    a for a in (summary or {}).get("actions", []) if a["status"] == "done"]),
                                "cost_usd": (summary or {}).get("cost", 0), "llm_calls": (summary or {}).get("llm_calls", 0),
                                "actions": 0, "gated": 0, "route": (summary or {}).get("route"), "tokens": {},
                                "rounds": 0, "incident": (summary or {}).get("id"), "wrong_detail": [],
                                "books_balanced": True, "money_left_sar": 0})
                    if summary is not None:
                        await self._wait_idle()
                elif summary is None:
                    row.update({"detected": False, "rca_correct": False, "recovered": False, "outcome_ok": False,
                                "mttr_s": None, "detect_s": None, "wrong_actions": 0, "cost_usd": 0, "llm_calls": 0,
                                "actions": 0, "gated": 0, "route": None, "tokens": {}, "rounds": 0, "incident": None,
                                "wrong_detail": [], "status": "missed", "books_balanced": False,
                                "money_left_sar": ((mizan.latest or {}).get("invariants", {}).get("money") or {}).get(
                                    "amount_sar") or 0})
                else:
                    row.update(score(summary, truths, started))
                self.results["rows"].append(row)
                row["variant"] = engine_label
                rows = self.results["rows"]
                summary_by = {}
                for e in {r["engine"] for r in rows}:
                    summary_by[e] = aggregate([r for r in rows if r["engine"] == e])
                    for group in GROUPS:
                        part = [r for r in rows if r["engine"] == e and r.get("group", "known") == group]
                        if part:
                            summary_by[f"{e}:{group}"] = aggregate(part)
                self.results["summary"] = summary_by
                self.status["done"] += 1
                self._save()
                bus.publish("benchmark", status=self.status, row=row, summary=self.results["summary"],
                            title=f"Benchmark {self.status['done']}/{self.status['total']}: {item} → "
                                  f"{row['status']}{'' if row['outcome_ok'] else ' (wrong outcome)'}")
        except asyncio.CancelledError:
            pass
        finally:
            runtime.auto_approve = False
            runtime.engine = previous_engine
            for key, value in previous_flags.items():
                setattr(runtime, key, value)
            await chaos.reset(announce=False)
            self.status["running"] = False
            self.status["current"] = None
            self.results["finished_at"] = time.time()
            self._save()
            bus.publish("benchmark", status=self.status, summary=self.results.get("summary"),
                        title="Benchmark finished")


benchmark = Benchmark()
