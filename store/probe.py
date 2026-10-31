"""Network probe that Nabd runs inside a service container (docker exec),
so it measures the path exactly as that service sees it."""

import json
import socket
import sys
import time

TARGETS = {
    "postgres": [("primary", "toxiproxy", 5433), ("standby", "postgres", 5432)],
    "redis": [("direct", "redis", 6379)],
    "psp": [("direct", "psp", 8000)],
}


def measure(host: str, port: int) -> dict:
    out = {"host": f"{host}:{port}"}
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3):
            out["tcp_connect_ms"] = round((time.perf_counter() - start) * 1000, 1)
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if port in (5432, 5433):
        import psycopg
        start = time.perf_counter()
        try:
            with psycopg.connect(host=host, port=port, dbname="dukkan", user="dukkan",
                                 password="dukkan", connect_timeout=6, application_name="nabd-probe") as conn:
                conn.execute("SELECT 1").fetchone()
            out["db_connect_and_query_ms"] = round((time.perf_counter() - start) * 1000, 1)
        except Exception as exc:  # report, never raise: this runs inside a live service
            out["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    return out


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "postgres"
    results = {name: measure(host, port) for name, host, port in TARGETS.get(target, [])}
    print(json.dumps({"target": target, "paths": results}))
