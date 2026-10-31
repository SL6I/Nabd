"""The remediation catalog and its safety gate. Nabd (and Claude) can only
propose actions from this list; the gate validates parameters, rates the
risk, sizes the blast radius and decides whether a human must approve."""

import time

import httpx
import psycopg
import redis

from . import config_store, dockerctl, mizan, topology
from .settings import GATEWAY_URL, PG_ADMIN, REDIS_HOST, runtime
from .telemetry import PROTECTED_REDIS_PREFIXES

_redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=3)
_http = httpx.Client(timeout=3.0)

PROTECTED_DB_APPS = {"", "nabd", "postgres", "gateway", "auth", "inventory", "orders", "payments", "psp",
                     "fulfillment", "catalog-sync"}

CATALOG = {
    "restart_service": {
        "summary": "Restart a container. Clears in-process state such as leaks or stuck threads.",
        "params": ["service"],
        "reversible": False,
    },
    "start_service": {
        "summary": "Start a container that has stopped or crashed.",
        "params": ["service"],
        "reversible": False,
    },
    "flush_redis_keys": {
        "summary": "Delete every Redis key under one prefix (protected prefixes refused).",
        "params": ["prefix"],
        "reversible": False,
    },
    "rollback_config": {
        "summary": "Re-deploy the previous config version of a service.",
        "params": ["service"],
        "reversible": True,
    },
    "switch_db_route": {
        "summary": "Move the orders service between its primary and standby database links.",
        "params": ["service", "route"],
        "reversible": True,
    },
    "terminate_db_sessions": {
        "summary": "Terminate every Postgres session of one client application (store services refused).",
        "params": ["application_name"],
        "reversible": False,
    },
    # Mizan's repairs: they fix the data a failure left behind, after the cause itself is fixed
    "replay_orphan_payments": {
        "summary": "Recreate the orders of customers who paid and were told their order succeeded, from the PSP's "
                   "charge metadata and under the order number they received (service=orders).",
        "params": ["service"],
        "reversible": False,
    },
    "refund_failed_payments": {
        "summary": "Refund, at the PSP, charges whose checkout failed after the money was taken (service=payments).",
        "params": ["service"],
        "reversible": False,
    },
    "replay_lost_events": {
        "summary": "Re-publish the order events of paid orders that fell out of the queue; fulfillment books each "
                   "order at most once (service=events).",
        "params": ["service"],
        "reversible": False,
    },
    "quarantine_stuck_message": {
        "summary": "Move the message the fulfillment worker is stuck on to the dead-letter queue, restart the "
                   "worker, and hand that message to a human (service=fulfillment).",
        "params": ["service"],
        "reversible": False,
    },
    "restore_catalog_prices": {
        "summary": "Set every product price that differs from the supplier's catalog back to the supplier's price, "
                   "and hand the orders sold at a wrong price to a human (service=inventory).",
        "params": ["service"],
        "reversible": False,
    },
}
REPAIR_TARGET = {"replay_orphan_payments": "orders", "refund_failed_payments": "payments",
                 "replay_lost_events": "events", "quarantine_stuck_message": "fulfillment",
                 "restore_catalog_prices": "inventory"}
MONEY_ACTIONS = {"replay_orphan_payments", "refund_failed_payments"}  # they spend stock or money


def money_at_stake(name: str) -> float:
    books = mizan.reconcile(grace_s=8)
    return books["promised_sar"] if name == "replay_orphan_payments" else books["failed_sar"]

RISK_RANK = {"low": 0, "medium": 1, "high": 2}

