"""Shared plumbing for every Dukkan service: JSON logs, Prometheus metrics,
hot-reloaded config, Postgres access and the hidden in-process fault hooks
that the chaos panel flips. Nabd never uses the fault hooks to fix anything."""

import json
import os
import random
import sys
import threading
import time

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE", "unknown")

REQUESTS = Counter("http_requests_total", "HTTP requests handled", ["route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.8, 1.0, 1.5, 2.5, 5.0, 10.0),
)
DEP_ERRORS = Counter("dependency_errors_total", "Failed calls to a dependency", ["dependency"])
DEP_LATENCY = Histogram(
    "dependency_duration_seconds", "Latency of calls to a dependency", ["dependency"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
)

_log_lock = threading.Lock()


def log(level: str, msg: str, **fields) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
        "level": level,
        "service": SERVICE,
        "msg": msg,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------- config

_config_cache: dict[str, tuple[int, dict]] = {}


def get_config(name: str, default: dict) -> dict:
    """Read /config/<name>.json, re-reading whenever the file changes."""
    path = f"/config/{name}.json"
    try:
        mtime = os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return default
    cached = _config_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return cached[1] if cached else default
    _config_cache[name] = (mtime, data)
    if cached:
        log("info", f"config reloaded: {name} now at version v{data.get('version')}")
    return data


# ---------------------------------------------------------------- postgres

def pg_connect(host: str = "postgres", port: int = 5432, timeout: int = 2):
    return psycopg.connect(
        host=host, port=port, dbname="dukkan", user="dukkan", password="dukkan",
        connect_timeout=timeout, application_name=SERVICE,
        options="-c statement_timeout=2000",
    )


# ---------------------------------------------------------------- hidden faults

CHAOS = {"cpu_hog": False, "memory_leak": False, "outage": False, "carrier_hang": False}
_leaked: list[bytes] = []


def _cpu_hog_loop() -> None:
    batch = 0
    while CHAOS["cpu_hog"]:
        batch += 1
        start = time.perf_counter()
        # sum() over a range runs in C without releasing the GIL, like a native reindex would,
        # so request handlers really starve instead of just sharing the core politely
        sum(range(2_000_000))
        if batch % 10 == 0:
            docs = random.randint(1800, 2400)
            log("info", f"reindex batch {batch} processed {docs} documents in {int((time.perf_counter() - start) * 1000)}ms")


def _memory_leak_loop() -> None:
    while CHAOS["memory_leak"]:
        _leaked.append(b"\x01" * (3 * 1024 * 1024))
        if len(_leaked) % 5 == 0:
            log("info", f"warming response cache: {len(_leaked) * 120} entries ({len(_leaked) * 3} MB)")
        time.sleep(1)


def create_app() -> FastAPI:
    app = FastAPI(title=f"dukkan-{SERVICE}")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        route = request.url.path
        if route in ("/metrics", "/health") or route.startswith("/__chaos"):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # unhandled errors still become a 500 with a log line
            status = 500
            log("error", f"unhandled {type(exc).__name__} on {route}: {exc}")
            response = JSONResponse({"error": "internal error"}, status_code=500)
        elapsed = time.perf_counter() - start
        REQUESTS.labels(route, str(status)).inc()
        LATENCY.labels(route).observe(elapsed)
        level = "info" if status < 400 else ("warn" if status < 500 else "error")
        log(level, f"{request.method} {route} {status} {int(elapsed * 1000)}ms")
        return response

    # async so they run on the event loop: request handlers live in the thread pool, and a pool
    # stuck on slow upstreams must not make the service look dead to Prometheus
    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.post("/__chaos/{fault}")
    def chaos(fault: str, on: int = 1):
        if fault not in CHAOS:
            return JSONResponse({"error": f"unknown fault {fault}"}, status_code=404)
        enable = bool(on)
        was = CHAOS[fault]
        CHAOS[fault] = enable
        loops = {"cpu_hog": _cpu_hog_loop, "memory_leak": _memory_leak_loop}
        if enable and not was and fault in loops:
            threading.Thread(target=loops[fault], daemon=True).start()
            if fault == "cpu_hog":
                log("info", "scheduled background job started: full product reindex")
        if not enable and fault == "memory_leak":
            _leaked.clear()
        return {"fault": fault, "enabled": enable}

    log("info", f"{SERVICE} started")
    return app
