"""Mizan (ميزان, "balance"): business invariants that catch silent failures.

Classic monitoring asks whether services answer. Mizan asks whether the
business still balances, using records that live outside the services that
could be lying about it:

  money      every payment the PSP captured has an order
  fulfilled  every order is fulfilled or still waiting in the pipeline
  conserved  order events produced = consumed + waiting + in flight + dead-lettered
  progress   while work is waiting, the fulfillment worker keeps moving
  prices     every catalog price matches the supplier's price list

A violation is evidence that no error log, latency graph or health check
will ever show. Mizan also traces a lost item through the logs to the last
service that held it, and repairs the data once the cause is fixed."""

import asyncio
import json
import re
import time

import httpx
import psycopg
import redis

from . import dockerctl
from .bus import bus
from .settings import PG_ADMIN, REDIS_HOST, runtime

PSP_URL = "http://psp:8000"
QUEUE, PROCESSING, DEAD = "q:order-events", "q:order-events:processing", "q:order-events:dead"
PAYMENT_GRACE_S = 20  # an order write follows its charge within milliseconds; 20 s is generous
FULFILL_GRACE_S = 10  # an order's event is published right after its commit
STALL_S = 30
CONFIRM_CHECKS = 2  # a violation must hold on two checks in a row before it alerts

INVARIANTS = {
    "money": {"node": "orders", "title": "Every captured payment has an order for the same amount",
              "title_ar": "كل مبلغ انخصم له طلب بنفس المبلغ"},
    "fulfilled": {"node": "events", "title": "Every order reaches fulfillment, and every shipment has an order",
                  "title_ar": "كل طلب يوصل للشحن، وكل شحنة لها طلب"},
    "conserved": {"node": "events", "title": "Every order event is accounted for",
                  "title_ar": "كل رسالة في الطابور محسوبة"},
    "progress": {"node": "fulfillment", "title": "Fulfillment keeps moving",
                 "title_ar": "الشحن ما يوقف"},
    "prices": {"node": "inventory", "title": "Every price matches the supplier",
               "title_ar": "كل سعر يطابق المورّد"},
}
PRICE_TOLERANCE = 0.005  # half a percent

_redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=3, socket_connect_timeout=2)
_http = httpx.Client(timeout=5.0)
_COMPLETED = re.compile(r"checkout completed (ord-[0-9a-f]+) for (\S+): .*\(payment (pay_[0-9a-f]+)\)")


def _pg():
    # a statement timeout: a lock on a table Mizan reads must make one check fail, not freeze the checker
    return psycopg.connect(**PG_ADMIN, connect_timeout=3, application_name="nabd",
                           options="-c statement_timeout=2500")


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- raw reads

def queue_state() -> dict:
    """One atomic read of the event pipeline."""
    pipe = _redis.pipeline(transaction=True)
    pipe.get("q:produced")
    pipe.get("q:consumed")
    pipe.llen(QUEUE)
    pipe.llen(PROCESSING)
    pipe.llen(DEAD)
    pipe.get("fulfillment:heartbeat")
    pipe.get("fulfillment:current")
    pipe.lindex(QUEUE, -1)
    produced, consumed, pending, processing, dead, beat, current, oldest = pipe.execute()
    now = time.time()
    cur = json.loads(current) if current else None
    old = json.loads(oldest) if oldest else None
    return {
        "produced": _int(produced), "consumed": _int(consumed),
        "pending": pending, "processing": processing, "dead": dead,
        "heartbeat_age_s": round(now - float(beat), 1) if beat else None,
        "current": ({"order_ref": cur.get("order_ref"), "sku": cur.get("sku"), "step": cur.get("step"),
                     "age_s": round(now - float(cur.get("started", now)), 1)} if cur else None),
        "oldest_wait_s": round(now - float(old.get("ts", now)), 1) if old else 0,
    }


