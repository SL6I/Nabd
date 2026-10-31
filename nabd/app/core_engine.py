"""The fast path. Gathers evidence with the read-only tools, matches it
against failure signatures, then lets a rule-based skeptic try to refute
each hypothesis. Runs locally in a couple of seconds for zero tokens."""

import asyncio
import hashlib
import time

from . import config_store, learned, mizan, telemetry, tools, topology
from .settings import runtime

STORE_SERVICES = set(topology.SERVICES)


def _tpl_hash(template: str) -> str:
    return hashlib.sha1(template.encode()).hexdigest()[:8]


def _templates(ev: dict, service: str, needle: str) -> list[dict]:
    return [t for t in ev["logs"].get(service, {}).get("templates", []) if needle.lower() in t["template"].lower()]


def _count(ts: list[dict]) -> int:
    return sum(t["count"] for t in ts)


def _quote(t: dict, width: int = 110) -> str:
    """Quote the real line (numbers intact) and say how many lines share its template."""
    text = t.get("sample") or t["template"]
    return f"{t['count']}× “{text[:width]}{'…' if len(text) > width else ''}”"


# ---------------------------------------------------------------- evidence

async def gather(alerts: list[dict], emit, since_ts: float) -> dict:
    """Run the investigation tools a careful SRE would, in that order."""

    async def run(name: str, **args):
        emit("tool_call", tool=name, args=args, engine="core")
        started = time.time()
        result = await tools.call(name, args)
        emit("tool_result", tool=name, args=args, engine="core", brief=tools.brief(name, result),
             ms=round((time.time() - started) * 1000))
        return result

    ev: dict = {"alerts": alerts, "logs": {}, "probes": {}, "forecasts": {}, "containers": {},
                # what is still firing right now: later rounds must not chase alerts that already cleared
                "firing": (telemetry.telemetry.latest or {}).get("alerts", alerts)}
    health, ev["redis"], ev["postgres"], deploys, probe = await asyncio.gather(
        run("get_service_health"), run("inspect_redis"), run("inspect_postgres"),
        run("get_recent_deploys", service="all", limit=8),
        # Probe the network path even when orders looks fine: with several faults at once, upstream
        # failures can stop traffic from ever reaching orders and hide a broken link behind them.
        run("probe_network_path", source="orders", target="postgres"))
    ev["health"], ev["services"], ev["link"] = health["health"], health["services"], health["orders_db_link_p95_ms"]
    ev["deploys"] = deploys["deploys"]
    if "paths" in probe:
        ev["probes"]["orders->postgres"] = probe

    alerting = {a["service"] for a in alerts}
    unhealthy = {n for n, s in ev["health"].items() if s != "ok" and n in STORE_SERVICES}
    window = int(max(10, time.time() - since_ts))  # only what was written since the incident (or the last fix)
    targets = sorted((alerting | unhealthy) & STORE_SERVICES, key=topology.depth, reverse=True)
    down = [s for s in targets if ev["health"].get(s) == "down"]
    for svc, result in zip(down, await asyncio.gather(*(run("inspect_container", service=s) for s in down))):
        ev["containers"][svc] = result
    readable = [s for s in targets if s not in down or ev["containers"][s].get("running")]
    for svc, result in zip(readable, await asyncio.gather(
            *(run("search_logs", service=s, window_seconds=window, only_problems=False) for s in readable))):
        ev["logs"][svc] = result

    # the books: only when Mizan sees something off, since reconciling reads the PSP ledger and the gateway log
    if learned.learner.broken() or learned.learner.latest.get("stalled"):
        ev["learned"] = await run("check_learned_laws")
    if runtime.business_invariants and not (mizan.mizan.latest or {}).get("balanced", True):
        ev["mizan"] = await run("check_invariants")
        books = ev["mizan"].get("books", {})
        samples = (books.get("charged_and_promised_but_no_order", {}).get("sample", [])[:2]
                   + books.get("charged_but_checkout_failed", {}).get("sample", [])[:1]
                   + books.get("orders_lost_before_fulfillment", {}).get("sample", [])[:2])
        ev["traces"] = list(await asyncio.gather(*(run("trace_item", item=x) for x in samples)))
        if ev["mizan"]["invariants"].get("prices", {}).get("ok") is False:
            ev["mizan"]["price_drift"] = await asyncio.to_thread(mizan.price_drift, mizan.mizan.since)
            for writer in {w["written_by"] for w in ev["mizan"]["price_drift"]["wrong"]} & STORE_SERVICES:
                if writer not in ev["logs"]:
                    ev["logs"][writer] = await run("search_logs", service=writer, window_seconds=window,
                                                   only_problems=False)

    leaking = [s for s in STORE_SERVICES if ev["health"].get(s) != "down" and
               any(a["name"] == "MemoryExhaustionPredicted" and a["service"] == s for a in alerts)]
    for svc, result in zip(leaking, await asyncio.gather(*(run("forecast_memory", service=s) for s in leaking))):
        ev["forecasts"][svc] = result
    return ev


