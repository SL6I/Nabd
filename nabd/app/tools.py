"""Read-only investigation tools. The core engine calls them directly and
Claude calls them through tool use; both see the same data."""

import asyncio
import json
import time

from . import config_store, dockerctl, learned, logs, mizan, telemetry, topology

SERVICE_ENUM = topology.SERVICES
NODE_ENUM = list(topology.NODES.keys())


def _forecast(values: list[tuple[float, float | None]], limit_mb: float | None) -> dict:
    pts = [(t, v) for t, v in values if v is not None]
    if len(pts) < 5:
        return {"enough_data": False}
    n = len(pts)
    mean_t = sum(t for t, _ in pts) / n
    mean_v = sum(v for _, v in pts) / n
    var = sum((t - mean_t) ** 2 for t, _ in pts) or 1e-9
    slope = sum((t - mean_t) * (v - mean_v) for t, v in pts) / var  # bytes per second
    current = pts[-1][1]
    out = {"enough_data": True, "current_mb": round(current / 1048576, 1),
           "slope_mb_per_s": round(slope / 1048576, 2), "limit_mb": limit_mb}
    if limit_mb and slope > 0.2 * 1048576:
        out["seconds_to_limit"] = max(0, round((limit_mb * 1048576 - current) / slope))
    return out


async def list_alerts() -> dict:
    firing = await telemetry.alerts()
    return {"firing": len(firing), "alerts": [{k: a[k] for k in ("name", "service", "dependency", "severity", "summary")}
                                              for a in firing]}


async def get_service_health() -> dict:
    snap = telemetry.telemetry.latest or await telemetry.snapshot()
    return {"health": snap["health"], "services": snap["services"],
            "redis": {k: snap["infra"]["redis"].get(k) for k in ("ok", "used_mb", "max_mb", "ratio", "error")},
            "postgres": {k: snap["infra"]["postgres"].get(k) for k in
                         ("ok", "connections", "max_connections", "blocked_queries", "error")},
            "orders_db_link_p95_ms": snap["infra"]["link"]}