# R2Act (arXiv 2607.04623): the strongest RAG-based LLMs name the faulty service 91.4-99.7% of the time,
# yet choose a valid recovery only 36.8-60.3% of the time. An action runs only if it can undo the
# diagnosed cause on the diagnosed component.
VALID_FIXES = {
    "cache_memory_exhaustion": {"flush_redis_keys", "restart_service"},
    "bad_config_deploy": {"rollback_config"},
    "network_degradation": {"switch_db_route"},
    "process_crash": {"start_service", "restart_service"},
    "cpu_saturation": {"restart_service"},
    "memory_leak": {"restart_service", "start_service"},
    "db_connection_exhaustion": {"terminate_db_sessions"},
    "db_lock_contention": {"terminate_db_sessions"},
    "orphaned_payments": {"replay_orphan_payments", "refund_failed_payments"},
    "lost_events": {"replay_lost_events"},
    "poison_message": {"quarantine_stuck_message"},
    "corrupted_data": {"restore_catalog_prices"},
    "silent_data_loss": set(),
    "dependency_failure": set(),
    "other": set(),
}
WHY_NOT = {
    "bad_config_deploy": "a restart re-reads the same bad config; only a rollback removes the bad value",
    "network_degradation": "restarting a service does not repair the network path it uses",
    "db_connection_exhaustion": "the hoarding client keeps its sessions until they are terminated",
    "db_lock_contention": "the lock belongs to another session; restarting the blocked services does not release it",
    "cache_memory_exhaustion": "the leaked keys stay in Redis until they are deleted",
    "cpu_saturation": "only a restart stops the runaway work inside the process",
    "dependency_failure": "the failing dependency is outside Nabd's control, so the right move is to escalate",
    "orphaned_payments": "the charges stay without orders until they are recreated or refunded",
    "lost_events": "a lost event never comes back by itself; only re-publishing it gets the order shipped",
    "poison_message": "restarting the worker alone puts the same message back at the head of the line",
    "silent_data_loss": "the loss comes from code, not config: an engineer must fix it",
    "corrupted_data": "the wrong values stay in the table until they are restored from the source of truth",
    "other": "no catalog action is known to fix an unclassified failure",
}
EXTERNAL = {n for n, meta in topology.NODES.items() if meta["kind"] == "external"}


def target_node(name: str, params: dict) -> str:
    if name == "flush_redis_keys":
        return "redis"
    if name == "terminate_db_sessions":
        return "postgres"
    if name == "switch_db_route":
        return "orders-db-link"
    if name in REPAIR_TARGET:
        return REPAIR_TARGET[name]
    return params.get("service", "?")


def validate(name: str, params: dict) -> str | None:
    """Return an error message, or None when the action is allowed."""
    if name not in CATALOG:
        return f"unknown action {name}"
    if name in ("restart_service", "start_service"):
        service = params.get("service")
        if service == "postgres":
            return "restarting Postgres is never automated: it drops every in-flight transaction"
        if service in EXTERNAL:
            return f"{service} is an external provider; Nabd cannot restart someone else's system"
        if service not in topology.STATELESS and service != "redis":
            return f"{service} is not a restartable service"
    if name == "flush_redis_keys":
        prefix = str(params.get("prefix", ""))
        if len(prefix) < 4 or "*" in prefix or ":" not in prefix:
            return "prefix must be a concrete namespace like 'tmp:session-dump:'"
        if prefix.startswith(PROTECTED_REDIS_PREFIXES):
            return f"{prefix} holds live reservations, payments or cache and is protected"
    if name == "rollback_config" and params.get("service") not in config_store.BASELINE:
        return f"{params.get('service')} has no managed config"
    if name == "switch_db_route":
        if params.get("service") != "orders":
            return "only orders has a standby database link"
        if params.get("route") not in ("primary", "standby"):
            return "route must be primary or standby"
    if name == "terminate_db_sessions" and str(params.get("application_name", "")) in PROTECTED_DB_APPS:
        return "refusing to terminate sessions of a store service or of Nabd itself"
    if name in REPAIR_TARGET and params.get("service") != REPAIR_TARGET[name]:
        return f"{name} only works on {REPAIR_TARGET[name]}"
    return None


def check_validity(hyp: dict, name: str, params: dict) -> tuple[bool, str]:
    """Does this action undo this diagnosis, on this component?"""
    category = hyp.get("category", "other")
    label = category.replace("_", " ")
    if name not in VALID_FIXES.get(category, set()):
        return False, f"{name} cannot fix {label}: {WHY_NOT.get(category, 'it does not address this cause')}"
    target = target_node(name, params)
    if target != hyp["node"]:
        return False, f"{name} acts on {target}, but the diagnosed root cause is {hyp['node']}"
    return True, f"valid fix for {label} on {hyp['node']}"