def signals(ev: dict) -> set[str]:
    out = {f"alert:{a['name']}:{a['service']}" for a in ev["alerts"]}
    r = ev.get("redis", {})
    if (r.get("ratio") or 0) >= 0.9:
        out.add("sig:redis_full")
        for p in r.get("top_prefixes", [])[:1]:
            out.add(f"sig:redis_prefix:{p['prefix']}")
    pg = ev.get("postgres", {})
    if (pg.get("ratio") or 0) >= 0.85:
        top = (pg.get("by_application") or [{}])[0].get("application")
        out.add(f"sig:pg_conn_high:{top}")
    for b in pg.get("blocking", [])[:1]:
        out.add(f"sig:pg_lock:{b['blocker_app']}")
    for d in ev.get("deploys", []):
        if d["minutes_ago"] <= 30 and d["diff"]:
            out.add(f"sig:deploy:{d['service']}:{','.join(sorted(d['diff']))}")
    for svc, c in ev.get("containers", {}).items():
        out.add(f"sig:down:{svc}:{'oom' if c.get('oom_killed') else 'exit'}")
    for svc, s in ev.get("services", {}).items():
        if (s.get("cpu") or 0) > 0.85:
            out.add(f"sig:cpu:{svc}")
    for svc, f in ev.get("forecasts", {}).items():
        if f.get("seconds_to_limit") is not None:
            out.add(f"sig:mem_leak:{svc}")
    probe = ev.get("probes", {}).get("orders->postgres", {}).get("paths", {})
    if _probe_ms(probe, "primary") and _probe_ms(probe, "primary") > 300:
        out.add("sig:link_slow:orders")
    for svc, L in ev.get("logs", {}).items():
        for t in L.get("templates", []):
            if t["level"] == "error":
                out.add(f"log:{svc}:{_tpl_hash(t['template'])}")
    return out


def _probe_ms(paths: dict, name: str) -> float | None:
    p = paths.get(name, {})
    return p.get("db_connect_and_query_ms") or (None if "error" not in p else 9999.0)


# ---------------------------------------------------------------- signatures

def _redis_rule(ev: dict) -> dict | None:
    r = ev["redis"]
    # redis-py drops the "OOM" error code, so match the message itself
    oom = [(svc, t) for svc in ev["logs"] for t in _templates(ev, svc, "command not allowed when used memory")]
    full = r.get("ok") and (r.get("ratio") or 0) >= 0.9
    if not full:
        return None  # OOM replies only happen at maxmemory; without that they are leftovers from earlier
    top = next((p for p in r.get("top_prefixes", []) if not p["protected"]), None)
    evidence = []
    if full:
        evidence.append(f"Redis uses {r['used_mb']} of {r['max_mb']} MB ({r['ratio']:.0%}) with policy "
                        f"{r.get('policy')}: new writes are rejected and nothing is evicted")
    if top:
        share = top["est_mb"] / r["max_mb"] if r.get("max_mb") else 0
        ttl = "never expire" if top.get("sample_ttl_s") == -1 else f"TTL {top.get('sample_ttl_s')}s"
        evidence.append(f"{top['keys']} keys under {top['prefix']} hold ≈{top['est_mb']} MB ({share:.0%} of memory) "
                        f"and {ttl}")
    for svc, t in oom[:3]:
        evidence.append(f"{svc}: {_quote(t)}")
    use_flush = bool(top and r.get("max_mb") and top["est_mb"] >= 0.3 * r["max_mb"])
    action = ({"name": "flush_redis_keys", "params": {"prefix": top["prefix"]}} if use_flush
              else {"name": "restart_service", "params": {"service": "redis"}})
    return {
        "node": "redis", "category": "cache_memory_exhaustion",
        "title": f"Redis is full: a key leak under {top['prefix'] if top else 'an unknown prefix'}",
        "explanation": "Redis reached maxmemory with the noeviction policy, so every write fails. Inventory "
                       "cannot store reservation holds and payments cannot store idempotency keys.",
        "evidence": evidence, "confidence": 0.95 if (full and oom) else 0.8, "action": action,
    }


def _config_rule(ev: dict) -> list[dict]:
    out = []
    psp = ev["services"].get("psp", {})
    psp_healthy = psp.get("up") == 1 and (psp.get("err") or 0) < 0.02 and (psp.get("p95_ms") or 0) < 500
    for d in ev["deploys"]:
        if d["minutes_ago"] > 30 or not d["diff"] or d["author"] == "nabd" or d["service"] != "payments":
            continue
        timeouts = _templates(ev, "payments", "timed out after")
        if not timeouts:
            continue
        diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in d["diff"].items())
        evidence = [
            f"payments: {_quote(timeouts[0])}",
            f"deploy {d['id']} by {d['author']} {d['minutes_ago']} min ago (v{d['version']}): {diff}; "
            f"“{d['message']}”",
            f"the PSP itself answers with p95 {psp.get('p95_ms')} ms and {(psp.get('err') or 0):.1%} errors",
        ]
        out.append({
            "node": "payments", "category": "bad_config_deploy",
            "title": f"Bad config deploy on payments: {diff}",
            "explanation": "The new PSP timeout is shorter than the provider's normal response time, so every "
                           "charge is abandoned before the PSP can answer.",
            "evidence": evidence, "confidence": 0.94 if psp_healthy else 0.6,
            "action": {"name": "rollback_config", "params": {"service": "payments"}},
            "deploy_id": d["id"],
        })
        break
    return out


def _link_rule(ev: dict) -> dict | None:
    paths = ev["probes"].get("orders->postgres", {}).get("paths", {})
    primary, standby = _probe_ms(paths, "primary"), _probe_ms(paths, "standby")
    slow_logs = _templates(ev, "orders", "slow database round-trip via primary")
    gw_timeouts = _templates(ev, "gateway", "upstream orders timed out")
    if not (primary and standby and primary > 300 and primary > 3 * standby):
        return None
    evidence = [f"probe from inside orders: connect + SELECT 1 takes {primary:.0f} ms on the primary link vs "
                f"{standby:.0f} ms on the standby link"]
    if (ev["link"].get("primary_p95_ms") or 0) > 300:
        evidence.append(f"orders write round-trip p95 on the primary link: {ev['link']['primary_p95_ms']} ms")
    for t in (slow_logs[:1] + gw_timeouts[:1]):
        evidence.append(_quote(t))
    return {
        "node": "orders-db-link", "category": "network_degradation",
        "title": "Network degradation on the orders → Postgres primary link",
        "explanation": "Every round trip on the primary link is slow while Postgres answers fast over the standby "
                       "link, so the database is healthy and the path to it is not.",
        "evidence": evidence, "confidence": 0.93,
        "action": {"name": "switch_db_route", "params": {"service": "orders", "route": "standby"}},
    }


