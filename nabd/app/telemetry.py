"""Telemetry: Prometheus queries, Redis and Postgres internals, container
states, and the health model that colors the topology."""

import asyncio
import math
import time

import httpx
import psycopg
import redis

from . import dockerctl
from .bus import bus
from .learned import learner
from .mizan import mizan
from .settings import PG_ADMIN, PROM_URL, REDIS_HOST, runtime
from .topology import SERVICES

_http = httpx.AsyncClient(timeout=5.0)
_redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=2, socket_connect_timeout=2)

PROTECTED_REDIS_PREFIXES = ("hold:", "idem:", "cache:", "q:", "fulfillment:")


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


async def prom_query(expr: str) -> list[dict]:
    try:
        resp = await _http.get(f"{PROM_URL}/api/v1/query", params={"query": expr})
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if data.get("status") != "success":
        return [{"error": data.get("error", "query failed")}]
    out = []
    for item in data["data"].get("result", []):
        value = item.get("value", [None, None])[1]
        out.append({"labels": item.get("metric", {}), "value": _num(value)})
    return out


async def prom_range(expr: str, seconds: int, step: int = 2) -> list[dict]:
    end = time.time()
    try:
        resp = await _http.get(f"{PROM_URL}/api/v1/query_range",
                               params={"query": expr, "start": end - seconds, "end": end, "step": step})
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if data.get("status") != "success":
        return [{"error": data.get("error", "query failed")}]
    return [{"labels": r.get("metric", {}), "values": [(float(t), _num(v)) for t, v in r.get("values", [])]}
            for r in data["data"].get("result", [])]


async def alerts() -> list[dict]:
    try:
        resp = await _http.get(f"{PROM_URL}/api/v1/alerts")
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for a in data.get("data", {}).get("alerts", []):
        if a.get("state") != "firing":
            continue
        labels = a.get("labels", {})
        out.append({
            "name": labels.get("alertname"),
            "service": labels.get("service", "?"),
            "dependency": labels.get("dependency"),
            "severity": labels.get("severity", "warning"),
            "summary": a.get("annotations", {}).get("summary", ""),
            "active_at": a.get("activeAt"),
            "key": f"{labels.get('alertname')}:{labels.get('service')}:{labels.get('dependency') or ''}",
        })
    return out


def redis_stats(sample_prefixes: bool = False) -> dict:
    try:
        info = _redis.info("memory")
        stats = _redis.info("stats")
        out = {
            "ok": True,
            "used_mb": round(info["used_memory"] / 1048576, 1),
            "max_mb": round(info.get("maxmemory", 0) / 1048576, 1) or None,
            "policy": info.get("maxmemory_policy"),
            "keys": _redis.dbsize(),
            "rejected_writes": stats.get("total_error_replies", 0),
        }
        out["ratio"] = round(out["used_mb"] / out["max_mb"], 3) if out["max_mb"] else None
        if sample_prefixes:
            out["top_prefixes"] = _sample_prefixes()
        return out
    except redis.RedisError as exc:
        return {"ok": False, "error": str(exc)}


def _sample_prefixes(limit: int = 50000) -> list[dict]:
    groups: dict[str, dict] = {}
    seen = 0
    for key in _redis.scan_iter(count=500):
        seen += 1
        name = key.decode(errors="replace")
        parts = name.split(":")
        prefix = ":".join(parts[:2]) + ":" if len(parts) > 2 else parts[0] + ":"
        g = groups.setdefault(prefix, {"prefix": prefix, "keys": 0, "sampled_bytes": 0, "sampled": 0})
        g["keys"] += 1
        if "ttl_s" not in g:
            try:
                g["ttl_s"] = _redis.ttl(key)  # -1 means the key never expires
            except redis.RedisError:
                g["ttl_s"] = None
        if g["sampled"] < 20:
            try:
                g["sampled_bytes"] += _redis.memory_usage(key) or 0
                g["sampled"] += 1
            except redis.RedisError:
                pass
        if seen >= limit:
            break
    out = []
    for g in groups.values():
        avg = g["sampled_bytes"] / g["sampled"] if g["sampled"] else 0
        out.append({"prefix": g["prefix"], "keys": g["keys"], "est_mb": round(avg * g["keys"] / 1048576, 1),
                    "sample_ttl_s": g.get("ttl_s"),
                    "protected": g["prefix"].startswith(PROTECTED_REDIS_PREFIXES)})
    return sorted(out, key=lambda g: g["est_mb"], reverse=True)[:6]