def canonical_fix(hyp: dict) -> dict | None:
    """The one fix that follows from a diagnosis without extra parameters, if there is one."""
    node, category = hyp["node"], hyp["category"]
    if category == "bad_config_deploy" and node in config_store.BASELINE:
        return {"name": "rollback_config", "params": {"service": node}}
    if category == "network_degradation" and node == "orders-db-link":
        return {"name": "switch_db_route", "params": {"service": "orders", "route": "standby"}}
    if category in ("process_crash", "memory_leak", "cpu_saturation") and (node in topology.STATELESS or node == "redis") \
            and node not in EXTERNAL:
        return {"name": "restart_service", "params": {"service": node}}  # docker restart also starts a stopped container
    for name, target in REPAIR_TARGET.items():
        if target == node and name in VALID_FIXES.get(category, set()):
            return {"name": name, "params": {"service": target}}
    return None


def preflight(name: str, params: dict) -> tuple[bool, str]:
    """A dry run: is the action executable right now, and what exactly would it change?"""
    try:
        if name in ("restart_service", "start_service"):
            info = dockerctl.inspect(params["service"])
            if "error" in info:
                return False, f"no container for {params['service']}"
            if name == "start_service" and info.get("running"):
                return False, f"{params['service']} is already running, so starting it would change nothing"
            return True, f"{params['service']} container is {info.get('status')}"
        if name == "flush_redis_keys":
            count = sum(1 for _ in _redis.scan_iter(match=f"{params['prefix']}*", count=1000))
            if not count:
                return False, f"no keys under {params['prefix']}"
            return True, f"{count} keys under {params['prefix']} would be deleted; protected prefixes untouched"
        if name == "rollback_config":
            latest = (config_store.history(params["service"], 1) or [{}])[-1]
            if latest.get("author") == "nabd":
                return False, (f"the current {params['service']} config is already Nabd's own change; rolling back "
                               f"again would re-apply the version it replaced")
            preview = config_store.rollback_preview(params["service"])
            if not preview["changes"]:
                return False, f"v{preview['restored_version']} is identical to the current config"
            diff = ", ".join(f"{k} {preview['current'].get(k)} → {v}" for k, v in preview["changes"].items())
            return True, f"v{preview['current_version']} → config of v{preview['restored_version']}: {diff}"
        if name == "switch_db_route":
            if config_store.current("orders").get("db_route") == params["route"]:
                return False, f"orders already uses the {params['route']} link"
            probe = dockerctl.exec_probe("orders", "postgres").get("paths", {}).get(params["route"], {})
            ms = probe.get("db_connect_and_query_ms")
            if not ms or ms > 300:
                return False, f"the {params['route']} link is not healthy either ({probe.get('error') or ms})"
            return True, f"the {params['route']} link answers in {ms:.0f} ms"
        if name == "terminate_db_sessions":
            with psycopg.connect(**PG_ADMIN, connect_timeout=3, application_name="nabd") as conn:
                count = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s "
                                     "AND usename <> 'postgres'", (params["application_name"],)).fetchone()[0]
            if not count:
                return False, f"{params['application_name']} has no sessions to terminate"
            return True, f"{count} sessions of {params['application_name']} would be terminated"
        if name == "replay_orphan_payments":
            books = mizan.reconcile(grace_s=8)
            n = len(books["orphans_promised"])
            if not n:
                return False, "no customer is waiting on a promised order that does not exist"
            return True, (f"{n} orders ({books['promised_sar']:,.0f} SAR) would be recreated under the order numbers "
                          f"the customers already have; {len(books['orphans_failed'])} failed checkouts are left "
                          f"for a refund, never turned into orders")
        if name == "refund_failed_payments":
            books = mizan.reconcile(grace_s=8)
            n = len(books["orphans_failed"])
            if not n:
                return False, "no charge without a checkout is waiting for a refund"
            return True, f"{n} charges ({books['failed_sar']:,.0f} SAR) would be refunded at the PSP"
        if name == "replay_lost_events":
            _, lost = mizan.lost_orders(mizan.mizan.since, 5)
            if not lost:
                return False, "every order is fulfilled or still in the queue"
            return True, (f"{len(lost)} order events would be re-published (e.g. {lost[0]['order_ref']}); "
                          f"fulfillment ignores an order it already booked")
        if name == "restore_catalog_prices":
            wrong = mizan.price_drift()["wrong"]
            if not wrong:
                return False, "every price already matches the supplier's catalog"
            w = wrong[0]
            return True, (f"{len(wrong)} prices would be set back to the supplier's, e.g. {w['sku']} "
                          f"{w['price']:.2f} → {w['truth']:.2f} SAR")
        if name == "quarantine_stuck_message":
            cur = mizan.queue_state()["current"]
            if not cur or cur["age_s"] < mizan.STALL_S:
                return False, "the worker is not stuck on any message"
            return True, (f"{cur['order_ref']} ({cur['sku']}) has been at “{cur['step']}” for {cur['age_s']:.0f} s; it "
                          f"would move to the dead-letter queue and fulfillment would restart to drop the hung call")
    except Exception as exc:  # a failed dry run blocks the action; it never crashes the agent
        return False, f"dry run failed: {type(exc).__name__}: {exc}"
    return False, f"no dry run defined for {name}"