def queued_refs() -> set[str]:
    pipe = _redis.pipeline(transaction=True)
    for name in (QUEUE, PROCESSING, DEAD):
        pipe.lrange(name, 0, -1)
    refs = set()
    for items in pipe.execute():
        for raw in items:
            try:
                refs.add(json.loads(raw)["order_ref"])
            except (ValueError, KeyError, TypeError):
                pass
    return refs


def charges(since: float, until: float) -> list[dict]:
    resp = _http.get(f"{PSP_URL}/v1/charges", params={"since": since, "until": until})
    resp.raise_for_status()
    return resp.json()["charges"]


def orphan_charges(since: float, grace_s: float = PAYMENT_GRACE_S) -> tuple[int, list[dict]]:
    """(charges checked, charges with no order)."""
    found = charges(since, time.time() - grace_s)
    if not found:
        return 0, []
    with _pg() as conn:
        have = {r[0] for r in conn.execute("SELECT payment_id FROM orders WHERE payment_id = ANY(%s)",
                                           ([c["id"] for c in found],)).fetchall()}
    return len(found), [c for c in found if c["id"] not in have]


def amount_mismatches(since: float, grace_s: float = PAYMENT_GRACE_S) -> list[dict]:
    """Orders whose recorded total differs from what the payment provider actually charged.

    Existence is not enough: an order that says 1.00 SAR for a 240 SAR charge balances nothing.
    (Added after a blind fault rewrote order totals and every existence check still passed.)"""
    found = {c["id"]: c for c in charges(since, time.time() - grace_s) if c.get("status", "captured") == "captured"}
    if not found:
        return []
    with _pg() as conn:
        rows = conn.execute("SELECT payment_id, order_ref, total_sar FROM orders WHERE payment_id = ANY(%s)",
                            (list(found),)).fetchall()
    out = []
    for pay, ref, total in rows:
        charged, recorded = float(found[pay]["amount_sar"] or 0), float(total or 0)
        if abs(charged - recorded) > 0.01:
            out.append({"payment_id": pay, "order_ref": ref, "charged_sar": charged, "recorded_sar": recorded,
                        "captured_at": found[pay]["captured_at"]})
    return out


def phantom_shipments(since: float) -> list[str]:
    """Shipments booked for an order that does not exist: work nobody paid for.
    (Added after a blind fault slipped a made-up event into the queue.)"""
    with _pg() as conn:
        return [r[0] for r in conn.execute(
            "SELECT f.order_ref FROM fulfillments f LEFT JOIN orders o ON o.order_ref = f.order_ref "
            "WHERE o.order_ref IS NULL AND f.created_at >= to_timestamp(%s) ORDER BY f.created_at LIMIT 50",
            (since,)).fetchall()]


def lost_orders(since: float, grace_s: float = FULFILL_GRACE_S) -> tuple[int, list[dict]]:
    """(orders checked, orders neither fulfilled nor anywhere in the pipeline)."""
    waiting = queued_refs()  # read the pipeline first: an event can only move toward "fulfilled" after this
    with _pg() as conn:
        checked = conn.execute("SELECT count(*) FROM orders WHERE created_at >= to_timestamp(%s) "
                               "AND created_at <= now() - make_interval(secs => %s)", (since, grace_s)).fetchone()[0]
        rows = conn.execute(
            "SELECT o.order_ref, o.payment_id, o.sku, o.qty, extract(epoch FROM o.created_at) FROM orders o "
            "WHERE o.created_at >= to_timestamp(%s) AND o.created_at <= now() - make_interval(secs => %s) "
            "AND o.order_ref IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM fulfillments f WHERE f.order_ref = o.order_ref) "
            "ORDER BY o.created_at", (since, grace_s)).fetchall()
    lost = [{"order_ref": r[0], "payment_id": r[1], "sku": r[2], "qty": r[3], "created_at": float(r[4])}
            for r in rows if r[0] not in waiting]
    return checked, lost


