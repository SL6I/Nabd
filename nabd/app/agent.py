"""The incident orchestrator: Detect → Triage → Investigate → Hypothesize →
Skeptic → Act → Verify → Report, looping back when verification fails."""

import asyncio
import json
import time
from datetime import datetime

from . import actions, core_engine, memory, postmortem, topology, verify
from .bus import bus
from .chaos import chaos
from .claude_engine import EngineError
from .claude_engine import engine as claude
from .learned import WINDOW_S, learner
from .settings import CLAUDE_AVAILABLE, LLM_NAME, runtime
from .telemetry import telemetry

STAGES = ["detect", "triage", "investigate", "hypothesize", "skeptic", "act", "verify", "report"]
TIMELINE_TYPES = {"incident_opened", "alert_storm", "router", "memory_hit", "hypothesis", "skeptic",
                  "action_result", "action_rejected", "action_blocked", "approval_result", "guard_reverted",
                  "recovered", "escalated", "retry", "transient", "action_corrected", "action_skipped",
                  "settle", "handoff", "guard_regression"}

# The escalation contract: how far Nabd may go on its own in one incident. Past any limit it stops
# acting and hands the incident to a human with everything it found, instead of trying one more thing.
BUDGET = {"actions": 6, "minutes": 15, "money_sar": 10000}
SETTLE_S = 10  # after fixing a cause, let in-flight orders land before repairing the books
OUTSIDE = {"dependency_failure", "silent_data_loss"}  # causes Nabd may diagnose but never fix


def _signatures(hyps: list[dict]) -> list[dict]:
    """Hypotheses backed by a real failure signature (not the 'unclassified' placeholder)."""
    return [h for h in hyps if h["category"] != "other"]


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Incident:
    def __init__(self, number: int, kind: str) -> None:
        self.id = f"INC-{number:04d}"
        self.kind = kind
        self.opened_at = time.time()
        self.started_at = self.opened_at
        self.resolved_at: float | None = None
        self.stage = "detect"
        self.status = "open"
        self.alerts: dict[str, dict] = {}
        self.truth: list[dict] = []
        self.hypotheses: list[dict] = []
        self.ruled_out: list[dict] = []
        self.actions: list[dict] = []
        self.verification: dict = {}
        self.route = "core"
        self.route_reason = ""
        self.tokens = {"input": 0, "output": 0, "cache_read": 0}
        self.cost = 0.0
        self.llm_calls = 0
        self.memory_hit: dict | None = None
        self.iterations = 0
        self.empty_rounds = 0
        self.claude_messages: list | None = None
        self.timeline: list[tuple[float, str]] = []
        self.signals: set[str] = set()
        self.used = {"actions": 0, "money_sar": 0.0}
        self.budget_spent: str | None = None
        self.handoffs: list[dict] = []
        self.last_change: float | None = None  # when Nabd last changed anything, fix or repair
        self.regression: str | None = None  # a fix broke a law that held before it

    def budget(self) -> dict:
        return {"limits": BUDGET, "actions": self.used["actions"], "money_sar": round(self.used["money_sar"], 2),
                "minutes": round((time.time() - self.opened_at) / 60, 1), "spent": self.budget_spent}

    def summary(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage,
            "opened_at": self.opened_at, "started_at": self.started_at, "resolved_at": self.resolved_at,
            "alerts": len(self.alerts), "route": self.route, "route_reason": self.route_reason,
            "hypotheses": [{k: h.get(k) for k in ("id", "title", "node", "category", "confidence", "kept", "source")}
                           for h in self.hypotheses],
            "actions": self.actions, "tokens": self.tokens, "cost": round(self.cost, 4),
            "llm_calls": self.llm_calls, "truth": self.truth, "iterations": self.iterations,
            "mttr_s": round(self.resolved_at - self.started_at, 1) if self.resolved_at and self.status == "resolved" else None,
            "detect_s": round(self.opened_at - self.started_at, 1),
            "memory_hit": self.memory_hit,
            "budget": self.budget(), "handoffs": self.handoffs,
        }