def risk(name: str, params: dict) -> str:
    if name == "start_service":
        return "low"
    if name == "restart_service":
        return "medium" if params.get("service") == "redis" else "low"
    if name in ("flush_redis_keys", "terminate_db_sessions", "switch_db_route",
                "replay_orphan_payments", "quarantine_stuck_message", "restore_catalog_prices"):
        return "medium"
    if name == "replay_lost_events":
        return "low"  # idempotent: fulfillment books an order at most once
    if name == "refund_failed_payments":
        return "high"  # moves money
    if name == "rollback_config":
        return "high"
    return "high"


def needs_approval(level: str) -> bool:
    if runtime.auto_approve:
        return False
    if level == "high":
        return True
    return level == "medium" and runtime.mode == "supervised"


def blast_radius(name: str, params: dict) -> list[str]:
    node = target_node(name, params)
    return [node] + topology.upstream_closure(node)


def describe(name: str, params: dict) -> str:
    if name == "restart_service":
        return f"Restart {params['service']}"
    if name == "start_service":
        return f"Start {params['service']}"
    if name == "flush_redis_keys":
        return f"Delete Redis keys under {params['prefix']}*"
    if name == "rollback_config":
        return f"Roll back {params['service']} config to the previous version"
    if name == "switch_db_route":
        return f"Move {params['service']} to the {params['route']} database link"
    if name == "terminate_db_sessions":
        return f"Terminate Postgres sessions of {params['application_name']}"
    if name == "replay_orphan_payments":
        return "Recreate the orders customers paid for and were promised"
    if name == "refund_failed_payments":
        return "Refund the charges of failed checkouts"
    if name == "replay_lost_events":
        return "Re-publish the lost order events"
    if name == "quarantine_stuck_message":
        return "Move the stuck message to the dead-letter queue and restart fulfillment"
    if name == "restore_catalog_prices":
        return "Restore the catalog prices from the supplier"
    return name