def price_drift(since: float | None = None) -> dict:
    """Catalog prices against the supplier's list, with who last wrote each wrong one and what it cost."""
    feed = {i["sku"]: float(i["price"]) for i in _http.get(f"{PSP_URL}/v1/supplier/catalog").json()["items"]}
    with _pg() as conn:
        rows = conn.execute("SELECT sku, price_sar, price_updated_by, extract(epoch FROM price_updated_at) "
                            "FROM products").fetchall()
        wrong = [{"sku": r[0], "price": float(r[1]), "truth": feed[r[0]], "written_by": r[2],
                  "written_at": float(r[3]) if r[3] else None}
                 for r in rows if r[0] in feed and abs(float(r[1]) - feed[r[0]]) > PRICE_TOLERANCE * feed[r[0]]]
        under, orders = 0.0, 0
        if wrong and since:
            for sku, qty, total in conn.execute(
                    "SELECT sku, qty, total_sar FROM orders WHERE created_at >= to_timestamp(%s) AND sku = ANY(%s)",
                    (since, [w["sku"] for w in wrong])).fetchall():
                gap = feed[sku] * qty - float(total)
                if gap > 0.01:
                    under += gap
                    orders += 1
    worst = max((abs(w["price"] - w["truth"]) / w["truth"] for w in wrong), default=0.0)
    return {"checked": len(rows), "wrong": sorted(wrong, key=lambda w: w["written_at"] or 0),
            "worst_pct": round(worst * 100, 1), "under_sar": round(under, 2), "orders": orders}


def promised(since: float) -> dict[str, dict]:
    """payment id → what the gateway told the customer, from its own log lines."""
    out = {}
    for line in dockerctl.logs("gateway", since - 5):
        m = _COMPLETED.search(line)
        if m:
            out[m.group(3)] = {"order_ref": m.group(1), "user": m.group(2)}
    return out


# ---------------------------------------------------------------- the checker

# The books are reconciled over a sliding horizon. An item is re-checked on every pass while it is young, so a
# loss shows within seconds; re-reading hours of settled charges on every pass would only load the provider's
# ledger (after a long idle stretch it slowed checkouts for everyone).
HORIZON_S = 1800