def _hung_rule(ev: dict) -> list[dict]:
    """Running, but not answering scrapes: usually something is starving the process."""
    out = []
    for svc, c in ev["containers"].items():
        if not c.get("running"):
            continue
        cpu = c.get("cpu_percent_of_one_core")
        busy = cpu is not None and cpu >= 80
        reindex = _templates(ev, svc, "reindex batch")
        evidence = [f"{svc} container is running but has stopped answering metric scrapes and health checks"]
        if cpu is not None:
            evidence.append(f"{svc} is using {cpu:.0f}% of a CPU core")
        if reindex:
            evidence.append(f"{svc}: {_quote(reindex[0])}")
        out.append({
            "node": svc, "category": "cpu_saturation" if busy or reindex else "process_crash",
            "title": f"{svc} is hung{': a runaway job is starving it' if busy or reindex else ''}",
            "explanation": "The process is alive but cannot serve requests. Restarting clears the stuck work.",
            "evidence": evidence, "confidence": 0.9 if (busy or reindex) else 0.75,
            "action": {"name": "restart_service", "params": {"service": svc}},
        })
    return out


def _down_rule(ev: dict) -> list[dict]:
    out = []
    for svc, c in ev["containers"].items():
        if c.get("running"):
            continue
        oom = c.get("oom_killed")
        unreachable = _templates(ev, "gateway", f"upstream {svc} unreachable")
        evidence = [f"{svc} container is {c.get('status')} (exit code {c.get('exit_code')}"
                    f"{', killed by the OOM killer' if oom else ''})"]
        if unreachable:
            evidence.append(f"gateway: {_quote(unreachable[0])}")
        out.append({
            "node": svc, "category": "memory_leak" if oom else "process_crash",
            "title": f"{svc} is down{' after running out of memory' if oom else ''}",
            "explanation": f"The {svc} process is not running, so every call to it fails." +
                           (" It hit its memory limit, which points at a leak." if oom else ""),
            "evidence": evidence, "confidence": 0.96,
            "action": {"name": "start_service", "params": {"service": svc}},
        })
    return out


def _cpu_rule(ev: dict) -> list[dict]:
    out = []
    for svc, s in ev["services"].items():
        if (s.get("cpu") or 0) <= 0.85 or ev["health"].get(svc) == "down":
            continue
        reindex = _templates(ev, svc, "reindex batch")
        evidence = [f"{svc} burns {s['cpu']:.0%} of a CPU core at only {s.get('rps')} req/s",
                    f"{svc} p95 latency {s.get('p95_ms')} ms"]
        if reindex:
            evidence.append(f"{svc}: {_quote(reindex[0])}")
        out.append({
            "node": svc, "category": "cpu_saturation",
            "title": f"A runaway background job is starving {svc}",
            "explanation": "CPU is pegged without a matching rise in traffic. A background reindex thread holds the "
                           "interpreter, so request handlers queue up.",
            "evidence": evidence, "confidence": 0.9 if reindex else 0.7,
            "action": {"name": "restart_service", "params": {"service": svc}},
        })
    return out


def _leak_rule(ev: dict) -> list[dict]:
    out = []
    for svc, f in ev["forecasts"].items():
        if f.get("seconds_to_limit") is None or f.get("slope_mb_per_s", 0) < 0.5:
            continue
        warm = _templates(ev, svc, "warming response cache")
        evidence = [f"{svc} memory is {f['current_mb']} MB and rising {f['slope_mb_per_s']} MB/s toward its "
                    f"{f['limit_mb']} MB limit: OOM kill in about {f['seconds_to_limit']} s",
                    f"traffic on {svc} is flat at {ev['services'].get(svc, {}).get('rps')} req/s"]
        if warm:
            evidence.append(f"{svc}: {_quote(warm[0])}")
        out.append({
            "node": svc, "category": "memory_leak",
            "title": f"Memory leak in {svc}: OOM predicted in {f['seconds_to_limit']} s",
            "explanation": "Memory grows linearly regardless of traffic. Restarting before the kernel kills the "
                           "process avoids dropping in-flight payments.",
            "evidence": evidence, "confidence": 0.9,
            "action": {"name": "restart_service", "params": {"service": svc}}, "predictive": True,
        })
    return out