def _wait_healthy(service: str, timeout_s: float = 20) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if _http.get(f"http://{service}:8000/health").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def execute(name: str, params: dict) -> dict:
    """Run an approved action. Blocking; call through asyncio.to_thread."""
    if name in ("restart_service", "start_service"):
        service = params["service"]
        (dockerctl.restart if name == "restart_service" else dockerctl.start)(service)
        healthy = service in ("redis",) or _wait_healthy(service)
        return {"ok": healthy, "detail": f"{service} {'is back and healthy' if healthy else 'did not report healthy in 20s'}"}

    if name == "flush_redis_keys":
        prefix = params["prefix"]
        before = _redis.info("memory")["used_memory"]
        deleted, batch = 0, []
        for key in _redis.scan_iter(match=f"{prefix}*", count=500):
            batch.append(key)
            if len(batch) >= 200:
                deleted += _redis.unlink(*batch)
                batch.clear()
        if batch:
            deleted += _redis.unlink(*batch)
        time.sleep(0.5)
        freed = (before - _redis.info("memory")["used_memory"]) / 1048576
        return {"ok": deleted > 0, "detail": f"deleted {deleted} keys under {prefix}, freed {freed:.1f} MB"}

    if name == "rollback_config":
        undo = {k: v for k, v in config_store.current(params["service"]).items() if k != "version"}
        entry = config_store.rollback(params["service"])
        return {"ok": True, "detail": f"{params['service']} v{entry['rolled_back_from']} → v{entry['version']} "
                                      f"(config of v{entry['restored_version']}): {entry['diff']}",
                "revert": None,  # the error-rate guard leaves rollbacks alone; only a broken law undoes one
                "undo": {"name": "restore_config", "params": {"service": params["service"], "config": undo}}}

    if name == "restore_config":  # only the no-regression guard calls this, to undo its own rollback
        entry = config_store.deploy(params["service"], params["config"], author="nabd",
                                    message="no-regression guard: undo Nabd's rollback")
        return {"ok": True, "detail": f"{params['service']} back to v{entry['version']}: {entry['diff']}"}

    if name == "switch_db_route":
        before = config_store.current("orders").get("db_route")
        entry = config_store.deploy("orders", {"db_route": params["route"]}, author="nabd",
                                    message=f"failover: orders database link {before} → {params['route']}")
        return {"ok": True, "detail": f"orders now uses the {params['route']} link (config v{entry['version']})",
                "revert": {"name": "switch_db_route", "params": {"service": "orders", "route": before}}}

    if name == "terminate_db_sessions":
        app_name = params["application_name"]
        with psycopg.connect(**PG_ADMIN, connect_timeout=3, autocommit=True, application_name="nabd") as conn:
            rows = conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = %s AND usename <> 'postgres'", (app_name,)).fetchall()
        killed = sum(1 for r in rows if r[0])
        return {"ok": killed > 0, "detail": f"terminated {killed} sessions of {app_name}"}

    if name == "replay_orphan_payments":
        r = mizan.replay_orphans()
        return {"ok": r["recreated"] > 0, "amount_sar": r["amount_sar"], "count": r["recreated"],
                "detail": f"recreated {r['recreated']} orders ({r['amount_sar']:,.0f} SAR) and sent them to "
                          f"fulfillment" + (f", e.g. {r['refs'][0]}" if r["refs"] else "")}

    if name == "refund_failed_payments":
        r = mizan.refund_failed()
        return {"ok": r["refunded"] > 0, "amount_sar": r["amount_sar"], "count": r["refunded"],
                "detail": f"refunded {r['refunded']} of {r['of']} charges ({r['amount_sar']:,.0f} SAR) at the PSP"}

    if name == "replay_lost_events":
        r = mizan.replay_lost()
        return {"ok": r["replayed"] > 0, "count": r["replayed"],
                "detail": f"re-published {r['replayed']} order events" + (f", e.g. {r['refs'][0]}" if r["refs"] else "")}

    if name == "restore_catalog_prices":
        r = mizan.restore_prices()
        out = {"ok": r["restored"] > 0, "count": r["restored"],
               "detail": f"restored {r['restored']} prices from the supplier's catalog"}
        if r["orders"]:
            out["handoff"] = {"what": f"{r['orders']} orders were sold below the supplier's price "
                                      f"({r['under_sar']:,.0f} SAR under)",
                              "why": "re-billing a customer after the sale is a business decision, not a repair",
                              "ask": "decide whether to honor these prices or contact the customers"}
        return out

    if name == "quarantine_stuck_message":
        r = mizan.quarantine_current()
        if not r["ok"]:
            return {"ok": False, "detail": r["detail"]}
        dockerctl.restart("fulfillment")
        healthy = _wait_healthy("fulfillment")
        return {"ok": healthy,
                "detail": f"{r['order_ref']} ({r['sku']}) is in the dead-letter queue and fulfillment restarted"
                          f"{'' if healthy else ' but did not report healthy'}",
                "handoff": {"what": f"order {r['order_ref']} ({r['sku']}) is parked in the dead-letter queue",
                            "why": f"the worker hung at “{r['step']}”",
                            "ask": "book this shipment by hand once the carrier answers, then add a timeout to the "
                                   "carrier call"}}

    return {"ok": False, "detail": f"no executor for {name}"}


def gateway_error_ratio_quick() -> float:
    """Six quick probes through the gateway, used by the post-action guard."""
    failures = 0
    for _ in range(6):
        try:
            if _http.get(f"{GATEWAY_URL}/api/products").status_code >= 500:
                failures += 1
        except httpx.HTTPError:
            failures += 1
    return failures / 6