class Mizan:
    def __init__(self) -> None:
        self.window_start = time.time()
        self.base: dict | None = None
        self.latest: dict | None = None
        self.streak = {k: 0 for k in INVARIANTS}
        self.first_seen: dict[str, float] = {}

    @property
    def since(self) -> float:
        return max(self.window_start, time.time() - HORIZON_S)

    def rebase(self) -> None:
        """Start a fresh accounting window (after a chaos reset, for example)."""
        self.window_start = time.time()
        try:
            q = queue_state()
            self.base = {k: q[k] for k in ("produced", "consumed", "pending", "processing", "dead")}
        except redis.RedisError:
            self.base = None
        self.streak = {k: 0 for k in INVARIANTS}
        self.first_seen = {}

    def check(self) -> dict:
        out: dict = {"at": time.time(), "since": self.since, "invariants": {}}
        inv = out["invariants"]

        try:
            checked, orphans = orphan_charges(self.since)
            wrong = amount_mismatches(self.since)
            amount = round(sum(c["amount_sar"] or 0 for c in orphans), 2)
            gap = round(sum(w["charged_sar"] - w["recorded_sar"] for w in wrong), 2)
            parts = []
            if orphans:
                parts.append(f"{len(orphans)} of {checked} captured payments have no order ({amount:,.0f} SAR)")
            if wrong:
                parts.append(f"{len(wrong)} order{' records' if len(wrong) == 1 else 's record'} a different amount than was charged "
                             f"({gap:,.0f} SAR missing from the books)")
            firsts = [c["captured_at"] for c in orphans[:1]] + [w["captured_at"] for w in wrong[:1]]
            inv["money"] = {"ok": not orphans and not wrong, "checked": checked,
                            "violations": len(orphans) + len(wrong), "amount_sar": amount,
                            "sample": [c["id"] for c in orphans[:5]],
                            "mismatched": len(wrong), "mismatch_sar": gap,
                            "mismatch_sample": [w["order_ref"] for w in wrong[:5]],
                            "first_at": min(firsts) if firsts else None,
                            "detail": "; ".join(parts) or f"{checked} charges reconciled, amounts included"}
        except (httpx.HTTPError, psycopg.Error, KeyError, ValueError) as exc:
            inv["money"] = {"ok": None, "detail": f"cannot reconcile: {type(exc).__name__}"}

        try:
            q = queue_state()
            if self.base is None or q["produced"] < self.base["produced"]:
                # the counters went backwards: Redis lost its data, so start a new window from here
                self.base = {k: q[k] for k in ("produced", "consumed", "pending", "processing", "dead")}
                out["rebased"] = "queue counters reset (Redis restarted)"
            b = self.base
            delta = {k: q[k] - b[k] for k in b}
            lost = delta["produced"] - delta["consumed"] - delta["pending"] - delta["processing"] - delta["dead"]
            carried = b["pending"] + b["processing"] + b["dead"]
            inv["conserved"] = {"ok": lost <= 0, "violations": max(0, lost), **delta,
                                "detail": (f"{carried} carried in + " if carried else "")
                                          + f"{delta['produced']} produced = {delta['consumed']} consumed + "
                                          f"{q['pending']} waiting + {q['processing']} in flight + {q['dead']} dead"
                                          + (f" + {lost} unaccounted" if lost > 0 else "")}
            stuck = q["current"] if q["current"] and q["current"]["age_s"] > STALL_S else None
            stalled = q["pending"] > 0 and ((q["heartbeat_age_s"] or 0) > STALL_S or stuck is not None)
            if stalled:
                detail = f"{q['pending']} waiting; worker silent {q['heartbeat_age_s']} s" + (
                    f", stuck on {stuck['order_ref']} ({stuck['sku']}) for {stuck['age_s']:.0f} s" if stuck else "")
            else:
                detail = f"{q['pending']} waiting, oldest {q['oldest_wait_s']:.0f} s" if q["pending"] else "queue drained"
            inv["progress"] = {"ok": not stalled, "violations": int(stalled), "pending": q["pending"],
                               "heartbeat_age_s": q["heartbeat_age_s"], "oldest_wait_s": q["oldest_wait_s"],
                               "stuck": stuck, "dead": q["dead"], "detail": detail}
        except (redis.RedisError, ValueError) as exc:
            inv["conserved"] = {"ok": None, "detail": f"cannot read the queue: {type(exc).__name__}"}
            inv["progress"] = {"ok": None, "detail": f"cannot read the queue: {type(exc).__name__}"}

        try:
            checked, lost = lost_orders(self.since)
            phantoms = phantom_shipments(self.since)
            parts = []
            if lost:
                parts.append(f"{len(lost)} of {checked} orders will never ship: not fulfilled and not in the queue")
            if phantoms:
                parts.append(f"{len(phantoms)} shipment{' was' if len(phantoms) == 1 else 's were'} booked for "
                             f"{'an order that does' if len(phantoms) == 1 else 'orders that do'} not exist")
            inv["fulfilled"] = {"ok": not lost and not phantoms, "checked": checked,
                                "violations": len(lost) + len(phantoms),
                                "sample": [o["order_ref"] for o in lost[:5]],
                                "phantoms": phantoms[:5], "phantom_count": len(phantoms),
                                "first_at": lost[0]["created_at"] if lost else None,
                                "detail": "; ".join(parts) or f"{checked} orders accounted for, and every shipment has one"}
        except (redis.RedisError, psycopg.Error) as exc:
            inv["fulfilled"] = {"ok": None, "detail": f"cannot check: {type(exc).__name__}"}

        try:
            d = price_drift(self.since)
            w = d["wrong"]
            inv["prices"] = {"ok": not w, "checked": d["checked"], "violations": len(w),
                             "sample": [x["sku"] for x in w[:5]], "worst_pct": d["worst_pct"],
                             "first_at": w[0]["written_at"] if w else None, "under_sar": d["under_sar"],
                             "detail": f"{d['checked']} prices match the supplier" if not w else
                             f"{len(w)} of {d['checked']} prices differ from the supplier (worst {d['worst_pct']}% off); "
                             f"{d['orders']} orders sold {d['under_sar']:,.0f} SAR under price"}
        except (httpx.HTTPError, psycopg.Error, KeyError, ValueError) as exc:
            inv["prices"] = {"ok": None, "detail": f"cannot compare prices: {type(exc).__name__}"}

        for name, meta in INVARIANTS.items():
            v = inv.setdefault(name, {"ok": None, "detail": "not checked"})
            v.update(node=meta["node"], title=meta["title"], title_ar=meta["title_ar"])
            self.streak[name] = self.streak[name] + 1 if v["ok"] is False else 0
            v["confirmed"] = self.streak[name] >= CONFIRM_CHECKS
            if v["confirmed"]:
                self.first_seen.setdefault(name, time.time())
            elif v["ok"]:
                self.first_seen.pop(name, None)
            v["since"] = self.first_seen.get(name)
        out["violated"] = [k for k, v in inv.items() if v.get("confirmed")]
        out["balanced"] = all(v["ok"] for v in inv.values())
        return out

    def alerts(self) -> list[dict]:
        """Confirmed violations, shaped like Prometheus alerts so the agent treats them the same way."""
        if not self.latest or not runtime.business_invariants:
            return []
        out = []
        for name in self.latest["violated"]:
            v = self.latest["invariants"][name]
            out.append({
                "name": "InvariantViolated", "service": v["node"], "dependency": name, "severity": "critical",
                "summary": f"{v['title']}: {v['detail']}",
                "active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v["since"] or time.time())),
                "key": f"InvariantViolated:{v['node']}:{name}", "source": "mizan",
            })
        return out

    def node_health(self) -> dict[str, str]:
        if not self.latest or not runtime.business_invariants:
            return {}
        inv = self.latest["invariants"]
        return {inv[name]["node"]: "crit" for name in self.latest["violated"]}  # a backlog alone is lag, not loss

    async def run(self) -> None:
        await asyncio.to_thread(self.rebase)
        while True:
            started = time.time()
            try:
                self.latest = await asyncio.to_thread(self.check)  # shipped to the UI inside each telemetry snapshot
            except Exception as exc:  # the checker must never take the agent down
                bus.publish("info", level="warn", title=f"mizan error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(1.0, 3.0 - (time.time() - started)))


mizan = Mizan()


# ---------------------------------------------------------------- investigation

def reconcile(grace_s: float = PAYMENT_GRACE_S) -> dict:
    """The detailed books: which orphans the customer was promised, which failed, which orders were lost."""
    _, orphans = orphan_charges(mizan.since, grace_s)
    told = promised(mizan.since) if orphans else {}
    kept, failed = [], []
    for c in orphans:
        (kept if c["id"] in told else failed).append({**c, **told.get(c["id"], {})})
    _, lost = lost_orders(mizan.since, min(grace_s, FULFILL_GRACE_S))
    q = queue_state()
    return {
        "window_start": mizan.since,
        "orphans_promised": kept, "orphans_failed": failed, "lost_orders": lost, "queue": q,
        "promised_sar": round(sum(c["amount_sar"] or 0 for c in kept), 2),
        "failed_sar": round(sum(c["amount_sar"] or 0 for c in failed), 2),
    }


def trace(item: str, window_s: int = 900) -> dict:
    """Follow one payment id or order ref through every hop and every durable record."""
    since = max(mizan.since - 5, time.time() - window_s)
    pay, ref = (item, None) if item.startswith("pay_") else (None, item)
    hops: list[dict] = []
    records: dict = {}
    for ln in dockerctl.logs("gateway", since):
        m = _COMPLETED.search(ln)
        if m and item in ln:
            ref, pay = ref or m.group(1), pay or m.group(3)
    checkout_ref = None  # the reference payments used before any order existed
    if pay:
        try:
            resp = _http.get(f"{PSP_URL}/v1/charges/{pay}")
            charge = resp.json() if resp.status_code == 200 else None
            records["psp_charge"] = charge["status"] if charge else "not found"
            checkout_ref = (charge or {}).get("ref")
        except (httpx.HTTPError, ValueError):
            records["psp_charge"] = "unknown"
    needles = [x for x in (pay, ref, checkout_ref) if x]
    for service in ("payments", "gateway", "orders", "fulfillment"):
        lines = [ln for ln in dockerctl.logs(service, since) if any(n in ln for n in needles)]
        for ln in lines[:3]:
            try:
                rec = json.loads(ln)
                hops.append({"service": service, "ts": rec.get("ts"), "msg": rec.get("msg")})
            except ValueError:
                hops.append({"service": service, "msg": ln[:200]})
    try:
        with _pg() as conn:
            row = conn.execute("SELECT order_ref, payment_id, recovered FROM orders WHERE order_ref = %s OR "
                               "payment_id = %s LIMIT 1", (ref, pay)).fetchone()
            if row:
                ref, pay = row[0], row[1]
            records["order_row"] = bool(row)
            records["fulfilled"] = bool(ref and conn.execute(
                "SELECT 1 FROM fulfillments WHERE order_ref = %s", (ref,)).fetchone())
    except psycopg.Error as exc:
        records["db_error"] = str(exc).splitlines()[0]
    if pay and "psp_charge" not in records:  # found only through the order row
        try:
            resp = _http.get(f"{PSP_URL}/v1/charges/{pay}")
            records["psp_charge"] = resp.json()["status"] if resp.status_code == 200 else "not found"
        except (httpx.HTTPError, ValueError, KeyError):
            records["psp_charge"] = "unknown"
    try:
        records["in_queue"] = bool(ref and ref in queued_refs())
    except redis.RedisError:
        records["in_queue"] = None
    hops.sort(key=lambda h: h.get("ts") or "")
    acked = [h for h in hops if h["service"] in ("orders", "gateway") and h.get("msg", "").startswith(
        ("queued order", "checkout completed"))]
    if records.get("psp_charge") == "captured" and not records.get("order_row"):
        told = any(h["msg"].startswith("checkout completed") for h in acked)
        lost_in = "orders" if told else "checkout (failed after the charge)"
        verdict = (f"charged and told the order {ref} exists, but there is no order row: the order was lost "
                   f"inside orders after it said yes" if told else
                   "charged, but the checkout failed afterwards: the customer saw an error and has no order")
    elif records.get("order_row") and not records.get("fulfilled") and not records.get("in_queue"):
        lost_in = "events"
        verdict = (f"order {ref} is in the database but was never fulfilled and is no longer in any queue: its "
                   f"event was lost between orders and fulfillment")
    else:
        lost_in, verdict = None, "nothing is missing for this item"
    return {"item": item, "payment_id": pay, "order_ref": ref, "hops": hops, "records": records,
            "lost_in": lost_in, "verdict": verdict}


# ---------------------------------------------------------------- repairs

def publish_events(orders: list[dict], count_as_new: bool) -> None:
    pipe = _redis.pipeline(transaction=True)
    for o in orders:
        pipe.lpush(QUEUE, json.dumps({"order_ref": o["order_ref"], "sku": o["sku"], "qty": o["qty"],
                                      "payment_id": o["payment_id"], "ts": time.time(), "replayed": True}))
        if count_as_new:
            pipe.incr("q:produced")
    pipe.execute()


def replay_orphans(grace_s: float = 8) -> dict:
    """Honor what the customer was told: recreate each promised order from the PSP's charge metadata."""
    books = reconcile(grace_s)
    made = []
    with _pg() as conn:
        for c in books["orphans_promised"]:
            meta = c.get("metadata") or {}
            row = conn.execute(
                "INSERT INTO orders (order_ref, user_name, sku, qty, total_sar, payment_id, recovered) "
                "SELECT %s, %s, %s, %s, %s, %s, true WHERE NOT EXISTS (SELECT 1 FROM orders WHERE payment_id = %s) "
                "ON CONFLICT (order_ref) DO NOTHING RETURNING order_ref",
                (c["order_ref"], meta.get("user") or c.get("user"), meta.get("sku"), int(meta.get("qty") or 1),
                 c["amount_sar"], c["id"], c["id"])).fetchone()
            if row:
                made.append({"order_ref": c["order_ref"], "sku": meta.get("sku"), "qty": int(meta.get("qty") or 1),
                             "payment_id": c["id"]})
        conn.commit()
    if made:
        publish_events(made, count_as_new=True)
    return {"recreated": len(made), "amount_sar": books["promised_sar"], "refs": [m["order_ref"] for m in made],
            "left_failed": len(books["orphans_failed"])}


def refund_failed(grace_s: float = 8) -> dict:
    """The customer saw an error, so the charge must not stand: refund it at the PSP."""
    books = reconcile(grace_s)
    done, amount = [], 0.0
    for c in books["orphans_failed"]:
        try:
            _http.post(f"{PSP_URL}/v1/refunds", json={"charge_id": c["id"]}).raise_for_status()
            done.append(c["id"])
            amount += c["amount_sar"] or 0
        except httpx.HTTPError:
            pass
    return {"refunded": len(done), "amount_sar": round(amount, 2), "of": len(books["orphans_failed"])}


def replay_lost(grace_s: float = 5) -> dict:
    """Re-publish the events of orders that fell out of the pipeline. Fulfillment is idempotent per order."""
    _, lost = lost_orders(mizan.since, grace_s)
    if lost:
        publish_events(lost, count_as_new=False)  # they were produced once already; this restores the balance
    return {"replayed": len(lost), "refs": [o["order_ref"] for o in lost[:5]]}


def restore_prices() -> dict:
    """Set every drifted price back to the supplier's. The orders already sold stay a human decision."""
    d = price_drift(mizan.since)
    with _pg() as conn:
        for w in d["wrong"]:
            conn.execute("UPDATE products SET price_sar = %s WHERE sku = %s", (w["truth"], w["sku"]))
        conn.commit()
    return {"restored": len(d["wrong"]), "orders": d["orders"], "under_sar": d["under_sar"]}


def quarantine_current() -> dict:
    """Move the message the worker is stuck on to the dead-letter queue, so the line behind it can move."""
    raw_current = _redis.get("fulfillment:current")
    if not raw_current:
        return {"ok": False, "detail": "the worker is not holding any message"}
    cur = json.loads(raw_current)
    ref = cur.get("order_ref")
    moved = None
    for raw in _redis.lrange(PROCESSING, 0, -1):
        try:
            if json.loads(raw).get("order_ref") == ref:
                moved = raw
                break
        except ValueError:
            continue
    if moved is None:
        return {"ok": False, "detail": f"{ref} is no longer in flight"}
    pipe = _redis.pipeline(transaction=True)
    pipe.lrem(PROCESSING, 1, moved)
    pipe.lpush(DEAD, json.dumps({**json.loads(moved), "quarantined_at": time.time(),
                                 "reason": f"worker stuck {time.time() - float(cur.get('started', time.time())):.0f} s "
                                           f"at step: {cur.get('step') or 'unknown'}"}))
    pipe.delete("fulfillment:current")
    pipe.execute()
    return {"ok": True, "order_ref": ref, "sku": cur.get("sku"), "step": cur.get("step")}
