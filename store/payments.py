"""Payments: idempotency keys in Redis, then a charge at the payment
provider (PSP). The PSP endpoint and timeout come from hot-reloaded config."""

import time

import httpx
import redis
from fastapi import HTTPException

from common import DEP_ERRORS, DEP_LATENCY, create_app, get_config, log

app = create_app()
cache = redis.Redis(host="redis", port=6379, socket_timeout=1, socket_connect_timeout=1)
http = httpx.Client()
DEFAULT = {"version": 1, "provider_url": "http://psp:8000/v1/charges", "timeout_ms": 2000}


@app.post("/charge")
def charge(body: dict):
    ref = str(body.get("order_ref", ""))
    amount = float(body.get("amount_sar", 0))
    cfg = get_config("payments", DEFAULT)

    try:
        fresh = cache.set(f"idem:{ref}", "1", nx=True, ex=600)
    except redis.RedisError as exc:
        DEP_ERRORS.labels("redis").inc()
        log("error", f"idempotency check failed for payment {ref}: {exc}")
        raise HTTPException(503, "payments temporarily unavailable") from exc
    if not fresh:
        raise HTTPException(409, f"duplicate payment {ref}")

    timeout_ms = int(cfg.get("timeout_ms", 2000))
    start = time.perf_counter()
    try:
        resp = http.post(cfg.get("provider_url", DEFAULT["provider_url"]), timeout=timeout_ms / 1000, json={
            "ref": ref, "amount_sar": amount,
            # like any real PSP, the charge carries what was bought, so a lost order can be rebuilt from it
            "metadata": {"user": body.get("user"), "sku": body.get("sku"), "qty": body.get("qty")},
        })
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        DEP_ERRORS.labels("psp").inc()
        log("error", f"PSP request for {ref} timed out after {timeout_ms}ms (config v{cfg.get('version')})")
        raise HTTPException(502, "payment provider timeout") from exc
    except httpx.HTTPError as exc:
        DEP_ERRORS.labels("psp").inc()
        log("error", f"PSP request for {ref} failed: {type(exc).__name__}: {exc}")
        raise HTTPException(502, "payment provider error") from exc
    finally:
        DEP_LATENCY.labels("psp").observe(time.perf_counter() - start)
    payment_id = resp.json()["id"]
    log("info", f"captured payment {payment_id} for {ref}: {amount} SAR")
    return {"payment_id": payment_id, "amount_sar": amount}