def pg_stats(detail: bool = False) -> dict:
    try:
        with psycopg.connect(**PG_ADMIN, connect_timeout=2, application_name="nabd") as conn:
            max_conn = int(conn.execute("SHOW max_connections").fetchone()[0])
            reserved = int(conn.execute("SHOW superuser_reserved_connections").fetchone()[0])
            rows = conn.execute(
                "SELECT coalesce(nullif(application_name, ''), usename, 'unknown'), state, count(*) "
                "FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1, 2"
            ).fetchall()
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0"
            ).fetchone()[0]
            out = {"ok": True, "max_connections": max_conn, "reserved_for_admin": reserved,
                   "connections": sum(r[2] for r in rows), "blocked_queries": blocked}
            out["ratio"] = round(out["connections"] / max(1, max_conn - reserved), 3)
            if detail:
                by_app: dict[str, dict] = {}
                for app, state, count in rows:
                    entry = by_app.setdefault(app, {"application": app, "total": 0, "states": {}})
                    entry["total"] += count
                    entry["states"][state or "unknown"] = count
                out["by_application"] = sorted(by_app.values(), key=lambda e: e["total"], reverse=True)
                locks = conn.execute(
                    "SELECT blocked.pid, blocked.application_name, left(blocked.query, 90), "
                    "round(extract(epoch FROM now() - blocked.query_start)::numeric, 1), "
                    "blocker.pid, blocker.application_name, blocker.state, left(blocker.query, 90), "
                    "round(extract(epoch FROM now() - blocker.xact_start)::numeric, 1) "
                    "FROM pg_stat_activity blocked "
                    "JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true "
                    "JOIN pg_stat_activity blocker ON blocker.pid = b.pid LIMIT 12"
                ).fetchall()
                out["blocking"] = [{
                    "blocked_pid": r[0], "blocked_app": r[1], "blocked_query": r[2], "waiting_s": float(r[3] or 0),
                    "blocker_pid": r[4], "blocker_app": r[5], "blocker_state": r[6], "blocker_query": r[7],
                    "blocker_txn_age_s": float(r[8] or 0),
                } for r in locks]
            return out
    except psycopg.Error as exc:
        return {"ok": False, "error": str(exc).strip().splitlines()[0] if str(exc).strip() else "connect failed"}


# ---------------------------------------------------------------- snapshot

def _by_service(rows: list[dict], key: str = "service") -> dict:
    return {r["labels"].get(key): r["value"] for r in rows if "labels" in r}


def health_model(svc: dict, infra: dict, containers: dict) -> dict:
    nodes = {"traffic": "ok"}
    for name in SERVICES:
        s = svc.get(name, {})
        c = containers.get(name, {})
        status = "ok"
        if c and not c.get("running", True):
            status = "down"
        elif s.get("up") == 0:
            status = "down"
        elif (s.get("err") or 0) > 0.2:
            status = "crit"
        elif ((s.get("err") or 0) > 0.03 or (s.get("p95_ms") or 0) > 800 or (s.get("cpu") or 0) > 0.85
              or (s.get("err4xx") or 0) > 0.25):
            status = "warn"
        nodes[name] = status
    r = infra.get("redis", {})
    nodes["redis"] = "down" if not r.get("ok") else ("crit" if (r.get("ratio") or 0) >= 0.95 else
                                                      "warn" if (r.get("ratio") or 0) >= 0.8 else "ok")
    p = infra.get("postgres", {})
    nodes["postgres"] = "down" if not p.get("ok") else (
        "crit" if (p.get("ratio") or 0) >= 0.9 or (p.get("blocked_queries") or 0) > 0 else
        "warn" if (p.get("ratio") or 0) >= 0.7 else "ok")
    link = infra.get("link", {}).get("primary_p95_ms")
    nodes["orders-db-link"] = "crit" if (link or 0) > 500 else "warn" if (link or 0) > 200 else "ok"
    nodes["events"] = "down" if not r.get("ok") else "ok"
    nodes["carrier"] = nodes["supplier"] = "ok"  # someone else's systems: Nabd only sees them through ours
    rank = {"ok": 0, "warn": 1, "crit": 2, "down": 3}
    for node, status in {**learner.node_health(), **mizan.node_health()}.items():  # broken books make a "green" node sick
        if rank[status] > rank.get(nodes.get(node, "ok"), 0):
            nodes[node] = status
    return nodes


