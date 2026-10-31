"""PSP: a stand-in for an external payment provider (mada-style gateway).

It keeps its own settlement ledger, apart from the store's database, the way a
real provider does: the record of money actually captured, readable through its
settlement API. It also hosts the freight carrier's quote API that the
fulfillment worker calls."""

import json
import random
import secrets
import sqlite3
import threading
import time

from fastapi import HTTPException

from common import CHAOS, create_app, log

app = create_app()

_db = sqlite3.connect("/tmp/psp-ledger.db", check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA synchronous=OFF")  # a simulated provider: speed over crash safety
_db.execute("CREATE TABLE IF NOT EXISTS charges (id TEXT PRIMARY KEY, ref TEXT, amount_sar REAL, metadata TEXT, "
            "status TEXT NOT NULL DEFAULT 'captured', captured_at REAL NOT NULL, refunded_at REAL)")
_db.execute("CREATE INDEX IF NOT EXISTS charges_captured_at ON charges (captured_at)")
_db.commit()
_db_lock = threading.Lock()


@app.post("/v1/charges")
def create_charge(body: dict):
    if CHAOS["outage"]:
        log("error", "acquirer link down: declining charge with 503 (provider-side maintenance)")
        raise HTTPException(503, "provider unavailable")
    time.sleep(random.uniform(0.04, 0.12))
    charge_id = f"pay_{secrets.token_hex(6)}"
    with _db_lock:
        _db.execute("INSERT INTO charges (id, ref, amount_sar, metadata, captured_at) VALUES (?, ?, ?, ?, ?)",
                    (charge_id, body.get("ref"), body.get("amount_sar"),
                     json.dumps(body.get("metadata") or {}), time.time()))
        _db.commit()
    return {"id": charge_id, "status": "captured", "amount_sar": body.get("amount_sar")}


@app.get("/v1/charges")
def list_charges(since: float = 0, until: float | None = None, status: str = "captured"):
    """Settlement report: charges captured in [since, until]."""
    with _db_lock:
        rows = _db.execute("SELECT id, ref, amount_sar, metadata, status, captured_at FROM charges "
                           "WHERE captured_at >= ? AND captured_at <= ? AND status = ? ORDER BY captured_at",
                           (since, until if until is not None else time.time(), status)).fetchall()
    return {"charges": [{"id": r[0], "ref": r[1], "amount_sar": r[2], "metadata": json.loads(r[3] or "{}"),
                         "status": r[4], "captured_at": r[5]} for r in rows]}


@app.get("/v1/charges/{charge_id}")
def get_charge(charge_id: str):
    with _db_lock:
        r = _db.execute("SELECT id, ref, amount_sar, metadata, status, captured_at FROM charges WHERE id = ?",
                        (charge_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no such charge")
    return {"id": r[0], "ref": r[1], "amount_sar": r[2], "metadata": json.loads(r[3] or "{}"), "status": r[4],
            "captured_at": r[5]}


@app.post("/v1/refunds")
def refund(body: dict):
    with _db_lock:
        updated = _db.execute("UPDATE charges SET status = 'refunded', refunded_at = ? "
                              "WHERE id = ? AND status = 'captured'", (time.time(), body.get("charge_id"))).rowcount
        _db.commit()
    if not updated:
        raise HTTPException(404, "no captured charge with that id")
    log("info", f"refunded {body.get('charge_id')}")
    return {"refunded": body.get("charge_id")}


SUPPLIER = {"DATE-SUKKARI-1KG": "65.00", "COFFEE-KHAWLANI": "89.00", "DALLAH-BRASS": "240.00",
            "OUD-BUKHOOR": "180.00", "SHEMAGH-RED": "155.00", "PRAYER-MAT": "75.00", "PALLET-DATES-500KG": "9500.00"}
_SUPPLIER_SINCE = time.time() - 86400


@app.get("/v1/supplier/catalog")
def supplier_catalog():
    """The supplier's price list: the source of truth for what each product costs."""
    return {"currency": "SAR", "items": [{"sku": sku, "price": price, "updated_at": _SUPPLIER_SINCE}
                                         for sku, price in SUPPLIER.items()]}


@app.post("/v1/freight/quote")
def freight_quote(body: dict):
    """The carrier's quote API. When the carrier hangs, it never answers."""
    while CHAOS["carrier_hang"]:
        time.sleep(1)
    time.sleep(random.uniform(0.2, 0.5))
    return {"carrier": "naqel-freight", "quote_sar": 380, "order_ref": body.get("order_ref")}