async def query_metrics(promql: str, range_seconds: int) -> dict:
    if range_seconds and range_seconds > 0:
        series = await telemetry.prom_range(promql, min(int(range_seconds), 900), step=max(2, int(range_seconds) // 30))
        out = []
        for s in series[:10]:
            if "error" in s:
                return {"error": s["error"]}
            vals = [v for _, v in s["values"] if v is not None]
            out.append({"labels": s["labels"], "points": len(vals),
                        "first": vals[0] if vals else None, "last": vals[-1] if vals else None,
                        "min": min(vals) if vals else None, "max": max(vals) if vals else None})
        return {"promql": promql, "range_seconds": range_seconds, "series": out}
    rows = await telemetry.prom_query(promql)
    if rows and "error" in rows[0]:
        return {"error": rows[0]["error"]}
    return {"promql": promql, "results": rows[:15]}


async def search_logs(service: str, window_seconds: int, only_problems: bool) -> dict:
    if service not in SERVICE_ENUM:
        return {"error": f"unknown service {service}"}
    return await asyncio.to_thread(logs.compress, service, window_seconds, only_problems)


async def get_recent_deploys(service: str, limit: int) -> dict:
    items = config_store.history(service if service in config_store.BASELINE else None, max(1, min(int(limit or 8), 20)))
    now = time.time()
    return {"deploys": [{"id": h["id"], "service": h["service"], "version": h["version"], "author": h["author"],
                         "message": h["message"], "diff": h["diff"], "minutes_ago": round((now - h["ts"]) / 60, 1),
                         "ts": round(h["ts"], 1)}
                        for h in reversed(items)],
            "current_versions": {s: config_store.current(s).get("version") for s in config_store.BASELINE}}


async def inspect_container(service: str) -> dict:
    if service not in topology.CONTAINERS:
        return {"error": f"unknown container {service}"}
    return await asyncio.to_thread(dockerctl.inspect, service)


async def inspect_redis() -> dict:
    return await asyncio.to_thread(telemetry.redis_stats, True)


async def inspect_postgres() -> dict:
    return await asyncio.to_thread(telemetry.pg_stats, True)


async def probe_network_path(source: str, target: str) -> dict:
    if source not in ("orders", "inventory", "payments") or target not in ("postgres", "redis", "psp"):
        return {"error": "source must be orders, inventory or payments; target postgres, redis or psp"}
    result = await asyncio.to_thread(dockerctl.exec_probe, source, target)
    result["source"] = source
    if source == "orders" and target == "postgres":
        result["note"] = "primary = the link orders uses by default (via the network proxy); standby = direct"
    return result


async def forecast_memory(service: str) -> dict:
    if service not in SERVICE_ENUM:
        return {"error": f"unknown service {service}"}
    series = await telemetry.prom_range(f'process_resident_memory_bytes{{service="{service}"}}', 60, 2)
    limit = (telemetry.telemetry.latest or {}).get("services", {}).get(service, {}).get("mem_limit_mb")
    if not series or "error" in series[0]:
        return {"service": service, "enough_data": False}
    return {"service": service, **_forecast(series[0]["values"], limit)}


async def check_invariants() -> dict:
    latest = mizan.mizan.latest or await asyncio.to_thread(mizan.mizan.check)
    out = {"window_start_ts": round(latest["since"], 1), "balanced": latest["balanced"],
           "invariants": {k: {kk: vv for kk, vv in v.items() if kk not in ("title_ar", "node", "since") and vv is not None}
                          for k, v in latest["invariants"].items()},
           "classic_alerts_firing": len(await telemetry.alerts())}
    if not latest["balanced"]:
        books = await asyncio.to_thread(mizan.reconcile)
        out["books"] = {
            "charged_and_promised_but_no_order": {"count": len(books["orphans_promised"]), "sar": books["promised_sar"],
                                                  "sample": [c["id"] for c in books["orphans_promised"][:3]]},
            "charged_but_checkout_failed": {"count": len(books["orphans_failed"]), "sar": books["failed_sar"],
                                            "sample": [c["id"] for c in books["orphans_failed"][:3]]},
            "orders_lost_before_fulfillment": {"count": len(books["lost_orders"]),
                                               "sample": [o["order_ref"] for o in books["lost_orders"][:3]]},
            "queue": books["queue"],
        }
    return out


async def check_learned_laws() -> dict:
    lr = learned.learner
    out = {k: v for k, v in lr.summary().items() if k not in ("suspects",)}
    if lr.broken():
        out["suspects"] = await asyncio.to_thread(lr.localize, True)
    return out


async def trace_item(item: str) -> dict:
    item = str(item).strip()
    if not (item.startswith("pay_") or item.startswith("ord-")):
        return {"error": "item must be a payment id (pay_…) or an order ref (ord-…)"}
    return await asyncio.to_thread(mizan.trace, item)


async def get_topology() -> dict:
    return {"depends_on": topology.DEPENDS_ON,
            "nodes": {k: v["kind"] for k, v in topology.NODES.items()}}


REGISTRY = {
    "list_alerts": list_alerts,
    "get_service_health": get_service_health,
    "query_metrics": query_metrics,
    "search_logs": search_logs,
    "get_recent_deploys": get_recent_deploys,
    "inspect_container": inspect_container,
    "inspect_redis": inspect_redis,
    "inspect_postgres": inspect_postgres,
    "probe_network_path": probe_network_path,
    "forecast_memory": forecast_memory,
    "get_topology": get_topology,
    "check_invariants": check_invariants,
    "trace_item": trace_item,
    "check_learned_laws": check_learned_laws,
}


def _obj(props: dict, description: str) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


TOOL_SCHEMAS = [
    {"name": "list_alerts", "description": "Firing Prometheus alerts right now (name, service, dependency, summary).",
     "input_schema": _obj({}, "")},
    {"name": "get_service_health",
     "description": "Current health of every node: rps, 5xx ratio, p95 latency, memory, CPU, container state, "
                    "Redis and Postgres saturation, orders DB link latency by route.",
     "input_schema": _obj({}, "")},
    {"name": "query_metrics",
     "description": "Run a PromQL query. range_seconds=0 for an instant query; >0 for a range summary "
                    "(first/last/min/max per series). Metrics: http_requests_total{service,route,status}, "
                    "http_request_duration_seconds_bucket, dependency_errors_total{service,dependency}, "
                    "dependency_duration_seconds_bucket, db_query_duration_seconds_bucket{route}, "
                    "process_resident_memory_bytes, process_cpu_seconds_total, up.",
     "input_schema": _obj({"promql": {"type": "string"}, "range_seconds": {"type": "integer"}}, "")},
    {"name": "search_logs",
     "description": "A service's recent logs compressed by Drain3 into templates with counts, level and a sample. "
                    "Set only_problems=true to keep warn/error templates only.",
     "input_schema": _obj({"service": {"type": "string", "enum": SERVICE_ENUM},
                           "window_seconds": {"type": "integer"},
                           "only_problems": {"type": "boolean"}}, "")},
    {"name": "get_recent_deploys",
     "description": "Config deploy history (newest first) with author, message, diff and age. service='all' for every service.",
     "input_schema": _obj({"service": {"type": "string", "enum": ["all", *config_store.BASELINE]},
                           "limit": {"type": "integer"}}, "")},
    {"name": "inspect_container",
     "description": "Docker state of one container: running, exit code, OOMKilled, restarts, memory use and limit.",
     "input_schema": _obj({"service": {"type": "string", "enum": topology.CONTAINERS}}, "")},
    {"name": "inspect_redis",
     "description": "Redis memory use vs maxmemory, eviction policy, key count and the largest key prefixes "
                    "(sampled, with protected flags).",
     "input_schema": _obj({}, "")},
    {"name": "inspect_postgres",
     "description": "Postgres connections vs max (by client application and state), blocked queries and who blocks them.",
     "input_schema": _obj({}, "")},
    {"name": "probe_network_path",
     "description": "Run a TCP + application-level probe from inside a service container to a dependency. "
                    "For orders→postgres it measures both the primary and the standby link.",
     "input_schema": _obj({"source": {"type": "string", "enum": ["orders", "inventory", "payments"]},
                           "target": {"type": "string", "enum": ["postgres", "redis", "psp"]}}, "")},
    {"name": "forecast_memory",
     "description": "Linear forecast of a service's memory over the last 60 s: slope and seconds until its limit.",
     "input_schema": _obj({"service": {"type": "string", "enum": SERVICE_ENUM}}, "")},
    {"name": "get_topology", "description": "The service dependency graph.", "input_schema": _obj({}, "")},
    {"name": "check_invariants",
     "description": "Mizan's business invariants, checked against records outside the services: every PSP charge has "
                    "an order (money), every order reaches fulfillment (fulfilled), every order event is accounted for "
                    "(conserved), the fulfillment worker keeps moving (progress). When unbalanced, also the books: "
                    "orphan charges split into customers who were told their order succeeded vs saw an error, and "
                    "orders lost before fulfillment, with sample ids to trace.",
     "input_schema": _obj({}, "")},
    {"name": "check_learned_laws",
     "description": "Laws Mizan learned by itself from healthy traffic: pairs of flows (requests per route, inserts "
                    "and updates per table, Redis writes per command) that always moved in a fixed ratio. Shows which "
                    "laws are broken now, which only lag behind a growing list, and suspects ranked by broken laws, "
                    "recent config changes and log lines never seen while healthy.",
     "input_schema": _obj({}, "")},
    {"name": "trace_item",
     "description": "Follow one payment id (pay_…) or order ref (ord-…) through the payments, gateway, orders and "
                    "fulfillment logs and every durable record (PSP ledger, orders table, queue, fulfillments). "
                    "Returns the hops, the records and where the item was lost.",
     "input_schema": _obj({"item": {"type": "string"}}, "")},
]
for _tool in TOOL_SCHEMAS:
    _tool["strict"] = True


async def call(name: str, args: dict) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return await fn(**args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # a broken probe should read as evidence, not crash the agent
        return {"error": f"{type(exc).__name__}: {exc}"}


def brief(name: str, r: dict) -> str:
    """One line for the War Room feed."""
    if not isinstance(r, dict):
        return str(r)[:140]
    if "error" in r and len(r) <= 2:
        return f"error: {r['error']}"[:160]
    try:
        if name == "list_alerts":
            return f"{r['firing']} alerts firing"
        if name == "get_service_health":
            bad = {k: v for k, v in r["health"].items() if v != "ok"}
            return "all nodes healthy" if not bad else "unhealthy: " + ", ".join(f"{k} ({v})" for k, v in bad.items())
        if name == "inspect_redis":
            top = (r.get("top_prefixes") or [{}])[0]
            return (f"{r.get('used_mb')}/{r.get('max_mb')} MB used ({(r.get('ratio') or 0):.0%}), {r.get('keys')} keys"
                    + (f"; largest prefix {top.get('prefix')} ≈{top.get('est_mb')} MB" if top else ""))
        if name == "inspect_postgres":
            text = f"{r.get('connections')}/{r.get('max_connections')} connections, {r.get('blocked_queries')} blocked"
            apps = r.get("by_application") or []
            return text + (f"; top client {apps[0]['application']} ({apps[0]['total']})" if apps else "")
        if name == "get_recent_deploys":
            recent = [d for d in r["deploys"] if d["minutes_ago"] <= 30]
            return f"{len(recent)} deploys in the last 30 min" + (
                ": " + "; ".join(f"{d['service']} v{d['version']} by {d['author']}" for d in recent[:3]) if recent else "")
        if name == "search_logs":
            errs = [t for t in r["templates"] if t["level"] == "error"]
            return (f"{r['raw_lines']} lines → {r['templates_found']} templates, {r['problem_lines']} problem lines"
                    + (f"; top error: {errs[0]['template'][:80]}" if errs else ""))
        if name == "probe_network_path":
            parts = []
            for path, p in r.get("paths", {}).items():
                ms = p.get("db_connect_and_query_ms") or p.get("tcp_connect_ms")
                parts.append(f"{path} {ms:.0f} ms" if ms else f"{path} failed")
            return f"{r.get('source')} → {r.get('target')}: " + ", ".join(parts)
        if name == "forecast_memory":
            if r.get("seconds_to_limit") is not None:
                return f"{r['current_mb']} MB, +{r['slope_mb_per_s']} MB/s, limit in {r['seconds_to_limit']} s"
            return f"{r.get('current_mb')} MB, stable"
        if name == "inspect_container":
            return f"{r.get('status')} (exit {r.get('exit_code')}{', OOM-killed' if r.get('oom_killed') else ''})"
        if name == "check_invariants":
            bad = [f"{k}: {v['detail']}" for k, v in r["invariants"].items() if v.get("ok") is False]
            return ("books balanced: " + "; ".join(v["detail"] for v in r["invariants"].values())[:120]
                    if not bad else "UNBALANCED · " + " · ".join(bad))[:220]
        if name == "check_learned_laws":
            if r.get("state") != "watching":
                return f"still learning ({r.get('progress', 0):.0%})"
            top = (r.get("suspects") or [{}])[0]
            return (f"{r.get('laws')} laws: {r.get('broken')} broken, {r.get('lagging', 0)} lagging"
                    + (f"; suspect {top.get('service')}" if top else ""))[:220]
        if name == "trace_item":
            hops = " → ".join(h["service"] for h in r["hops"])
            return f"{r['item']}: {hops or 'no log hops'} · {r['verdict']}"[:220]
        if name == "query_metrics":
            n = len(r.get("results", r.get("series", [])))
            return f"{n} series for {r.get('promql', '')[:70]}"
    except (KeyError, TypeError, IndexError):
        pass
    return to_text(r, 150)


def to_text(result: dict, limit: int = 7000) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '…"(truncated)"'