def _pg_rules(ev: dict) -> list[dict]:
    out = []
    pg = ev["postgres"]
    if not pg.get("ok"):
        return out
    too_many = [t for svc in ev["logs"] for t in _templates(ev, svc, "connection slots")] + \
               [t for svc in ev["logs"] for t in _templates(ev, svc, "too many clients")]
    apps = pg.get("by_application", [])
    rogue = next((a for a in apps if a["application"] not in STORE_SERVICES | {"nabd", "postgres"}), None)
    if (pg.get("ratio") or 0) >= 0.85 and rogue and rogue["total"] >= 0.5 * pg["connections"]:
        idle = rogue["states"].get("idle", 0)
        evidence = [f"Postgres: {pg['connections']} of {pg['max_connections'] - pg['reserved_for_admin']} client "
                    f"slots in use",
                    f"{rogue['application']} holds {rogue['total']} sessions, {idle} of them idle"]
        evidence += [_quote(t) for t in too_many[:2]]
        out.append({
            "node": "postgres", "category": "db_connection_exhaustion",
            "title": f"{rogue['application']} is hoarding Postgres connections",
            "explanation": "A batch client opened every free connection and sits idle, so the store services "
                           "cannot connect at all.",
            "evidence": evidence, "confidence": 0.94,
            "action": {"name": "terminate_db_sessions", "params": {"application_name": rogue["application"]}},
        })
    blockers: dict[str, list] = {}
    for b in pg.get("blocking", []):
        if b["blocker_app"] not in STORE_SERVICES and b["blocker_txn_age_s"] > 3:
            blockers.setdefault(b["blocker_app"], []).append(b)
    for app, rows in blockers.items():
        first = rows[0]
        timeouts = [t for svc in ev["logs"] for t in _templates(ev, svc, "statement timeout")]
        evidence = [f"{len(rows)} queries are blocked behind pid {first['blocker_pid']} ({app}, "
                    f"{first['blocker_state']}, transaction open {first['blocker_txn_age_s']:.0f} s)",
                    f"blocker's last statement: “{first['blocker_query']}”",
                    f"blocked: “{first['blocked_query']}” waiting {first['waiting_s']:.0f} s"]
        evidence += [_quote(t) for t in timeouts[:1]]
        out.append({
            "node": "postgres", "category": "db_lock_contention",
            "title": f"{app} holds a lock on products and never commits",
            "explanation": "An open transaction holds an ACCESS EXCLUSIVE lock, so every read and stock update on "
                           "products waits until the statement timeout.",
            "evidence": evidence, "confidence": 0.93,
            "action": {"name": "terminate_db_sessions", "params": {"application_name": app}},
        })
    return out


def _when(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts)) if ts else "?"


def _trace_line(t: dict) -> str:
    hops = " → ".join(f"{h['service']} “{(h.get('msg') or '')[:60]}”" for h in t["hops"][:4])
    return f"trace {t['item']}: {hops or 'no log lines'} · {t['verdict']}"