async def snapshot() -> dict:
    queries = {
        "rps": 'sum by (service) (rate(http_requests_total[15s]))',
        "err5xx": 'sum by (service) (rate(http_requests_total{status=~"5.."}[15s]))',
        "err4xx": 'sum by (service) (rate(http_requests_total{status=~"4.."}[15s]))',
        "p95": 'histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[15s])))',
        "mem": 'process_resident_memory_bytes{job="dukkan"}',
        "cpu": 'rate(process_cpu_seconds_total{job="dukkan"}[15s])',
        "up": 'up{job="dukkan"}',
        "link": 'histogram_quantile(0.95, sum by (route, le) (rate(db_query_duration_seconds_bucket[15s])))',
    }
    results = await asyncio.gather(*(prom_query(q) for q in queries.values()), alerts())
    data = dict(zip(queries.keys(), results[:-1]))
    classic = results[-1]
    firing = classic + mizan.alerts() + learner.alerts()
    redis_info, pg_info, containers = await asyncio.gather(
        asyncio.to_thread(redis_stats), asyncio.to_thread(pg_stats), asyncio.to_thread(dockerctl.states))

    rps, err, p95 = _by_service(data["rps"]), _by_service(data["err5xx"]), _by_service(data["p95"])
    err4xx = _by_service(data["err4xx"])
    mem, cpu, up = _by_service(data["mem"]), _by_service(data["cpu"]), _by_service(data["up"])
    services = {}
    for name in SERVICES:
        r = rps.get(name)
        services[name] = {
            "rps": round(r, 2) if r is not None else None,
            "err": round((err.get(name) or 0) / r, 4) if r else (0.0 if r == 0 else None),
            "err4xx": round((err4xx.get(name) or 0) / r, 4) if r else None,
            "p95_ms": round(p95[name] * 1000) if p95.get(name) is not None else None,
            "mem_mb": round(mem[name] / 1048576, 1) if mem.get(name) is not None else None,
            "cpu": round(cpu[name], 3) if cpu.get(name) is not None else None,
            "up": up.get(name),
            "container": containers.get(name, {}).get("status"),
            "mem_limit_mb": containers.get(name, {}).get("memory_limit_mb"),
        }
    link = _by_service(data["link"], key="route")
    infra = {
        "redis": redis_info,
        "postgres": pg_info,
        "link": {
            "primary_p95_ms": round(link["primary"] * 1000) if link.get("primary") is not None else None,
            "standby_p95_ms": round(link["standby"] * 1000) if link.get("standby") is not None else None,
        },
    }
    snap = {
        "at": time.time(),
        "services": services,
        "infra": infra,
        "alerts": firing,
        "classic_alerts": len(classic),
        "mizan": mizan.latest if runtime.business_invariants else None,
        "learned": learner.summary(),
        "containers": {k: v.get("status") for k, v in containers.items()},
    }
    snap["health"] = health_model(services, infra, containers)
    return snap


class TelemetryLoop:
    def __init__(self) -> None:
        self.latest: dict | None = None

    async def run(self) -> None:
        while True:
            started = time.time()
            try:
                self.latest = await snapshot()
                bus.publish("telemetry", data=self.latest)
            except Exception as exc:  # telemetry must never take the agent down
                bus.publish("info", level="warn", title=f"telemetry error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(0.5, 2.0 - (time.time() - started)))


telemetry = TelemetryLoop()
