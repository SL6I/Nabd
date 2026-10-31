"""Fulfillment: a worker that consumes order events and books each order for
shipping. It uses the reliable-queue pattern: an event moves to a processing
list while it is worked on and leaves it only once the order is booked. Pallet
(freight) orders need a quote from the freight carrier first."""

import json
import threading
import time

import httpx
import psycopg
import redis

from common import DEP_ERRORS, create_app, log, pg_connect

app = create_app()

QUEUE, PROCESSING = "q:order-events", "q:order-events:processing"
CARRIER = "http://psp:8000/v1/freight/quote"
WORK_S = 0.08
r = redis.Redis(host="redis", port=6379, socket_timeout=5, socket_connect_timeout=2)
carrier = httpx.Client(timeout=None)  # the carrier "always answers", so nobody set a timeout


def _step(event: dict, step: str, started: float) -> None:
    r.set("fulfillment:current", json.dumps({**event, "step": step, "started": started}))


def _book(conn, event: dict, started: float) -> None:
    if str(event.get("sku", "")).startswith("PALLET-"):
        _step(event, "waiting for the freight carrier's quote", started)
        carrier.post(CARRIER, json={"order_ref": event["order_ref"], "qty": event.get("qty")}).raise_for_status()
        _step(event, "booking shipment", started)
    time.sleep(WORK_S)
    conn.execute("INSERT INTO fulfillments (order_ref, payment_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (event["order_ref"], event.get("payment_id")))
    conn.commit()


def _worker() -> None:
    moved = 0
    while True:  # anything left in processing by a previous run goes back to the queue
        try:
            if r.rpoplpush(PROCESSING, QUEUE) is None:
                break
            moved += 1
        except redis.RedisError:
            time.sleep(1)
    if moved:
        log("info", f"requeued {moved} in-flight order event(s) from a previous run")
    conn = None
    while True:
        try:
            r.set("fulfillment:heartbeat", time.time())
            raw = r.brpoplpush(QUEUE, PROCESSING, timeout=2)
            if raw is None:
                continue
            event = json.loads(raw)
            started = time.time()
            _step(event, "booking shipment", started)
            if conn is None or conn.closed:
                conn = pg_connect()
            _book(conn, event, started)
            pipe = r.pipeline(transaction=True)
            pipe.lrem(PROCESSING, 1, raw)
            pipe.incr("q:consumed")
            pipe.delete("fulfillment:current")
            pipe.execute()
        except redis.RedisError as exc:
            DEP_ERRORS.labels("redis").inc()
            log("error", f"order event queue unavailable: {exc}")
            time.sleep(1)
        except psycopg.Error as exc:
            DEP_ERRORS.labels("postgres").inc()
            log("error", f"could not book fulfillment: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc}")
            conn = None
            time.sleep(1)
        except httpx.HTTPError as exc:
            DEP_ERRORS.labels("carrier").inc()
            log("error", f"freight carrier request failed: {type(exc).__name__}")
            time.sleep(1)


threading.Thread(target=_worker, daemon=True).start()
