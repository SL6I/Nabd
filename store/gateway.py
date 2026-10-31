"""Gateway: the public API. Checkout fans out to auth, inventory, payments
and orders, so any broken dependency shows up here as a symptom."""

import secrets
import time

import httpx
from fastapi import HTTPException

from common import DEP_ERRORS, DEP_LATENCY, create_app, log

app = create_app()
TIMEOUT_S = 2.5
http = httpx.Client(timeout=TIMEOUT_S)
UPSTREAM = {
    "auth": "http://auth:8000",
    "inventory": "http://inventory:8000",
    "orders": "http://orders:8000",
    "payments": "http://payments:8000",
}


def call(dep: str, method: str, path: str, **kwargs) -> dict:
    start = time.perf_counter()
    try:
        resp = http.request(method, UPSTREAM[dep] + path, **kwargs)
    except httpx.TimeoutException as exc:
        DEP_ERRORS.labels(dep).inc()
        log("error", f"upstream {dep} timed out after {TIMEOUT_S}s on {path}")
        raise HTTPException(504, f"{dep} timed out") from exc
    except httpx.HTTPError as exc:
        DEP_ERRORS.labels(dep).inc()
        log("error", f"upstream {dep} unreachable on {path}: {type(exc).__name__}")
        raise HTTPException(502, f"{dep} unreachable") from exc
    finally:
        DEP_LATENCY.labels(dep).observe(time.perf_counter() - start)
    if resp.status_code >= 500:
        DEP_ERRORS.labels(dep).inc()
        log("error", f"upstream {dep} returned {resp.status_code} on {path}: {resp.text[:100]}")
        raise HTTPException(502, f"{dep} failed")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:200])
    return resp.json()


@app.post("/api/login")
def login(body: dict):
    return call("auth", "POST", "/login", json=body)


@app.get("/api/products")
def products():
    return call("inventory", "GET", "/products")


@app.post("/api/checkout")
def checkout(body: dict):
    session = call("auth", "GET", "/verify", params={"token": body.get("token", "")})
    sku, qty = body.get("sku"), int(body.get("qty", 1))
    hold = call("inventory", "POST", "/reserve", json={"sku": sku, "qty": qty})
    ref = f"ref-{secrets.token_hex(5)}"
    total = round(hold["unit_price_sar"] * qty, 2)
    payment = call("payments", "POST", "/charge", json={
        "order_ref": ref, "amount_sar": total, "user": session["user"], "sku": sku, "qty": qty,
    })
    order = call("orders", "POST", "/orders", json={
        "user": session["user"], "sku": sku, "qty": qty,
        "total_sar": total, "payment_id": payment["payment_id"],
    })
    log("info", f"checkout completed {order['order_id']} for {session['user']}: {qty} x {sku} = {total} SAR "
                f"(payment {payment['payment_id']})")
    return {"order_id": order["order_id"], "total_sar": total, "payment_id": payment["payment_id"]}
