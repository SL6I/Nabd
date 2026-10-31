"""Recovery is proven, not assumed: a synthetic shopper must check out three
times in a row, the gateway must meet its SLO, alerts must clear, every
node Nabd touched must report healthy, and Mizan's books must balance."""

import asyncio
import random
import time

import httpx

from .learned import learner
from .mizan import mizan
from .settings import GATEWAY_URL
from .telemetry import telemetry

SLO_ERROR_RATIO = 0.02
SLO_P95_MS = 800


async def journey() -> dict:
    steps = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            t0 = time.perf_counter()
            r = await client.post(f"{GATEWAY_URL}/api/login", json={"user": "nabd-synthetic"})
            steps.append({"step": "login", "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000)})
            token = r.json().get("token", "") if r.status_code == 200 else ""
            t0 = time.perf_counter()
            r = await client.get(f"{GATEWAY_URL}/api/products")
            steps.append({"step": "browse", "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000)})
            t0 = time.perf_counter()
            r = await client.post(f"{GATEWAY_URL}/api/checkout", json={
                "token": token, "sku": random.choice(["DATE-SUKKARI-1KG", "COFFEE-KHAWLANI", "PRAYER-MAT"]), "qty": 1})
            steps.append({"step": "checkout", "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000)})
        except httpx.HTTPError as exc:
            steps.append({"step": "error", "status": 0, "detail": type(exc).__name__})
    ok = len(steps) == 3 and all(s["status"] == 200 for s in steps)
    return {"ok": ok, "steps": steps}


async def run(emit, timeout_s: int = 90) -> tuple[bool, dict]:
    started = time.time()
    deadline = started + timeout_s
    passes = 0
    failing = 0
    books_only = 0
    last_shape = None
    checks: dict = {}
    while time.time() < deadline:
        j = await journey()
        snap = telemetry.latest or {}
        gw = snap.get("services", {}).get("gateway", {})
        health = snap.get("health", {})
        alerts = snap.get("alerts", [])
        checks = {
            "journey": {"ok": j["ok"], "detail": " → ".join(f"{s['step']} {s['status']}" for s in j["steps"])},
            "gateway_slo": {"ok": (gw.get("err") or 0) < SLO_ERROR_RATIO and (gw.get("p95_ms") or 0) < SLO_P95_MS,
                            "detail": f"5xx {(gw.get('err') or 0):.1%} (< {SLO_ERROR_RATIO:.0%}), "
                                      f"p95 {gw.get('p95_ms')} ms (< {SLO_P95_MS})"},
            "alerts_clear": {"ok": not alerts,
                             "detail": "no alerts firing" if not alerts else
                             f"{len(alerts)} still firing: " + ", ".join(f"{a['name']}/{a['service']}" for a in alerts[:4])},
            "nodes_healthy": {"ok": all(health.get(n, "ok") == "ok" for n in health),
                              "detail": "every node healthy" if all(v == "ok" for v in health.values()) else
                              "not yet: " + ", ".join(f"{k} {v}" for k, v in health.items() if v != "ok")},
        }
        from .settings import runtime
        unbalanced = [v for v in (mizan.latest or {}).get("invariants", {}).values()
                      if v.get("ok") is False] if runtime.business_invariants else []
        learned_only = False
        if learner.broken() or (learner.on() and learner.latest.get("stalled")):
            learned_only = not unbalanced
            unbalanced.append({"title": "Learned laws", "detail": f"{learner.latest.get('broken', 0)} broken, "
                                                                  f"{learner.latest.get('stalled', 0)} stalled"})
        checks["books_balanced"] = {"ok": not unbalanced,
                                    "detail": "Mizan: every payment has an order and every order is accounted for"
                                    if not unbalanced else "Mizan: " + "; ".join(f"{v['title'].lower()}: {v['detail']}"
                                                                                 for v in unbalanced[:2])}
        all_ok = all(c["ok"] for c in checks.values())
        passes = passes + 1 if all_ok else 0
        shape = tuple((k, c["ok"]) for k, c in checks.items()) + (passes,)
        if shape != last_shape:
            emit("verify_progress", checks=checks, passes=passes, needed=3,
                 remaining_s=round(deadline - time.time()))
            last_shape = shape
        if passes >= 3:
            return True, checks
        # still hard-failing well after the fixes landed: something else is broken, go look now
        failing = failing + 1 if (not j["ok"] or (gw.get("err") or 0) > 0.10) else 0
        # the store works again but the books do not: no point waiting, the damage needs repairing
        books_only = books_only + 1 if (unbalanced and all(c["ok"] for k, c in checks.items()
                                                           if k not in ("books_balanced", "alerts_clear",
                                                                        "nodes_healthy"))) else 0
        if learned_only and time.time() - started < 45:
            books_only = 0  # learned laws judge 30 s windows: give them one clean window after the fix
        if time.time() - started > 15 and books_only >= 4:
            checks["early_exit"] = {"ok": False, "detail": "checkouts work again, but the books still do not balance: "
                                                          "the damage has to be repaired"}
            emit("verify_progress", checks=checks, passes=0, needed=3, remaining_s=0)
            return False, checks
        if time.time() - started > 40 and failing >= 3:
            checks["early_exit"] = {"ok": False, "detail": "checkouts still failing 40 s after the fixes: "
                                                          "another fault is still active"}
            emit("verify_progress", checks=checks, passes=0, needed=3, remaining_s=0)
            return False, checks
        await asyncio.sleep(3)
    return False, checks
