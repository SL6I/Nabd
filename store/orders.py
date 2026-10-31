"""Orders: writes orders to Postgres, then publishes an order event that the
fulfillment worker consumes. It normally reaches the database over the primary
network link (through a proxy); a standby link goes direct. A config flag can
switch order writes to batched inserts."""

import json
import secrets
import threading
import time

import psycopg
import redis
from fastapi import HTTPException
from prometheus_client import Histogram

from common import DEP_ERRORS, DEP_LATENCY, create_app, get_config, log, pg_connect

app = create_app()

ROUTES = {"primary": ("toxiproxy", 5433), "standby": ("postgres", 5432)}
DEFAULT = {"version": 1, "db_route": "primary", "write_mode": "sync", "batch_size": 8, "event_queue_max": 0}
QUEUE = "q:order-events"
FLUSH_EVERY_S = 2.0
DB_ROUNDTRIP = Histogram(
    "db_query_duration_seconds", "Order write round-trip by network link", ["route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
)
events = redis.Redis(host="redis", port=6379, socket_timeout=1, socket_connect_timeout=1)

_buffer: list[dict] = []
_buffer_lock = threading.Lock()


def _config() -> dict:
    return {**DEFAULT, **get_config("orders", DEFAULT)}


def _write(rows: list[dict]) -> str:
    """Insert orders in one transaction over the configured link. Returns the route used."""
    route = _config().get("db_route", "primary")
    host, port = ROUTES.get(route, ROUTES["primary"])
    start = time.perf_counter()
    try:
        with pg_connect(host, port, timeout=4) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO orders (order_ref, user_name, sku, qty, total_sar, payment_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (order_ref) DO NOTHING",
                    [(r["order_ref"], r["user"], r["sku"], r["qty"], r["total_sar"], r["payment_id"]) for r in rows],
                )
            conn.commit()
    except psycopg.Error as exc:
        DEP_ERRORS.labels("postgres").inc()
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        log("error", f"order write failed via {route} link: {message}")
        raise
    finally:
        elapsed = time.perf_counter() - start
        DB_ROUNDTRIP.labels(route).observe(elapsed)
        DEP_LATENCY.labels("postgres").observe(elapsed)
    if elapsed > 0.5:
        log("warn", f"slow database round-trip via {route} link: {int(elapsed * 1000)}ms")
    return route


def _publish(rows: list[dict]) -> None:
    """Tell fulfillment about committed orders."""
    cap = int(_config().get("event_queue_max") or 0)
    try:
        pipe = events.pipeline(transaction=True)
        for r in rows:
            pipe.lpush(QUEUE, json.dumps({"order_ref": r["order_ref"], "sku": r["sku"], "qty": r["qty"],
                                          "payment_id": r["payment_id"], "ts": time.time()}))
            pipe.incr("q:produced")
        if cap > 0:
            pipe.ltrim(QUEUE, 0, cap - 1)  # keeps the newest `cap` events
        pipe.execute()
    except redis.RedisError as exc:
        DEP_ERRORS.labels("redis").inc()
        log("error", f"could not publish {len(rows)} order event(s): {exc}")


def _flusher() -> None:
    while True:
        time.sleep(FLUSH_EVERY_S)
        size = max(1, int(_config().get("batch_size", 8)))
        with _buffer_lock:
            batch = _buffer[:size]
            _buffer.clear()
        if not batch:
            continue
        try:
            _write(batch)
        except psycopg.Error:
            continue
        _publish(batch)
        log("info", f"flushed batch of {len(batch)} orders")


threading.Thread(target=_flusher, daemon=True).start()


@app.post("/orders")
def create_order(body: dict):
    row = {"order_ref": f"ord-{secrets.token_hex(5)}", "user": body.get("user"), "sku": body.get("sku"),
           "qty": int(body.get("qty", 1)), "total_sar": float(body.get("total_sar", 0)),
           "payment_id": body.get("payment_id")}
    if _config().get("write_mode") == "batched":
        with _buffer_lock:
            _buffer.append(row)
        log("info", f"queued order {row['order_ref']} for batch insert (payment {row['payment_id']})")
        return {"order_id": row["order_ref"], "write_mode": "batched"}
    try:
        route = _write([row])
    except psycopg.Error as exc:
        raise HTTPException(503, "order store unavailable") from exc
    _publish([row])
    return {"order_id": row["order_ref"], "db_route": route}