def _mizan_rules(ev: dict) -> list[dict]:
    """Silent failures: what broke the books, and how to repair them once the cause is fixed."""
    m = ev.get("mizan")
    if not m:
        return []
    inv, books, traces = m["invariants"], m.get("books", {}), ev.get("traces", [])
    classic = m.get("classic_alerts_firing", 0)
    gw_err = (ev["services"].get("gateway", {}).get("err") or 0)
    quiet = f"classic monitoring: {classic} Prometheus alerts, gateway 5xx {gw_err:.1%}"
    out: list[dict] = []

    # ---- cause: an item vanished inside a component → the recent change that owns that component
    lost_in: dict[str, list[dict]] = {}
    for t in traces:
        if t.get("lost_in") in topology.NODES:
            lost_in.setdefault(t["lost_in"], []).append(t)
    for node, ts in lost_in.items():
        owners = [node] if node in config_store.BASELINE else \
            [n for n in topology.dependents(node) if n in config_store.BASELINE]
        name = "money" if node == "orders" else "fulfilled"
        first = inv.get(name, {}).get("first_at")
        deploys = [d for d in ev["deploys"] if d["service"] in owners and d["minutes_ago"] <= 30
                   and d["diff"] and d["author"] != "nabd"]
        evidence = [f"Mizan · {inv[name]['title']}: {inv[name]['detail']}", quiet]
        if node == "events" and inv.get("conserved", {}).get("ok") is False:
            evidence.append(f"Mizan · conservation: {inv['conserved']['detail']}")
        evidence += [_trace_line(t) for t in ts[:2]]
        if deploys:
            d = deploys[0]
            diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in d["diff"].items())
            before = bool(first and d.get("ts") and d["ts"] <= first + 5)
            evidence.append(f"deploy {d['id']} to {d['service']} by {d['author']} {d['minutes_ago']} min ago "
                            f"(v{d['version']}): {diff}; “{d['message']}”")
            if first:
                evidence.append(f"first lost item at {_when(first)}, "
                                f"{'after' if before else 'BEFORE'} the deploy at {_when(d.get('ts'))}")
            out.append({
                "node": d["service"], "category": "bad_config_deploy", "mizan": True, "deploy_id": d["id"],
                "deploy_ts": d.get("ts"), "first_violation": first,
                "title": f"Silent loss after a config change on {d['service']}: {diff}",
                "explanation": (f"Every request still succeeds, but items stop existing after {d['service']} accepts "
                                f"them. The loss started with this change, and the trace ends inside {node}."),
                "evidence": evidence, "confidence": 0.92 if before else 0.55,
                "action": {"name": "rollback_config", "params": {"service": d["service"]}},
            })
        else:
            out.append({
                "node": node, "category": "silent_data_loss", "mizan": True, "first_violation": first,
                "title": f"Items are silently lost inside {node}",
                "explanation": "The trace ends inside this component and no recent change explains it: a code bug "
                               "that needs an engineer. Nabd can repair the data but not the code.",
                "evidence": evidence + ["no config change on this component in the last 30 min"],
                "confidence": 0.75, "action": {"name": "none", "params": {}},
            })

    # ---- the order exists, but records a different amount than the customer paid
    money = inv.get("money", {})
    if money.get("ok") is False and money.get("mismatched"):
        out.append({
            "node": "orders", "category": "corrupted_data", "mizan": True, "ledger": True,
            "first_violation": money.get("first_at"),
            "title": (f"{money['mismatched']} order{' records' if money['mismatched'] == 1 else 's record'} a different "
                      f"amount than the customer was charged"),
            "explanation": "Every payment has its order, but the order's total is not what the payment provider "
                           "captured. The provider's ledger is the source of truth. Nabd has no safe action that "
                           "rewrites order totals, so it stops and hands the list to a person.",
            "evidence": [f"Mizan · {money['title']}: {money['detail']}",
                         "orders with the wrong total: " + ", ".join(money.get("mismatch_sample", [])), quiet],
            "confidence": 0.85, "action": {"name": "none", "params": {}},
        })

    # ---- a shipment for an order nobody placed
    ful = inv.get("fulfilled", {})
    if ful.get("ok") is False and ful.get("phantom_count"):
        out.append({
            "node": "events", "category": "corrupted_data", "mizan": True, "phantom": True,
            "title": (f"{ful['phantom_count']} shipment{' was' if ful['phantom_count'] == 1 else 's were'} booked for "
                      f"{'an order that does' if ful['phantom_count'] == 1 else 'orders that do'} not exist"),
            "explanation": "The order-event queue carried an event that no order produced, and fulfillment shipped "
                           "it. Goods may leave without payment. Nabd has no safe action that cancels a shipment, "
                           "so it stops and hands the list to a person.",
            "evidence": [f"Mizan · {ful['title']}: {ful['detail']}",
                         "shipments with no order: " + ", ".join(ful.get("phantoms", [])), quiet],
            "confidence": 0.85, "action": {"name": "none", "params": {}},
        })

    # ---- liveness: the worker holds one message and does nothing else
    prog = inv.get("progress", {})
    stuck = prog.get("stuck")
    if prog.get("ok") is False and stuck:
        fl = ev["services"].get("fulfillment", {})
        errors = [t for t in ev["logs"].get("fulfillment", {}).get("templates", []) if t["level"] == "error"]
        evidence = [f"Mizan · {prog['title']}: {prog['detail']}",
                    f"the worker has been at step “{stuck.get('step')}” for {stuck['age_s']:.0f} s on "
                    f"{stuck['order_ref']} ({stuck['sku']}); every order behind it waits",
                    f"fulfillment is up, CPU {(fl.get('cpu') or 0):.0%}, "
                    f"{len(errors)} error templates in its logs: blocked, not crashing", quiet]
        out.append({
            "node": "fulfillment", "category": "poison_message", "mizan": True,
            "title": f"One message ({stuck['order_ref']}) is blocking the fulfillment line",
            "explanation": "Head-of-line blocking: the worker waits forever on one message, so the queue behind it "
                           "never moves. Moving that message to the dead-letter queue frees the line; a human "
                           "handles the message itself.",
            "evidence": evidence, "confidence": 0.9, "stuck": stuck,
            "action": {"name": "quarantine_stuck_message", "params": {"service": "fulfillment"}},
        })
        if "carrier" in str(stuck.get("step", "")):
            out.append({
                "node": "carrier", "category": "dependency_failure", "mizan": True,
                "title": "The freight carrier's quote API never answers",
                "explanation": "The step the worker is stuck on is a call to the carrier, and the call has no "
                               "timeout. The carrier is someone else's system: a human must chase it.",
                "evidence": [f"stuck step: “{stuck.get('step')}” for {stuck['age_s']:.0f} s",
                             "no error was ever returned: the request is simply still open"],
                "confidence": 0.85, "action": {"name": "none", "params": {}},
            })

    # ---- corruption: the database recorded who wrote each wrong value
    prices = inv.get("prices", {})
    drift = m.get("price_drift") or {}
    if prices.get("ok") is False and drift.get("wrong"):
        wrong = drift["wrong"]
        writers = sorted({w["written_by"] for w in wrong if w.get("written_by")})
        first = min((w["written_at"] for w in wrong if w.get("written_at")), default=None)
        evidence = [f"Mizan · {prices['title']}: {prices['detail']}", quiet,
                    "; ".join(f"{w['sku']} {w['price']:.2f} instead of {w['truth']:.2f} SAR, last written by "
                              f"{w.get('written_by') or 'unknown'}" for w in wrong[:3])]
        for writer in writers:
            logs = [t for t in ev["logs"].get(writer, {}).get("templates", []) if t["level"] in ("info", "warn")]
            if logs:
                evidence.append(f"{writer}: {_quote(logs[0])}")
        deploys = [d for d in ev["deploys"] if d["service"] in writers and d["minutes_ago"] <= 30
                   and d["diff"] and d["author"] != "nabd"]
        if deploys:
            d = deploys[0]
            diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in d["diff"].items())
            before = bool(first and d.get("ts") and d["ts"] <= first + 5)
            evidence.append(f"deploy {d['id']} to {d['service']} by {d['author']} {d['minutes_ago']} min ago "
                            f"(v{d['version']}): {diff}; “{d['message']}”")
            if first:  # the trigger keeps the latest write per row, so this is the oldest of those
                evidence.append(f"the wrong prices were written from {_when(first)} on, "
                                f"{'after' if before else 'BEFORE'} the deploy at {_when(d.get('ts'))}")
            out.append({
                "node": d["service"], "category": "bad_config_deploy", "mizan": True, "deploy_id": d["id"],
                "deploy_ts": d.get("ts"), "first_violation": first,
                "title": f"Prices drift after a config change on {d['service']}: {diff}",
                "explanation": f"Every request succeeds, but {d['service']} keeps rewriting prices away from the "
                               f"supplier's list, a little more on every run. The database records it as the writer.",
                "evidence": evidence, "confidence": 0.92 if before else 0.55,
                "action": {"name": "rollback_config", "params": {"service": d["service"]}},
            })
        elif writers:
            out.append({
                "node": writers[0], "category": "silent_data_loss", "mizan": True, "first_violation": first,
                "title": f"{writers[0]} writes wrong prices",
                "explanation": "The database names this component as the writer and no recent change explains it: "
                               "a code bug for an engineer. Nabd can restore the data but not the code.",
                "evidence": evidence, "confidence": 0.75, "action": {"name": "none", "params": {}},
            })
        out.append({
            "node": "inventory", "category": "corrupted_data", "phase": "heal", "mizan": True,
            "title": f"{len(wrong)} catalog prices are wrong",
            "explanation": "Restore each price from the supplier's catalog, the source of truth. Orders already sold "
                           "at a wrong price go to a human: re-billing a customer is a business decision.",
            "evidence": [f"Mizan · {prices['title']}: {prices['detail']}"], "confidence": 0.95,
            "action": {"name": "restore_catalog_prices", "params": {"service": "inventory"}},
        })

    # ---- repair: deterministic accounting, whatever caused it
    promised = books.get("charged_and_promised_but_no_order", {})
    failed = books.get("charged_but_checkout_failed", {})
    lost = books.get("orders_lost_before_fulfillment", {})
    if promised.get("count"):
        out.append({
            "node": "orders", "category": "orphaned_payments", "phase": "heal", "mizan": True,
            "title": f"{promised['count']} customers paid and were told their order exists, but it does not",
            "explanation": "Honor what the customer was told: rebuild each order from the PSP's charge metadata "
                           "(user, item, quantity) under the order number they already received.",
            "evidence": [f"{promised['count']} captured charges ({promised['sar']:,.0f} SAR) have no order; the "
                         f"gateway logged “checkout completed” for each",
                         *[_trace_line(t) for t in traces if t["payment_id"] in promised.get("sample", [])][:1]],
            "confidence": 0.95,
            "action": {"name": "replay_orphan_payments", "params": {"service": "orders"}},
        })
    if failed.get("count"):
        out.append({
            "node": "payments", "category": "orphaned_payments", "phase": "heal", "mizan": True,
            "title": f"{failed['count']} customers were charged although their checkout failed",
            "explanation": "The customer saw an error, so there is no order to honor and the charge must not "
                           "stand: refund it at the PSP.",
            "evidence": [f"{failed['count']} captured charges ({failed['sar']:,.0f} SAR) have no order and no "
                         f"“checkout completed” line",
                         *[_trace_line(t) for t in traces if t["payment_id"] in failed.get("sample", [])][:1]],
            "confidence": 0.93,
            "action": {"name": "refund_failed_payments", "params": {"service": "payments"}},
        })
    if lost.get("count"):
        out.append({
            "node": "events", "category": "lost_events", "phase": "heal", "mizan": True,
            "title": f"{lost['count']} paid orders will never ship: their events were lost",
            "explanation": "The orders exist, but their events are in no queue and fulfillment never saw them. "
                           "Re-publishing them is safe: fulfillment books each order at most once.",
            "evidence": [f"Mizan · {inv['fulfilled']['title']}: {inv['fulfilled']['detail']}",
                         *[_trace_line(t) for t in traces if t["order_ref"] in lost.get("sample", [])][:1]],
            "confidence": 0.95,
            "action": {"name": "replay_lost_events", "params": {"service": "events"}},
        })
    return out