class Agent:
    def __init__(self) -> None:
        self.incident: Incident | None = None
        self.closed: list[dict] = []
        self.counter = 0
        self.cooldown_until = 0.0
        self.pending: dict[str, dict] = {}
        self.paused = False

    # ---------------------------------------------------------- plumbing

    def _emit(self, inc: Incident, type_: str, **data) -> dict:
        event = bus.publish(type_, incident=inc.id, stage=inc.stage, **data)
        if type_ in TIMELINE_TYPES:
            text = data.get("title") or data.get("text") or type_
            inc.timeline.append((event["ts"], str(text)))
        return event

    def _stage(self, inc: Incident, stage: str) -> None:
        inc.stage = stage
        self._emit(inc, "stage", stage_index=STAGES.index(stage), title=stage)

    def _merge_alerts(self, inc: Incident) -> int:
        added = 0
        for a in (telemetry.latest or {}).get("alerts", []):
            if a["key"] not in inc.alerts:
                inc.alerts[a["key"]] = a
                added += 1
        return added

    def state(self) -> dict:
        return {"incident": self.incident.summary() if self.incident else None,
                "closed": self.closed[-30:], "pending_approvals": list(self.pending)}

    # ---------------------------------------------------------- main loop

    async def run(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self.incident or self.paused or time.time() < self.cooldown_until:
                continue
            firing = (telemetry.latest or {}).get("alerts", [])
            if not firing:
                continue
            self.counter = max(self.counter, len(self.closed)) + 1
            kind = "predictive" if all(a["name"] == "MemoryExhaustionPredicted" for a in firing) else "reactive"
            inc = Incident(self.counter, kind)
            self.incident = inc
            asyncio.create_task(self._handle(inc))

    async def _handle(self, inc: Incident) -> None:
        emit = lambda t, **d: self._emit(inc, t, **d)  # noqa: E731
        recovered = False
        try:
            # ------------------------------------------------ detect
            self._merge_alerts(inc)
            inc.truth = chaos.truths()  # kept for scoring only; no engine reads it
            starts = [t["injected_at"] for t in inc.truth] + [x for x in (_parse_ts(a["active_at"])
                                                                        for a in inc.alerts.values()) if x]
            inc.started_at = min(starts) if starts else inc.opened_at
            emit("incident_opened", kind=inc.kind,
                 title=f"{inc.id} opened: {len(inc.alerts)} alerts" +
                       (" (predictive: nothing has failed yet)" if inc.kind == "predictive" else ""),
                 alerts=list(inc.alerts.values()))
            for _ in range(3):  # let the alert storm settle so related alerts land in one incident
                await asyncio.sleep(2)
                self._merge_alerts(inc)
            services = sorted({a["service"] for a in inc.alerts.values()})
            emit("alert_storm", count=len(inc.alerts), services=services, alerts=list(inc.alerts.values()),
                 title=f"Alert storm: {len(inc.alerts)} alerts across {len(services)} services")
            snap = telemetry.latest or {}
            if not snap.get("alerts") and all(v == "ok" for v in snap.get("health", {}).values()):
                ok, inc.verification = await verify.run(emit, timeout_s=20)
                if ok:
                    inc.status, inc.resolved_at = "transient", time.time()
                    emit("transient", title="The alerts cleared on their own and the store checks out: "
                                            "closed as transient, nothing changed")
                    return

            # ------------------------------------------------ triage
            self._stage(inc, "triage")
            health = (telemetry.latest or {}).get("health", {})
            unhealthy = [n for n, s in health.items() if s != "ok"]
            candidates = [n for n in unhealthy
                          if all(health.get(d, "ok") == "ok" for d in topology.DEPENDS_ON.get(n, []))]
            emit("triage", unhealthy=unhealthy, candidates=candidates,
                 title=f"{len(unhealthy)} unhealthy nodes; {len(candidates)} have healthy dependencies and "
                       f"are root-cause candidates")

            while True:
                # ------------------------------------------- investigate
                self._stage(inc, "investigate")
                alerts = list(inc.alerts.values())
                done_at = [a["executed_at"] for a in inc.actions if a.get("executed_at")]
                # later rounds only read logs written after Nabd's last action, so fixed faults stay fixed
                since = max(done_at) if inc.iterations and done_at else inc.started_at - 15
                ev = await core_engine.gather(alerts, emit, since_ts=since)
                inc.signals = core_engine.signals(ev)
                matches = memory.store.search(inc.signals)
                best = matches[0] if matches and matches[0]["similarity"] >= 0.6 else None
                if best and inc.iterations == 0:
                    if self._live_roots(best, ev):
                        inc.memory_hit = {"incident": best["incident"], "similarity": best["similarity"],
                                          "title": best["title"], "mttr_s": best.get("mttr_s")}
                        emit("memory_hit", match=inc.memory_hit,
                             title=f"Seen before: {best['incident']} “{best['title']}” ({best['similarity']:.0%} similar)")
                    else:
                        nodes = ", ".join(sorted({r["node"] for r in best.get("roots", [])}))
                        emit("info", title=f"Looks like {best['incident']} ({best['similarity']:.0%}), but {nodes} is "
                                           f"healthy right now, so its old fix is not reused")

                # ------------------------------------------- hypothesize
                self._stage(inc, "hypothesize")
                core = core_engine.diagnose(ev)
                if runtime.engine == "baseline":
                    core = self._baseline_runbook(alerts)
                route, reason = self._route(core, best if inc.iterations == 0 and self._live_roots(best, ev) else None)
                inc.route, inc.route_reason = route, reason
                emit("router", route=route, reason=reason, title=f"Router → {route}: {reason}")
                hyps, ruled_out = core["hypotheses"], core["ruled_out"]
                if route == "claude":
                    try:
                        feedback = None
                        if inc.iterations > 0:
                            ran = [a for a in inc.actions if a["status"] == "done"]
                            stopped = [a for a in inc.actions if a["status"] in ("blocked", "rejected", "skipped")]
                            feedback = (
                                "The store is still unhealthy. Current checks:\n"
                                + json.dumps({k: v.get("detail") for k, v in inc.verification.items()}, ensure_ascii=False)
                                + ("\nExecuted: " + "; ".join(f"{a['description']} ({a.get('result', '')})" for a in ran)
                                   if ran else "\nNothing was executed.")
                                + ("\nNot executed: " + "; ".join(f"{a['description']}: {a.get('result', a['status'])}"
                                                                  for a in stopped) if stopped else "")
                                + "\nInvestigate what is still broken and submit a diagnosis of the remaining root "
                                  "causes. Keep a well-supported diagnosis even if nothing fixed it yet; if the cause is "
                                  "outside Nabd's control, say so again with action none rather than guessing a fix.")
                        diag = await claude.investigate(inc, alerts, ev["health"], emit, feedback)
                        hyps, ruled_out = diag["hypotheses"], diag["ruled_out"]
                        have = {(h["node"], h["category"]) for h in hyps}
                        hyps += [h for h in core["hypotheses"]
                                 if h.get("phase") == "heal" and (h["node"], h["category"]) not in have]
                        if diag.get("summary"):
                            emit("llm_text", role="investigator", text=diag["summary"])
                    except EngineError as exc:
                        inc.route, inc.route_reason = "core", f"{LLM_NAME} unavailable ({exc}); core engine took over"
                        emit("router", route="core", reason=inc.route_reason, title=f"Router → core: {inc.route_reason}")
                if route == "memory":
                    memory.store.mark_reused(best["incident"])
                    if not _signatures(hyps):
                        hyps = self._from_memory(best, ev)
                if inc.iterations:
                    for h in hyps:
                        h["id"] = f"{h['id']}.{inc.iterations + 1}"
                for h in hyps:
                    emit("hypothesis", hypothesis=h, title=f"{h['id']} {h['title']} ({h['confidence']:.0%})")
                for r in ruled_out:
                    emit("ruled_out", item=r["item"], reason=r["reason"], title=f"Ruled out: {r['item']}")
                if inc.route != "claude":
                    for sp in core.get("suspects", []):
                        emit("suspect", item=sp["item"], reason=sp["reason"], title=f"Suspect: {sp['item']}")
                roots = [h["node"] for h in hyps]
                health = ev["health"]
                symptoms = sorted({n for n in health if n not in roots and
                                   any(n in topology.upstream_closure(r) for r in roots) and health.get(n) != "ok"})
                emit("clusters", roots=roots, symptoms=symptoms,
                     clusters=[{"root": h["node"], "symptoms": [n for n in topology.upstream_closure(h["node"])
                                                                if health.get(n, "ok") != "ok"]} for h in hyps])

                # ------------------------------------------- skeptic
                self._stage(inc, "skeptic")
                verdicts = {}
                if inc.route == "claude" and hyps:
                    try:
                        verdicts = await claude.review(inc, hyps, emit)
                    except EngineError as exc:
                        emit("info", title=f"{LLM_NAME} skeptic unavailable ({exc}); rule-based skeptic used")
                for h in hyps:
                    v = verdicts.get(h["id"]) or (
                        {"challenge": "none (runbook)", "verdict": "upheld", "reasoning": "The baseline does not question itself."}
                        if inc.route == "baseline" else core_engine.skeptic(ev, h))
                    h["skeptic"] = v
                    h["kept"] = v["verdict"] != "refuted"
                    emit("skeptic", hypothesis_id=h["id"], challenge=v["challenge"], verdict=v["verdict"],
                         reasoning=v["reasoning"], title=f"Skeptic on {h['id']}: {v['verdict']}")
                if hyps and not any(h["kept"] for h in hyps):
                    emit("info", title="The skeptic refuted every hypothesis: nothing will be executed this round")
                if inc.iterations:
                    earlier = [h for h in inc.hypotheses if h.get("kept")]
                    known = {(h["node"], h["category"]) for h in earlier}
                    inc.hypotheses = earlier + [h for h in hyps if (h["node"], h["category"]) not in known]
                else:
                    inc.hypotheses = hyps
                inc.ruled_out = ruled_out

                # a confident diagnosis that the cause is outside our control ends here: retrying would only
                # pressure the model into guessing a fix, which is exactly what the gates then have to catch
                outside = [h for h in hyps if h["kept"]]
                if outside and all(h["action"]["name"] == "none" and h["category"] != "other"
                                   and h["confidence"] >= 0.8 for h in outside):
                    inc.status = "escalated"
                    h = outside[0]
                    emit("escalated", title=f"Root cause is outside Nabd's control ({h['category'].replace('_', ' ')} "
                                            f"on {h['node']}): paging the on-call engineer with the diagnosis, "
                                            f"nothing was changed")
                    break

                # ------------------------------------------- act
                self._stage(inc, "act")
                plan = []
                seen = {(a["name"], tuple(sorted(a["params"].items()))) for a in inc.actions if a["status"] == "done"}
                # causes first, deepest dependency first; repairs to the books last, once the cause is gone
                for h in sorted((h for h in hyps if h["kept"]),
                                key=lambda h: (h.get("phase") == "heal", -topology.depth(h["node"]))):
                    key = (h["action"]["name"], tuple(sorted(h["action"]["params"].items())))
                    # a repair may run again in a later round: more damage can surface while the cause is fixed
                    if h["action"]["name"] != "none" and (key not in seen or h.get("phase") == "heal"):
                        seen.add(key)
                        plan.append(h)
                if plan:
                    emit("plan", steps=[{"hypothesis": h["id"], "phase": h.get("phase", "fix"),
                                         "action": actions.describe(h["action"]["name"], h["action"]["params"])}
                                        for h in plan],
                         title=f"Plan: {len(plan)} actions, causes first, then repair the books")
                settled = False
                for h in plan:
                    if h.get("phase") == "heal" and not settled:
                        settled = True
                        if any(a["round"] == inc.iterations and a["status"] == "done" for a in inc.actions):
                            emit("settle", seconds=SETTLE_S,
                                 title=f"Cause fixed; waiting {SETTLE_S} s for in-flight orders to land before "
                                       f"repairing the books")
                            await asyncio.sleep(SETTLE_S)
                    await self._act(inc, h, emit)
                    if inc.budget_spent or inc.regression:
                        break
                if inc.regression and not inc.budget_spent:
                    inc.status = "escalated"
                    emit("escalated", title=f"No-regression guard: {inc.regression}. Nabd stops acting and hands "
                                            f"the incident to the on-call engineer")
                    break
                if inc.budget_spent:
                    inc.status = "escalated"
                    emit("escalated", budget=inc.budget(),
                         title=f"Autonomy budget spent ({inc.budget_spent}): Nabd stops acting and hands the "
                               f"incident to the on-call engineer with everything it found")
                    break

                # ------------------------------------------- verify
                self._stage(inc, "verify")
                acted = any(a["round"] == inc.iterations and a["status"] == "done" for a in inc.actions)
                # nothing changed this round: a short look is enough before re-investigating
                recovered, checks = await verify.run(emit, timeout_s=90 if acted else 30)
                inc.verification = checks
                if inc.regression and not recovered:
                    inc.status = "escalated"
                    emit("escalated", checks=checks,
                         title=f"No-regression guard: {inc.regression}. Nabd stops acting and hands the "
                               f"incident to the on-call engineer")
                    break
                if recovered:
                    inc.resolved_at = time.time()
                    inc.status = "resolved"
                    emit("recovered", mttr_s=round(inc.resolved_at - inc.started_at, 1),
                         title=f"Recovered and verified in {round(inc.resolved_at - inc.started_at)} s from the first fault")
                    # what Nabd diagnosed but must not fix itself still needs a person
                    repaired = any(a["name"] in actions.REPAIR_TARGET and a["status"] == "done" for a in inc.actions)
                    for h in inc.hypotheses:
                        if h.get("kept") and h["category"] in OUTSIDE:
                            if repaired and h.get("learned") and h["category"] == "silent_data_loss":
                                continue  # the books already rebuilt what the learned laws saw go missing
                            self._handoff(inc, emit, {"what": h["title"], "why": h["explanation"],
                                                      "ask": "follow up with the owner of " + h["node"]})
                    break
                inc.empty_rounds = inc.empty_rounds + 1 if not plan else 0
                if inc.empty_rounds >= 2:
                    inc.status = "escalated"
                    emit("escalated", checks=checks,
                         title="No safe fix in Nabd's catalog for this: paging the on-call engineer with the evidence, "
                               "nothing was changed")
                    break
                if inc.iterations >= 2 or runtime.engine == "baseline":
                    inc.status = "escalated"
                    emit("escalated", checks=checks,
                         title="Still not healthy after 3 rounds: paging the on-call engineer with the full evidence")
                    break
                inc.iterations += 1
                self._merge_alerts(inc)
                emit("retry", iteration=inc.iterations,
                     title=f"Verification failed; round {inc.iterations + 1}: re-investigating what is still broken")

            # ------------------------------------------------ report
            self._stage(inc, "report")
            known = {t["fault"] for t in inc.truth}
            inc.truth += [t for t in chaos.truths() if t["fault"] not in known and t["injected_at"] <= inc.opened_at + 60]
            pm = await postmortem.build(inc, recovered)
            if recovered and inc.route != "baseline":
                # only real diagnoses become memory; "unclassified, did nothing" is not knowledge
                kept = [h for h in inc.hypotheses if h.get("kept") and h["category"] != "other"
                        and h["action"]["name"] != "none"]
                if kept:
                    memory.store.remember(inc.id, pm["title"], inc.signals,
                                          [{"node": h["node"], "category": h["category"], "action": h["action"]}
                                           for h in kept], pm["durations"]["mttr_s"])
                chaos.forget_resolved([t["fault"] for t in inc.truth])
            emit("postmortem_ready", postmortem_id=inc.id, title=f"Postmortem {inc.id} written (EN + AR)",
                 summary=inc.summary())
        except Exception as exc:  # the agent must survive its own bugs
            inc.status = "error"
            emit("info", level="error", title=f"Nabd hit an internal error: {type(exc).__name__}: {exc}")
        finally:
            if inc.status == "open":
                inc.status = "escalated"
            self.closed.append(inc.summary())
            self.incident = None
            self.cooldown_until = time.time() + 6
            bus.publish("incident_closed", incident=inc.id, summary=inc.summary())

    # ---------------------------------------------------------- routing

    @staticmethod
    def _baseline_runbook(alerts: list[dict]) -> dict:
        """The classic runbook Nabd is measured against: restart whatever is alerting."""
        services = sorted({a["service"] for a in alerts if a["service"] in topology.STATELESS},
                          key=topology.depth, reverse=True)
        return {"hypotheses": [{
            "id": f"B{i}", "node": svc, "category": "other", "title": f"Runbook: restart {svc}",
            "explanation": "The service is alerting, so the runbook restarts it.",
            "evidence": [f"{svc} has a firing alert"], "confidence": 0.5,
            "action": {"name": "restart_service", "params": {"service": svc}}, "source": "baseline",
        } for i, svc in enumerate(services, 1)], "ruled_out": [], "unexplained": []}

    @staticmethod
    def _live_roots(best: dict | None, ev: dict) -> list[dict]:
        """A remembered root cause counts only if that component is actually unhealthy right now."""
        if not best:
            return []
        alerting = {a["service"] for a in ev["alerts"]}
        return [r for r in best.get("roots", []) if ev["health"].get(r["node"], "ok") != "ok" or r["node"] in alerting]

    def _handoff(self, inc: Incident, emit, item: dict) -> None:
        inc.handoffs.append(item)
        emit("handoff", handoff=item, title=f"Handed to on-call: {item['what']}")

    def _from_memory(self, best: dict, ev: dict) -> list[dict]:
        """No signature matched, but this looks like a past incident: reuse what fixed it."""
        live = self._live_roots(best, ev)
        return [{
            "id": f"M{i}", "node": r["node"], "category": r["category"],
            "title": f"Same as {best['incident']}: {r['category'].replace('_', ' ')} on {r['node']}",
            "explanation": f"The signals match {best['incident']} ({best['similarity']:.0%}); its fix worked then.",
            "evidence": [f"fingerprint similarity {best['similarity']:.0%} with {best['incident']} “{best['title']}”"],
            "confidence": round(0.6 + 0.3 * best["similarity"], 2), "action": r["action"], "source": "memory",
        } for i, r in enumerate(live, 1)]

    def _route(self, core: dict, best: dict | None) -> tuple[str, str]:
        repairs = [h for h in core["hypotheses"] if h.get("phase") == "heal"]
        hyps = [h for h in core["hypotheses"] if h.get("phase") != "heal"]  # routing is about the cause
        if runtime.engine == "baseline":
            return "baseline", "benchmark baseline: restart every alerting service, no diagnosis"
        if repairs and not hyps and not core["unexplained"]:
            return "core", "only the books are left to repair: deterministic accounting, 0 tokens"
        strong_memory = bool(best and best["similarity"] >= 0.75 and best.get("roots"))
        if not _signatures(hyps) and strong_memory and (not CLAUDE_AVAILABLE or runtime.engine != "claude"):
            return "memory", f"no signature matched, but it looks like {best['incident']} ({best['similarity']:.0%})"
        if not CLAUDE_AVAILABLE or runtime.engine == "core":
            if best and best["similarity"] >= 0.75 and hyps:
                return "memory", f"matches {best['incident']} at {best['similarity']:.0%}; reusing its fix, 0 tokens"
            return "core", ("no LLM API key, local core engine" if not CLAUDE_AVAILABLE
                            else "core engine selected in settings")
        if runtime.engine == "claude":
            return "claude", f"{LLM_NAME} selected in settings"
        if best and best["similarity"] >= 0.75 and hyps and all(h["confidence"] >= 0.8 for h in hyps):
            return "memory", f"matches {best['incident']} at {best['similarity']:.0%}; reusing its fix, 0 tokens"
        low = [h for h in hyps if h["confidence"] < 0.8]
        if not hyps:
            return "claude", "no known failure signature matched"
        if low:
            return "claude", f"{len(low)} hypothesis below 80% confidence"
        if core["unexplained"]:
            return "claude", f"alerts on {', '.join(core['unexplained'])} are not explained by any signature"
        if len(hyps) >= 2:
            return "claude", f"{len(hyps)} concurrent root causes: worth a deeper second opinion"
        return "core", f"one signature at {hyps[0]['confidence']:.0%} confidence: fast path, 0 tokens"

    # ---------------------------------------------------------- acting

    async def _act(self, inc: Incident, h: dict, emit) -> None:
        name, params = h["action"]["name"], dict(h["action"]["params"])
        act = {"id": f"{inc.id}-A{len(inc.actions) + 1}", "round": inc.iterations, "hypothesis": h["id"],
               "name": name, "params": params,
               "description": actions.describe(name, params) if name in actions.CATALOG else name,
               "status": "proposed", "proposed_at": time.time()}
        inc.actions.append(act)

        def block(reason: str, stage: str) -> None:
            act["status"] = "blocked"
            act["result"] = reason
            act["blocked_by"] = stage
            emit("action_blocked", action=act, title=f"{stage} blocked “{act['description']}”: {reason}")

        if inc.route != "baseline":
            minutes = (time.time() - inc.opened_at) / 60
            if inc.used["actions"] >= BUDGET["actions"]:
                inc.budget_spent = f"{BUDGET['actions']} actions used"
            elif minutes >= BUDGET["minutes"]:
                inc.budget_spent = f"{BUDGET['minutes']} minutes elapsed"
            if inc.budget_spent:
                return block(f"autonomy budget spent: {inc.budget_spent}", "Autonomy budget")

        error = actions.validate(name, params)
        if error:
            return block(error, "Safety gate")
        if inc.route != "baseline":  # the runbook baseline is measured without Nabd's checks
            valid, why = actions.check_validity(h, name, params)
            if not valid:
                fix = actions.canonical_fix(h)
                if not fix or actions.validate(fix["name"], fix["params"]) or \
                        not actions.check_validity(h, fix["name"], fix["params"])[0]:
                    return block(why, "Validity gate")
                act["corrected_from"] = act["description"]
                name, params = fix["name"], dict(fix["params"])
                act.update(name=name, params=params, description=actions.describe(name, params))
                emit("action_corrected", action=act,
                     title=f"Validity gate: {why}. Corrected to “{act['description']}”")
                valid, why = actions.check_validity(h, name, params)
            if h.get("phase") != "heal" and any(a is not act and a["status"] == "done" and a["name"] == name
                                                and a["params"] == params for a in inc.actions):
                act["status"], act["result"] = "skipped", "already done earlier in this incident"
                emit("action_skipped", action=act, title=f"Skipped “{act['description']}”: already done in this incident")
                return
            act["validity"] = why
            ok, dry_run = await asyncio.to_thread(actions.preflight, name, params)
            act["preflight"] = dry_run
            if not ok:
                return block(dry_run, "Dry run")
        act["risk"] = actions.risk(name, params)
        act["blast_radius"] = actions.blast_radius(name, params)
        act["reversible"] = actions.CATALOG[name]["reversible"]
        emit("action_proposed", action=act, title=f"Proposed: {act['description']} ({act['risk']} risk)")

        stake = await asyncio.to_thread(actions.money_at_stake, name) if name in actions.MONEY_ACTIONS else 0.0
        act["money_sar"] = stake
        over = inc.route != "baseline" and inc.used["money_sar"] + stake > BUDGET["money_sar"]
        if over:
            act["approval_reason"] = (f"{stake:,.0f} SAR would take this incident past the {BUDGET['money_sar']:,} SAR "
                                      f"Nabd may move on its own")
        if actions.needs_approval(act["risk"]) or (over and not runtime.auto_approve):
            act["status"] = "awaiting_approval"
            decision = await self._request_approval(inc, act, h, emit)
            if decision["decision"] != "approve":
                act["status"] = "rejected"
                timed_out = decision["decision"] == "timeout"
                act["approved_by"] = "no answer from on-call" if timed_out else f"rejected via {decision['via']}"
                emit("action_rejected", action=act,
                     title=f"{'No answer from on-call' if timed_out else 'On-call rejected'}: {act['description']}")
                return
            act["approved_by"] = f"on-call via {decision['via']}"
        else:
            act["approved_by"] = ("auto (benchmark)" if runtime.auto_approve else
                                  f"policy: {act['risk']} risk runs automatically in {runtime.mode}")

        before = await asyncio.to_thread(actions.gateway_error_ratio_quick) if act["reversible"] else None
        target = params.get("service")
        # the runbook baseline is measured without Nabd's checks, this guard included
        held = (learner.holding() if target and name not in actions.REPAIR_TARGET and inc.route != "baseline"
                else set())
        act["status"] = "running"
        emit("action_running", action=act, title=f"Executing: {act['description']}")
        try:
            result = await asyncio.to_thread(actions.execute, name, params)
        except Exception as exc:
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        act["status"] = "done" if result.get("ok") else "failed"
        act["result"] = result.get("detail")
        act["executed_at"] = inc.last_change = time.time()
        if name in actions.REPAIR_TARGET and result.get("ok"):
            learner.hush()
        inc.used["actions"] += 1
        act["count"], act["amount_sar"] = result.get("count"), result.get("amount_sar")
        if name in actions.MONEY_ACTIONS:
            inc.used["money_sar"] += result.get("amount_sar") or 0
        emit("budget", budget=inc.budget(), title="budget")
        if result.get("handoff"):
            self._handoff(inc, emit, result["handoff"])
        emit("action_result", action=act, title=f"{'Done' if result.get('ok') else 'Failed'}: {act['description']}"
                                                 f" · {act['result']}")

        revert = result.get("revert")
        if held and result.get("ok"):
            asyncio.create_task(self._no_regression(inc, act, held, target, revert or result.get("undo"), emit))
        if revert and before is not None:
            await asyncio.sleep(4)
            after = await asyncio.to_thread(actions.gateway_error_ratio_quick)
            if after > before + 0.34:
                await asyncio.to_thread(actions.execute, revert["name"], revert["params"])
                act["status"] = "reverted"
                emit("guard_reverted", action=act,
                     title=f"Guard: errors rose from {before:.0%} to {after:.0%}, so Nabd reverted "
                           f"“{act['description']}”")

    async def _no_regression(self, inc: Incident, act: dict, held: set, target: str, revert: dict | None,
                             emit) -> None:
        """No-regression guard (after STRATUS, NeurIPS 2025): a learned law that held before a fix, counts the
        changed service's own requests, and fails on a window measured entirely after Nabd's last change
        means the fix made things worse. Undo it when it can be undone; either way Nabd stops acting on
        this incident.

        A law is judged over the last 30 s, so for 30 s after any change the window still holds data from
        before it, the fault's own included: no law that starts failing then is held against the fix.
        Found the hard way: a correct rollback was reverted because the fault was still in the window."""
        t0 = act["executed_at"]
        while self.incident is inc and inc.status == "open" and act["status"] == "done":
            await asyncio.sleep(5)
            clean = max(t0, inc.last_change or t0) + WINDOW_S  # the first moment a window is all "after"
            if time.time() > clean + 60:
                return
            broke = learner.regressions(held, after=clean, nodes={target}) if time.time() >= clean else []
            if not broke:
                continue
            law = broke[0].sentence()
            act["regressed"] = [b.sentence() for b in broke[:3]]
            if revert:
                await asyncio.to_thread(actions.execute, revert["name"], revert["params"])
                act["status"] = "reverted"
                inc.regression = f"“{act['description']}” broke a law that held before it ({law}), so Nabd reverted it"
                emit("guard_reverted", action=act, title=f"Guard: {inc.regression}")
            else:
                inc.regression = f"“{act['description']}” broke a law that held before it ({law})"
                emit("guard_regression", action=act, law=law,
                     title=f"Guard: {inc.regression}; it cannot be undone automatically")
                self._handoff(inc, emit, {"what": f"check the effect of “{act['description']}”",
                                          "why": f"after it, this law stopped holding: {law}",
                                          "ask": "decide whether to undo it"})
            return

    async def _request_approval(self, inc: Incident, act: dict, h: dict, emit) -> dict:
        event = asyncio.Event()
        self.pending[act["id"]] = {"event": event, "decision": None, "via": None}
        emit("approval_required", approval_id=act["id"], action=act,
             hypothesis={k: h.get(k) for k in ("id", "title", "node", "category", "evidence", "confidence", "skeptic")},
             title=f"Approval needed: {act['description']}")
        try:
            await asyncio.wait_for(event.wait(), timeout=runtime.approval_timeout_s)
            entry = self.pending.pop(act["id"])
            decision = {"decision": entry["decision"], "via": entry["via"]}
        except asyncio.TimeoutError:
            self.pending.pop(act["id"], None)
            decision = {"decision": "timeout", "via": "timeout"}
        emit("approval_result", approval_id=act["id"], **decision,
             title=f"{'Approved' if decision['decision'] == 'approve' else 'Not approved'} via {decision['via']}: "
                   f"{act['description']}")
        return decision

    def decide(self, approval_id: str, decision: str, via: str) -> bool:
        entry = self.pending.get(approval_id)
        if not entry or entry["event"].is_set():
            return False
        entry.update(decision=decision, via=via)
        entry["event"].set()
        return True


agent = Agent()
