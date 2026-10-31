"""Catalog sync: a background job that keeps product prices in line with the
supplier's catalog. Normally it applies only the entries the supplier changed
(delta mode). A config flag can route every price through the FX service so
the catalog is ready for other currencies."""

import threading
import time

import httpx
import psycopg

from common import DEP_ERRORS, create_app, get_config, log, pg_connect

app = create_app()

FEED = "http://psp:8000/v1/supplier/catalog"
DEFAULT = {"version": 1, "reprice_mode": "delta", "fx_rate": 3.75, "interval_s": 5}
USD_PEG = 3.75  # SAR per USD
http = httpx.Client(timeout=5.0)


def _delta(conn, feed: list[dict], cursor: float) -> tuple[int, float]:
    changed = [item for item in feed if item["updated_at"] > cursor]
    for item in changed:
        conn.execute("UPDATE products SET price_sar = %s WHERE sku = %s AND price_sar <> %s",
                     (float(item["price"]), item["sku"], float(item["price"])))
    conn.commit()
    if changed:
        log("info", f"applied {len(changed)} supplier price change(s): " + ", ".join(i["sku"] for i in changed))
    return len(changed), max([cursor] + [i["updated_at"] for i in changed])


def _fx_roundtrip(conn, rate: float) -> int:
    """Price every product through USD and back, so a multi-currency catalog stays consistent."""
    rows = conn.execute("SELECT sku, price_sar FROM products ORDER BY sku").fetchall()
    moves = []
    for sku, price in rows:
        usd = round(float(price) / USD_PEG, 2)
        new = round(usd * rate, 2)
        if abs(new - float(price)) >= 0.01:
            conn.execute("UPDATE products SET price_sar = %s WHERE sku = %s", (new, sku))
            moves.append(f"{sku} {float(price):.2f} → {new:.2f}")
    conn.commit()
    log("info", f"repriced {len(rows)} products through FX (rate {rate})"
        + (f": {'; '.join(moves[:3])}{' …' if len(moves) > 3 else ''}" if moves else ": no change"))
    return len(moves)


def _worker() -> None:
    cursor = time.time()  # the catalog was in sync at startup
    while True:
        cfg = {**DEFAULT, **get_config("catalog-sync", DEFAULT)}
        try:
            feed = http.get(FEED).json()["items"]
            with pg_connect() as conn:
                if cfg.get("reprice_mode") == "fx_roundtrip":
                    _fx_roundtrip(conn, float(cfg.get("fx_rate", USD_PEG)))
                else:
                    _, cursor = _delta(conn, feed, cursor)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            DEP_ERRORS.labels("supplier").inc()
            log("error", f"supplier catalog unavailable: {type(exc).__name__}")
        except psycopg.Error as exc:
            DEP_ERRORS.labels("postgres").inc()
            log("error", f"catalog sync write failed: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc}")
        time.sleep(max(1.0, float(cfg.get("interval_s", 5))))


threading.Thread(target=_worker, daemon=True).start()