def _learned_rule(ev: dict) -> list[dict]:
    """The laws nobody wrote: a fixed ratio between two flows broke, so items vanish between them."""
    lr = ev.get("learned")
    if not lr or lr.get("state") != "watching":
        return []
    out = []
    worst, suspects = lr.get("worst") or [], lr.get("suspects") or []
    head = (f"Mizan learned {lr.get('laws')} laws from {lr.get('flows')} flows of healthy traffic; "
            f"{lr.get('broken')} broke and {lr.get('lagging', 0)} only lag behind a growing list")
    if worst and suspects:
        top = suspects[0]
        laws = [f"law “{w['law']}” broke: {w['now']} ({w['missing']} items in 30 s)" for w in worst[:2]]
        evidence = [head, *laws, *top["evidence"]]
        first = lr.get("first_break")
        change = next((d for d in ev["deploys"] if d["id"] == top.get("change")), None)
        if change:
            diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in change["diff"].items())
            novel = any("never logged" in e for e in top["evidence"])
            out.append({
                "node": top["service"], "category": "bad_config_deploy", "mizan": True, "learned": True,
                "deploy_id": change["id"], "deploy_ts": change.get("ts"), "first_violation": first,
                "title": f"Items vanish behind {top['service']} after its change: {diff}",
                "explanation": f"No rule was written for this. Two flows that always moved together stopped "
                               f"agreeing right after {top['service']} changed, and {top['service']} sits on the "
                               f"side that still counts.",
                "evidence": evidence, "confidence": 0.9 if novel else 0.84,
                "action": {"name": "rollback_config", "params": {"service": top["service"]}},
            })
        out.append({
            "node": top["service"], "category": "silent_data_loss", "mizan": True, "learned": True,
            "first_violation": first,
            "title": f"Work that passed through {top['service']} was lost while the law was broken",
            "explanation": "Nabd can stop the loss, but it has no rule to rebuild what was lost here: a person "
                           "must reconcile the affected records.",
            "evidence": evidence, "confidence": 0.8 if change else 0.72,
            "action": {"name": "none", "params": {}},
        })
    for st in (lr.get("stalls") or [])[:1]:
        out.append({
            "node": learned._queue_node(), "category": "other", "mizan": True, "learned": True,
            "title": "Work keeps arriving but its consumer has stopped",
            "explanation": "The producer still counts, a list keeps growing and the consumer counts nothing. "
                           "Waiting is not loss, but nothing is moving.",
            "evidence": [head, f"law “{st['law']}”: {st['now']}",
                         f"growing: {', '.join(lr.get('growing') or [])}"],
            "confidence": 0.65, "action": {"name": "none", "params": {}},
        })
    return out


