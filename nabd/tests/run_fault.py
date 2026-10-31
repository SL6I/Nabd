"""Inject one fault, approve whatever Nabd asks for (as an on-call engineer would), and print the
incident as it unfolds. Usage: python run_fault.py <fault> [engine] [timeout_s]
Runs on the host against http://localhost:7000."""

import json
import sys
import time

import httpx

BASE = "http://localhost:7000"
SHOWN = {"incident_opened", "alert_storm", "router", "tool_result", "hypothesis", "skeptic", "plan", "settle",
         "action_proposed", "action_result", "action_blocked", "action_corrected", "approval_required", "recovered",
         "escalated", "retry", "handoff", "verify_progress", "transient", "budget"}


def main() -> None:
    fault = sys.argv[1]
    engine = sys.argv[2] if len(sys.argv) > 2 else "core"
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    c = httpx.Client(base_url=BASE, timeout=30)
    for _ in range(60):  # Nabd may still be starting
        try:
            if c.get("/api/state").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    c.post("/api/settings", json={"engine": engine, "mode": "autopilot"})
    c.post("/api/chaos/reset")
    time.sleep(8)
    before = {i["id"] for i in c.get("/api/state").json()["agent"]["closed"]}
    print(f"inject {fault} (engine {engine})", flush=True)
    t0 = time.time()
    c.post("/api/chaos/inject", json={"fault": fault})
    inc_id, seen = None, 0
    while time.time() - t0 < timeout:
        time.sleep(2)
        st = c.get("/api/state").json()
        cur = st["agent"]["incident"]
        if cur and not inc_id:
            inc_id = cur["id"]
        for a in st["agent"]["pending_approvals"]:
            c.post("/api/approve", json={"approval_id": a, "decision": "approve", "via": "test"})
        if inc_id:
            events = c.get(f"/api/incidents/{inc_id}/events").json()
            for e in events[seen:]:
                if e["type"] in SHOWN:
                    if e["type"] == "verify_progress":
                        bad = {k: v["detail"] for k, v in e["checks"].items() if not v["ok"]}
                        print(f"  {e['ts'] - t0:6.1f}s verify: passes {e['passes']}/3 {json.dumps(bad, ensure_ascii=False)[:300]}")
                    elif e["type"] == "hypothesis":
                        h = e["hypothesis"]
                        print(f"  {e['ts'] - t0:6.1f}s {h['id']} [{h.get('phase', 'fix')}] {h['title']} "
                              f"({h['confidence']:.0%}) → {h['action']['name']}")
                        for ev in h["evidence"][:6]:
                            print(f"           · {ev[:230]}")
                    elif e["type"] == "budget":
                        b = e["budget"]
                        print(f"  {e['ts'] - t0:6.1f}s budget: {b['actions']}/{b['limits']['actions']} actions, "
                              f"{b['money_sar']}/{b['limits']['money_sar']} SAR")
                    else:
                        print(f"  {e['ts'] - t0:6.1f}s {e['type']}: {e.get('title', '')[:260]}")
            seen = len(events)
            if not cur and any(i["id"] == inc_id for i in st["agent"]["closed"]):
                done = next(i for i in st["agent"]["closed"] if i["id"] == inc_id)
                print(f"closed {inc_id}: {done['status']} mttr {done['mttr_s']} s, route {done['route']}, "
                      f"handoffs {len(done.get('handoffs') or [])}")
                m = (st.get("telemetry") or {}).get("mizan") or {}
                for k, v in (m.get("invariants") or {}).items():
                    print(f"  books {k}: {v['ok']} · {v['detail']}")
                return
        elif time.time() - t0 > 240:
            print("no incident opened in 240 s")
            m = (st.get("telemetry") or {}).get("mizan") or {}
            for k, v in (m.get("invariants") or {}).items():
                print(f"  books {k}: {v['ok']} · {v['detail']}")
            return
        if inc_id is None and int(time.time() - t0) % 20 < 2:
            m = (st.get("telemetry") or {}).get("mizan") or {}
            print(f"  {time.time() - t0:6.1f}s waiting · classic alerts {st['telemetry'].get('classic_alerts')} · "
                  + " · ".join(f"{k}: {v['detail']}" for k, v in (m.get('invariants') or {}).items()), flush=True)
    print("timed out")


if __name__ == "__main__":
    main()
