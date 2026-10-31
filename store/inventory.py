"""Inventory: product catalog (Postgres, cached in Redis) and stock
reservations. Reservation holds live in Redis, so a full Redis breaks
checkout even though catalog reads degrade gracefully."""

import json
import secrets
import time

import psycopg
import redis
from fastapi import HTTPException

from common import DEP_ERRORS, DEP_LATENCY, create_app, get_config, log, pg_connect

app = create_app()
_clients: dict[str, redis.Redis] = {}
_deferred: list[tuple[str, int]] = []  # stock decrements waiting for the background applier


def cache() -> redis.Redis:
    """The Redis client for the configured cache host (hot-reloaded config)."""
    host = get_config("inventory", {"cache_host": "redis"}).get("cache_host", "redis")
    if host not in _clients:
        _clients[host] = redis.Redis(host=host, port=6379, socket_timeout=1, socket_connect_timeout=1)
    return _clients[host]


def _db(query: str, params: tuple = (), fetch: bool = True):
    start = time.perf_counter()
    try:
        with pg_connect() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
    except psycopg.Error as exc:
        DEP_ERRORS.labels("postgres").inc()
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        log("error", f"database query failed: {message}")
        raise HTTPException(503, "inventory database unavailable") from exc
    finally:
        DEP_LATENCY.labels("postgres").observe(time.perf_counter() - start)


@app.get("/products")
def products():
    try:
        cached = cache().get("cache:products")
    except redis.RedisError as exc:
        DEP_ERRORS.labels("redis").inc()
        log("warn", f"catalog cache read failed, falling back to database: {exc}")
        cached = None
    if cached:
        return json.loads(cached)

    rows = _db("SELECT sku, name_en, name_ar, price_sar FROM products ORDER BY sku")
    items = [{"sku": r[0], "name_en": r[1], "name_ar": r[2], "price_sar": float(r[3])} for r in rows]
    ttl = int(get_config("inventory", {"cache_ttl_s": 30}).get("cache_ttl_s", 30))
    try:
        cache().setex("cache:products", ttl, json.dumps(items, ensure_ascii=False))
    except redis.RedisError as exc:
        DEP_ERRORS.labels("redis").inc()
        log("warn", f"catalog cache write skipped: {exc}")
    return items


@app.post("/reserve")
def reserve(body: dict):
    sku = str(body.get("sku", ""))
    qty = int(body.get("qty", 1))
    if get_config("inventory", {}).get("reserve_mode") == "deferred":
        # check stock now, decrement later: the applier that should drain _deferred was never started
        rows = _db("SELECT price_sar FROM products WHERE sku = %s AND stock >= %s", (sku, qty))
        if rows:
            _deferred.append((sku, qty))
            log("info", f"deferred stock decrement for {sku} x{qty} ({len(_deferred)} pending)")
    else:
        rows = _db(
            "UPDATE products SET stock = stock - %s WHERE sku = %s AND stock >= %s RETURNING price_sar",
            (qty, sku, qty),
        )
    if not rows:
        raise HTTPException(409, f"sku {sku} out of stock")
    hold_id = f"hold-{secrets.token_hex(4)}"
    start = time.perf_counter()
    try:
        cache().setex(f"hold:{hold_id}", 300, json.dumps({"sku": sku, "qty": qty}))
    except redis.RedisError as exc:
        DEP_ERRORS.labels("redis").inc()
        log("error", f"failed to store reservation hold {hold_id} in redis: {exc}")
        raise HTTPException(503, "reservation store unavailable") from exc
    finally:
        DEP_LATENCY.labels("redis").observe(time.perf_counter() - start)
    return {"hold_id": hold_id, "unit_price_sar": float(rows[0][0])}