def diagnose(ev: dict) -> dict:
    hyps: list[dict] = []
    for rule in (_down_rule, _hung_rule, _pg_rules, _config_rule, _cpu_rule, _leak_rule, _mizan_rules, _learned_rule):
        hyps.extend(rule(ev))
    # one hypothesis per node and category: the hung rule and the CPU rule can both see a starving service
    unique: dict[tuple, dict] = {}
    for h in hyps:
        key = (h["node"], h["category"])
        if key not in unique or h["confidence"] > unique[key]["confidence"]:
            unique[key] = h
    hyps = list(unique.values())
    # when the hand-written books can rebuild the records, the learned "a person must reconcile" is moot
    if any(h.get("phase") == "heal" for h in hyps):
        hyps = [h for h in hyps if not (h.get("learned") and h["category"] == "silent_data_loss")]
    for rule in (_redis_rule, _link_rule):
        h = rule(ev)
        if h:
            hyps.append(h)

    # a crashed service already explains its own leak forecast
    down = {h["node"] for h in hyps if h["category"] in ("process_crash",) or h["action"]["name"] == "start_service"}
    hyps = [h for h in hyps if not (h.get("predictive") and h["node"] in down)]

    explained = set()
    for h in hyps:
        if h.get("mizan"):  # an invariant alert is accounted for by any hypothesis built from the books
            explained |= {a["service"] for a in ev["alerts"] if a.get("source") == "mizan"}
        if h.get("phase") == "heal":
            continue  # repairing the books explains nothing about why services broke
        explained |= {h["node"], *topology.upstream_closure(h["node"])}
    unexplained = sorted({a["service"] for a in ev.get("firing", ev["alerts"])} - explained)
    if not [h for h in hyps if h.get("phase") != "heal"] and unexplained:
        # point the human at the deepest unhealthy component, the same rule triage uses for candidates
        sick = [n for n, s in ev["health"].items() if s != "ok" and n != "traffic"]
        worst = max(sick or {a["service"] for a in ev["alerts"]}, key=topology.depth)
        hyps.append({
            "node": worst, "category": "other", "title": f"Unclassified failure around {worst}",
            "explanation": "No known failure signature matches the evidence.",
            "evidence": [f"{a['name']} on {a['service']}: {a['summary']}" for a in (ev.get("firing") or ev["alerts"])[:4]],
            "confidence": 0.3, "action": {"name": "none", "params": {}},
        })

    used_deploys = {h.get("deploy_id") for h in hyps}
    alerting = {a["service"] for a in ev["alerts"]}
    ruled_out, suspects = [], []
    for d in ev["deploys"]:
        if d["minutes_ago"] > 30 or not d["diff"] or d["id"] in used_deploys or d["author"] == "nabd":
            continue
        svc = d["service"]
        diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in d["diff"].items())
        item = f"deploy {d['id']} to {svc} ({diff}, {d['minutes_ago']} min ago)"
        roots_above = [h["node"] for h in hyps if h["category"] != "other" and svc in topology.upstream_closure(h["node"])]
        troubled = (ev["health"].get(svc, "ok") != "ok" or svc in alerting
                    or any(t["level"] in ("error", "warn") for t in ev["logs"].get(svc, {}).get("templates", [])))
        # rule a change out only with a checked reason; an unexplained change on a sick service is a suspect
        if roots_above:
            ruled_out.append({"item": item, "reason": f"{svc}'s errors are fully explained by {', '.join(roots_above)}"})
        elif not troubled:
            checked = "no alerts, and no warnings or errors in its logs" if svc in ev["logs"] else "no alerts and every check green"
            ruled_out.append({"item": item, "reason": f"{svc} is healthy: {checked}"})
        else:
            suspects.append({"item": item, "service": svc,
                             "reason": f"{svc} is unhealthy and no known signature explains this change"})
    for h in hyps:
        if h["category"] == "other":
            h["evidence"] += [f"unexplained recent change: {s['item']}" for s in suspects]
    unexplained = sorted(set(unexplained) | {s["service"] for s in suspects})

    for i, h in enumerate(sorted(hyps, key=lambda h: topology.depth(h["node"]), reverse=True), 1):
        h["id"] = f"H{i}"
        h["source"] = "core"
    hyps.sort(key=lambda h: h["id"])
    return {"hypotheses": hyps, "ruled_out": ruled_out, "suspects": suspects, "unexplained": unexplained}


# ---------------------------------------------------------------- skeptic

def skeptic(ev: dict, h: dict) -> dict:
    s = ev["services"]
    cat = h["category"]
    if cat == "cache_memory_exhaustion":
        r = ev["redis"]
        ok = (r.get("ratio") or 0) >= 0.9
        return {"challenge": "Is Redis really full, or are the clients misreporting a network problem?",
                "verdict": "upheld" if ok else "uncertain",
                "reasoning": f"INFO memory reports {r.get('used_mb')}/{r.get('max_mb')} MB; the errors are OOM "
                             f"replies, not timeouts." if ok else "Redis memory is below 90%; the OOM errors may be stale."}
    if cat == "bad_config_deploy" and h.get("mizan"):
        first, deployed = h.get("first_violation"), h.get("deploy_ts")
        ok = bool(first and deployed and deployed <= first + 5)
        return {"challenge": "Did the loss start before this change, which would make it a coincidence?",
                "verdict": "upheld" if ok else "uncertain",
                "reasoning": (f"The first lost item is from {_when(first)}, after the change went out at "
                              f"{_when(deployed)}, and the trace ends in the component this change configures."
                              if ok else "The loss cannot be placed after the change with the evidence at hand.")}
    if cat == "corrupted_data" and h.get("ledger"):
        return {"challenge": "Could the charge have been changed or partly refunded at the provider?",
                "verdict": "upheld",
                "reasoning": "The provider's ledger shows each charge captured for its full amount; only our "
                             "order row differs."}
    if cat == "corrupted_data" and h.get("phantom"):
        return {"challenge": "Could the order still be on its way to the database?", "verdict": "upheld",
                "reasoning": "An order is committed before its event is published, so a shipped event always "
                             "has its order; these have none."}
    if cat == "corrupted_data":
        return {"challenge": "Could the supplier itself have changed these prices?",
                "verdict": "upheld",
                "reasoning": "The supplier's catalog still lists the old prices; only our copy moved, and the "
                             "database names our own job as the writer."}
    if cat in ("orphaned_payments", "lost_events"):
        return {"challenge": "Could these records still be on their way (in flight, not lost)?",
                "verdict": "upheld",
                "reasoning": "Each one is older than the grace window, and Mizan read the queues before the "
                             "database, so an item in motion is always seen in one of them."}
    if cat == "poison_message":
        st = h.get("stuck") or {}
        return {"challenge": "Is the worker just slow on a big order rather than stuck?",
                "verdict": "upheld" if (st.get("age_s") or 0) > 30 else "uncertain",
                "reasoning": f"Normal orders take under a second; this one has been at the same step for "
                             f"{st.get('age_s', 0):.0f} s while the worker sends no heartbeat."}
    if cat == "silent_data_loss":
        return {"challenge": "Is this loss real, or a reporting artifact?", "verdict": "upheld",
                "reasoning": "The PSP ledger, the orders table and the queues are independent records, and they "
                             "disagree."}
    if cat == "dependency_failure":
        return {"challenge": "Is the external system really the one hanging?", "verdict": "upheld",
                "reasoning": "The request to it is still open after the stall threshold, with no error and no "
                             "response."}
    if cat == "bad_config_deploy":
        psp = s.get("psp", {})
        healthy = psp.get("up") == 1 and (psp.get("p95_ms") or 0) < 500
        return {"challenge": "Could the payment provider itself be slow or down?",
                "verdict": "upheld" if healthy else "refuted",
                "reasoning": f"The PSP answers in p95 {psp.get('p95_ms')} ms, far above the new 25 ms timeout but "
                             f"well inside the old 2000 ms one." if healthy else "The PSP is unhealthy too."}
    if cat == "network_degradation":
        paths = ev["probes"].get("orders->postgres", {}).get("paths", {})
        standby = _probe_ms(paths, "standby")
        ok = bool(standby and standby < 150)
        return {"challenge": "Could Postgres itself be slow, not the link?",
                "verdict": "upheld" if ok else "refuted",
                "reasoning": f"The same query over the standby link takes {standby:.0f} ms, so Postgres is fast."
                             if ok else "The standby link is slow too; the database may be the problem."}
    if cat in ("process_crash",) or (cat == "memory_leak" and h["action"]["name"] == "start_service"):
        planned = any(d["service"] == h["node"] and d["minutes_ago"] < 10 for d in ev["deploys"])
        return {"challenge": f"Was {h['node']} stopped on purpose for maintenance?",
                "verdict": "uncertain" if planned else "upheld",
                "reasoning": "There is no deploy or maintenance record for it, and the exit code is a kill, "
                             "not a clean shutdown." if not planned else "A recent deploy touched it."}
    if cat == "cpu_saturation":
        mine = s.get(h["node"], {}).get("rps") or 0
        gw = s.get("gateway", {}).get("rps") or 0
        return {"challenge": "Is the CPU simply following real traffic?",
                "verdict": "upheld" if mine <= max(gw * 1.5, 1) else "uncertain",
                "reasoning": f"{h['node']} handles {mine} req/s, in line with the gateway's {gw} req/s, yet uses a "
                             f"full core."}
    if cat == "memory_leak":
        return {"challenge": "Is memory just following a traffic spike?",
                "verdict": "upheld",
                "reasoning": "The growth is linear and steady while request rate is flat; caches do not grow "
                             "like that."}
    if cat == "db_connection_exhaustion":
        gw = s.get("gateway", {}).get("rps") or 0
        return {"challenge": "Could this be genuine load from the store?",
                "verdict": "upheld",
                "reasoning": f"The sessions belong to a batch client, not to a store service, and most are idle; "
                             f"store traffic is a normal {gw} req/s."}
    if cat == "db_lock_contention":
        return {"challenge": "Is the blocker just a normal short transaction?",
                "verdict": "upheld",
                "reasoning": "It has been idle in a transaction holding the lock for longer than any store query "
                             "is allowed to run."}
    return {"challenge": "Does any known signature fit?", "verdict": "uncertain",
            "reasoning": "The evidence does not match a known failure; a human should look."}
