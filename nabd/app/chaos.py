"""The chaos panel: eight realistic faults, each with a ground truth the
benchmark scores against. Nabd's diagnosis never reads this module's state;
it sees only what a real on-call engineer would see."""

import asyncio
import json
import os
import random
import threading
import time
import uuid

import httpx
import psycopg
import redis

from . import config_store, dockerctl
from .bus import bus
from .mizan import mizan
from .settings import CLAUDE_AVAILABLE, CONFIG_DIR, GATEWAY_URL, PG_ADMIN, PG_APP, REDIS_HOST, TOXIPROXY_URL

FAULTS = {
    "redis_oom": {
        "title": "Cache key leak fills Redis",
        "title_ar": "تسريب مفاتيح يملأ ذاكرة Redis",
        "blurb": "A session-dump job writes large keys with no TTL until Redis hits maxmemory.",
        "truth": {"node": "redis", "category": "cache_memory_exhaustion",
                  "fixes": ["flush_redis_keys", "restart_service:redis"]},
    },
    "payments_bad_config": {
        "title": "Bad config deploy on payments",
        "title_ar": "تحديث إعدادات خاطئ لخدمة الدفع",
        "blurb": "ci-bot ships v2: PSP timeout 2000 ms to 25 ms. Every charge times out.",
        "truth": {"node": "payments", "category": "bad_config_deploy", "fixes": ["rollback_config:payments"]},
    },
    "orders_db_latency": {
        "title": "Degraded network link to the database",
        "title_ar": "بطء في خط الشبكة لقاعدة البيانات",
        "blurb": "The primary link between orders and Postgres adds ~700 ms per round trip.",
        "truth": {"node": "orders-db-link", "category": "network_degradation", "fixes": ["switch_db_route:orders"]},
    },
    "auth_crash": {
        "title": "Auth service crashes",
        "title_ar": "تعطّل خدمة الدخول",
        "blurb": "The auth container is killed. Logins and checkouts fail.",
        "truth": {"node": "auth", "category": "process_crash", "fixes": ["start_service:auth", "restart_service:auth"]},
    },
    "inventory_cpu_hog": {
        "title": "Runaway reindex job pegs inventory CPU",
        "title_ar": "مهمة فهرسة تستهلك معالج المخزون",
        "blurb": "A background reindex thread starves the request handlers.",
        "truth": {"node": "inventory", "category": "cpu_saturation", "fixes": ["restart_service:inventory"]},
    },
    "payments_memory_leak": {
        "title": "Memory leak in payments",
        "title_ar": "تسريب ذاكرة في خدمة الدفع",
        "blurb": "Payments grows ~3 MB/s toward its 320 MB limit. Nabd should act before the OOM kill.",
        "truth": {"node": "payments", "category": "memory_leak",
                  "fixes": ["restart_service:payments", "start_service:payments"]},
    },
    "db_conn_exhaustion": {
        "title": "Rogue batch job exhausts DB connections",
        "title_ar": "مهمة تقارير تستنزف اتصالات قاعدة البيانات",
        "blurb": "nightly-report-job opens every free Postgres slot and sits idle.",
        "truth": {"node": "postgres", "category": "db_connection_exhaustion",
                  "fixes": ["terminate_db_sessions:nightly-report-job"]},
    },
    "db_lock": {
        "title": "Migration holds a lock on products",
        "title_ar": "ترحيل بيانات يقفل جدول المنتجات",
        "blurb": "analytics-migration takes an ACCESS EXCLUSIVE lock and never commits.",
        "truth": {"node": "postgres", "category": "db_lock_contention",
                  "fixes": ["terminate_db_sessions:analytics-migration"]},
    },
    # Held out: Nabd's signature library was written before these existed and has no rule for any of them.
    # They measure what the system does with something it has never seen.
    "auth_session_ttl": {
        "title": "Session lifetime set to zero",
        "title_ar": "مدة الجلسة صارت صفر",
        "blurb": "A security-hardening deploy sets auth token_ttl_s 3600 to 0. Every checkout gets 401.",
        "held_out": True,
        "truth": {"node": "auth", "category": "bad_config_deploy", "fixes": ["rollback_config:auth"]},
    },
    "inventory_cache_host": {
        "title": "Cache host typo in inventory",
        "title_ar": "خطأ في اسم خادم الكاش",
        "blurb": "A deploy points inventory at redis-cache.internal, which does not resolve. Reservations fail.",
        "held_out": True,
        "truth": {"node": "inventory", "category": "bad_config_deploy", "fixes": ["rollback_config:inventory"]},
    },
    "psp_outage": {
        "title": "Payment provider outage",
        "title_ar": "انقطاع مزوّد الدفع",
        "blurb": "The external PSP declines every charge with 503. Nothing inside Dukkan can fix it.",
        "held_out": True, "expect": "escalate",
        "truth": {"node": "psp", "category": "dependency_failure", "fixes": []},
    },
    "redis_crash": {
        "title": "Redis process dies",
        "title_ar": "توقف ريديس",
        "blurb": "The Redis container is killed. Reservations and payment idempotency fail.",
        "held_out": True,
        "truth": {"node": "redis", "category": "process_crash",
                  "fixes": ["start_service:redis", "restart_service:redis"]},
    },
    # Silent: every request still succeeds, no error is logged and no Prometheus alert fires. Only the
    # books show them: the PSP ledger, the orders table, the queue and fulfillment stop agreeing.
    "silent_order_drop": {
        "title": "Paid orders silently vanish",
        "title_ar": "طلبات مدفوعة تختفي بصمت",
        "blurb": "ci-bot ships batched order inserts. Checkout still says success, but each flush keeps only the "
                 "first 8 orders and drops the rest: customers pay for orders that never exist.",
        "silent": True,
        "truth": {"node": "orders", "category": "bad_config_deploy", "fixes": ["rollback_config:orders"],
                  "repairs": ["replay_orphan_payments"]},
    },
    "queue_drop_under_load": {
        "title": "Queue loses orders in a flash sale",
        "title_ar": "الطابور يضيّع طلبات وقت العروض",
        "blurb": "An ops change caps the order-event queue at 50. Harmless on a normal day; then a flash sale "
                 "brings 5x the shoppers and the oldest events are trimmed before fulfillment sees them.",
        "silent": True,
        "truth": {"node": "orders", "category": "bad_config_deploy", "fixes": ["rollback_config:orders"],
                  "repairs": ["replay_lost_events"]},
    },
    "workflow_hang": {
        "title": "Fulfillment hangs on one order",
        "title_ar": "الشحن يعلق على طلب واحد",
        "blurb": "A pallet order needs a freight quote; the carrier's API never answers and the call has no timeout. "
                 "The worker stays up and silent while every order behind it waits.",
        "silent": True,
        "truth": {"node": "fulfillment", "category": "poison_message",
                  "fixes": ["quarantine_stuck_message:fulfillment"], "handoff": True},
    },
    # Not a fault: five times the shoppers for two minutes. The right outcome is that nothing happens.
    "flash_sale": {
        "title": "Flash sale, nothing broken",
        "title_ar": "عرض مفاجئ بدون أي عطل",
        "blurb": "Thirty shoppers instead of six for two minutes. Every law is a ratio, so load alone must not break "
                 "one: Nabd should open no incident and change nothing.",
        "benign": True, "expect": "quiet",
        "truth": {"node": None, "category": "none", "fixes": []},
    },
    # Unseen by Mizan's hand-written books: no rule anywhere checks stock. Only the learned laws can see it.
    "stock_not_decremented": {
        "title": "Stock silently stops going down",
        "title_ar": "المخزون ما عاد ينقص",
        "blurb": "A performance change defers stock decrements off the checkout path, but the job that should apply "
                 "them never runs. Every order succeeds; the store starts selling stock it does not have.",
        "silent": True, "unseen": True,
        "truth": {"node": "inventory", "category": "bad_config_deploy", "fixes": ["rollback_config:inventory"],
                  "handoff": True},
    },
    "catalog_price_drift": {
        "title": "Sync job slowly corrupts prices",
        "title_ar": "مهمة مزامنة تخرّب الأسعار شوي شوي",
        "blurb": "A catalog-sync change prices every product through the FX service, with a rate that is slightly off. "
                 "Each run shaves about 1.3% off every price, and the error compounds.",
        "silent": True,
        "truth": {"node": "catalog-sync", "category": "bad_config_deploy", "fixes": ["rollback_config:catalog-sync"],
                  "repairs": ["restore_catalog_prices"], "handoff": True},
    },
}
SUPPLIER_PRICES = {"DATE-SUKKARI-1KG": 65.00, "COFFEE-KHAWLANI": 89.00, "DALLAH-BRASS": 240.00, "OUD-BUKHOOR": 180.00,
                   "SHEMAGH-RED": 155.00, "PRAYER-MAT": 75.00, "PALLET-DATES-500KG": 9500.00}
SILENT = [k for k, v in FAULTS.items() if v.get("silent")]
BENIGN = [k for k, v in FAULTS.items() if v.get("benign")]
HELD_OUT = [k for k, v in FAULTS.items() if v.get("held_out")]
KNOWN = [k for k in FAULTS if k not in SILENT and k not in HELD_OUT and k not in BENIGN]
FLASH_SALE = {"shoppers": 30, "seconds": 120}
# Blind faults: written by someone who is not on the team that wrote Nabd's rules, in a file the
# authors do not open. They are built from the generic steps below and only their truth is scored.
BLIND_PATH = os.getenv("BLIND_FAULTS", "/blind/faults.json")
BLIND_STEPS = {"sql", "config", "redis", "toxic", "kill", "http", "shoppers", "sleep"}


def load_blind() -> list[str]:
    """(Re)load the blind faults into FAULTS; returns their ids. Titles stay generic on purpose."""
    for k in [k for k, v in FAULTS.items() if v.get("blind")]:
        FAULTS.pop(k)
    try:
        with open(BLIND_PATH, encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, ValueError):
        return []
    ids = []
    for i, f in enumerate(spec.get("faults", []), 1):
        truth = f.get("truth", {})
        fid = f"blind_{i}"
        FAULTS[fid] = {
            "title": f"Blind fault #{i}", "title_ar": f"عطل سري رقم {i}",
            "blurb": "Written by a teammate in a file Nabd's authors never opened.",
            "blind": True, "expect": f.get("expect", "recover"),
            "truth": {"node": truth.get("node"), "category": truth.get("category", "other"),
                      "fixes": truth.get("fixes", [])},
            "_inject": [s for s in f.get("inject", []) if s.get("type") in BLIND_STEPS],
            "_undo": [s for s in f.get("undo", []) if s.get("type") in BLIND_STEPS],
        }
        ids.append(fid)
    return ids

SCENARIOS = {
    "perfect_storm": {"title": "Perfect storm", "title_ar": "العاصفة الكاملة",
                      "faults": ["redis_oom", "payments_bad_config", "orders_db_latency"], "red_herring": True},
    "auth_and_lock": {"title": "Crash + lock", "faults": ["auth_crash", "db_lock"]},
    "leak_and_config": {"title": "Two causes, one service", "faults": ["payments_memory_leak", "payments_bad_config"]},
    "db_double": {"title": "Database pressure + CPU", "faults": ["db_conn_exhaustion", "inventory_cpu_hog"]},
    "cache_and_crash": {"title": "Cache leak + crash", "faults": ["redis_oom", "auth_crash"]},
    "config_and_latency": {"title": "Config + network", "faults": ["payments_bad_config", "orders_db_latency"],
                           "red_herring": True},
    "lock_and_leak": {"title": "Lock + leak", "faults": ["db_lock", "payments_memory_leak"]},
}

_redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=3)
_http = httpx.Client(timeout=5.0)


class Chaos:
    def __init__(self) -> None:
        self.active: dict[str, dict] = {}
        self._held_conns: list = []
        self._lock_conn = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------- public API

    def snapshot(self) -> dict:
        return {k: {"since": v["since"], "hidden": v["hidden"]} for k, v in self.active.items()}

    def truths(self) -> list[dict]:
        return [{"fault": k, **FAULTS[k]["truth"], "expect": FAULTS[k].get("expect", "recover"),
                 "held_out": bool(FAULTS[k].get("held_out")), "silent": bool(FAULTS[k].get("silent")),
                 "blind": bool(FAULTS[k].get("blind")), "injected_at": v["since"]}
                for k, v in self.active.items()]

    async def inject(self, fault: str, hidden: bool = False, quiet: bool = False) -> dict:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault}")
        if fault in self.active:
            return {"fault": fault, "already_active": True}
        self.active[fault] = {"since": time.time(), "hidden": hidden}
        if not quiet:
            bus.publish("chaos", action="inject", fault=None if hidden else fault,
                        title="Mystery fault injected" if hidden else f"Injected: {FAULTS[fault]['title']}",
                        hidden=hidden)
        if FAULTS[fault].get("blind"):
            await asyncio.to_thread(self._run_steps, FAULTS[fault]["_inject"])
        else:
            await asyncio.to_thread(getattr(self, f"_inject_{fault}"))
        return {"fault": fault, "injected": True}

    def _run_steps(self, steps: list[dict]) -> None:
        for st in steps:
            try:
                kind = st["type"]
                if kind == "sql":
                    with psycopg.connect(**PG_ADMIN, connect_timeout=3, autocommit=True,
                                         application_name=st.get("app", "ops-console")) as conn:
                        conn.execute(st["sql"])
                elif kind == "config":
                    config_store.deploy(st["service"], st["changes"], author=st.get("author", "ci-bot"),
                                        message=st.get("message", "config change"),
                                        ts=time.time() - 60 * float(st.get("minutes_ago", 0)))
                elif kind == "redis":
                    _redis.execute_command(*st["command"])
                elif kind == "toxic":
                    _http.post(f"{TOXIPROXY_URL}/proxies/{st['proxy']}/toxics", json=st["toxic"])
                elif kind == "kill":
                    dockerctl.kill(st["service"])
                elif kind == "http":
                    _http.request(st.get("method", "POST"), st["url"], json=st.get("json"), params=st.get("params"))
                elif kind == "shoppers":
                    self._set_shoppers(int(st["count"]))
                elif kind == "sleep":
                    time.sleep(min(30.0, float(st["seconds"])))
            except Exception as exc:  # a broken step is reported, never fatal; its content is not echoed
                bus.publish("info", level="warn", title=f"a blind step failed: {type(exc).__name__}")

    async def scenario(self, name: str) -> dict:
        spec = SCENARIOS[name]
        bus.publish("chaos", action="scenario", scenario=name, faults=spec["faults"],
                    title=f"Scenario: {spec['title']} ({len(spec['faults'])} faults at once)")
        if spec.get("red_herring"):
            await asyncio.to_thread(self._red_herring_deploy)
        await asyncio.gather(*(self.inject(fault, quiet=True) for fault in spec["faults"]))
        return {"scenario": name, "faults": spec["faults"]}

    async def mystery(self) -> dict:
        count = random.choice([1, 1, 2, 2, 3])
        # held-out faults need the Claude path; without it the mystery stays within known faults
        pool = KNOWN + HELD_OUT if CLAUDE_AVAILABLE else KNOWN
        picks = random.sample(pool, count)
        bus.publish("chaos", action="mystery", title=f"Mystery: {count} hidden fault{'s' if count > 1 else ''} injected",
                    hidden=True)
        await asyncio.gather(*(self.inject(fault, hidden=True, quiet=True) for fault in picks))
        return {"mystery": True, "count": count}

    async def reset(self, announce: bool = True) -> None:
        await asyncio.to_thread(self._reset_all)
        self.active.clear()
        await asyncio.to_thread(mizan.rebase)  # a fresh accounting window: old damage is not this test's
        if announce:
            bus.publish("chaos", action="reset", title="Chaos reset: every fault cleared, configs back to baseline")

    def forget_resolved(self, faults: list[str]) -> None:
        """Called after Nabd verifies recovery so the panel stops showing them as live."""
        for fault in faults:
            self.active.pop(fault, None)

    # ---------------------------------------------------------- faults

    def _inject_redis_oom(self) -> None:
        # Fill to the limit, then land one large dump the moment any headroom appears. Redis only
        # checks maxmemory before a command, so that write overshoots and every later write fails
        # (expiring session keys alone would otherwise keep freeing just enough room).
        for size in (512 * 1024, 32 * 1024):
            payload = os.urandom(size)
            for _ in range(5000):
                try:
                    _redis.set(f"tmp:session-dump:{uuid.uuid4().hex[:12]}", payload)
                except redis.ResponseError:
                    break
        big = os.urandom(6 * 1024 * 1024)
        for _ in range(100):
            try:
                _redis.set(f"tmp:session-dump:{uuid.uuid4().hex[:12]}", big)
                break
            except redis.ResponseError:
                time.sleep(0.2)

    def _inject_payments_bad_config(self) -> None:
        config_store.deploy("payments", {"timeout_ms": 25}, author="ci-bot",
                            message="perf: tighten PSP timeout for faster failover (PR #482)")

    def _red_herring_deploy(self) -> None:
        config_store.deploy("inventory", {"cache_ttl_s": 45}, author="hessa",
                            message="feat: keep the product catalog cached a little longer",
                            ts=time.time() - random.randint(240, 420))

    def _inject_orders_db_latency(self) -> None:
        _http.post(f"{TOXIPROXY_URL}/proxies/orders_db_primary/toxics", json={
            "name": "wan_latency", "type": "latency", "stream": "downstream", "toxicity": 1.0,
            "attributes": {"latency": 700, "jitter": 120},
        })

    def _inject_auth_crash(self) -> None:
        dockerctl.kill("auth")

    def _inject_inventory_cpu_hog(self) -> None:
        _http.post("http://inventory:8000/__chaos/cpu_hog", params={"on": 1})

    def _inject_payments_memory_leak(self) -> None:
        _http.post("http://payments:8000/__chaos/memory_leak", params={"on": 1})

    def _inject_db_conn_exhaustion(self) -> None:
        with psycopg.connect(**PG_ADMIN, connect_timeout=3) as conn:
            max_conn = int(conn.execute("SHOW max_connections").fetchone()[0])
            reserved = int(conn.execute("SHOW superuser_reserved_connections").fetchone()[0])
            used = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'"
                                ).fetchone()[0]
        for _ in range(max(0, max_conn - reserved - used)):
            try:
                self._held_conns.append(psycopg.connect(**PG_APP, connect_timeout=3,
                                                        application_name="nightly-report-job"))
            except psycopg.Error:
                break

    def _inject_auth_session_ttl(self) -> None:
        config_store.deploy("auth", {"token_ttl_s": 0}, author="sec-bot",
                            message="security: enforce short-lived sessions per audit finding SEC-219")

    def _inject_inventory_cache_host(self) -> None:
        config_store.deploy("inventory", {"cache_host": "redis-cache.internal"}, author="ci-bot",
                            message="infra: move inventory to the dedicated cache cluster")

    def _inject_psp_outage(self) -> None:
        _http.post("http://psp:8000/__chaos/outage", params={"on": 1})

    def _inject_redis_crash(self) -> None:
        dockerctl.kill("redis")

    def _inject_silent_order_drop(self) -> None:
        config_store.deploy("orders", {"write_mode": "batched"}, author="ci-bot",
                            message="perf: batch order inserts to cut DB round-trips (PR #511)")

    def _inject_queue_drop_under_load(self) -> None:
        config_store.deploy("orders", {"event_queue_max": 50}, author="ops-bot",
                            message="ops: cap the order event queue to protect Redis memory",
                            ts=time.time() - 8 * 60)  # shipped earlier and harmless, until the sale
        self._set_shoppers(FLASH_SALE["shoppers"])
        bus.publish("info", title=f"Flash sale: {FLASH_SALE['shoppers']} shoppers for {FLASH_SALE['seconds']} s")
        threading.Timer(FLASH_SALE["seconds"], self._set_shoppers, args=(6,)).start()

    def _inject_workflow_hang(self) -> None:
        _http.post("http://psp:8000/__chaos/carrier_hang", params={"on": 1})
        # a B2B customer orders a pallet, the first freight order of the day
        token = _http.post(f"{GATEWAY_URL}/api/login", json={"user": "othaim-b2b"}).json().get("token", "")
        _http.post(f"{GATEWAY_URL}/api/checkout", json={"token": token, "sku": "PALLET-DATES-500KG", "qty": 1})

    def _inject_flash_sale(self) -> None:
        self._set_shoppers(FLASH_SALE["shoppers"])
        bus.publish("info", title=f"Flash sale: {FLASH_SALE['shoppers']} shoppers for {FLASH_SALE['seconds']} s")
        threading.Timer(FLASH_SALE["seconds"], self._set_shoppers, args=(6,)).start()

    def _inject_stock_not_decremented(self) -> None:
        config_store.deploy("inventory", {"reserve_mode": "deferred"}, author="ci-bot",
                            message="perf: take stock decrements off the checkout path (PR #540)")

    def _inject_catalog_price_drift(self) -> None:
        config_store.deploy("catalog-sync", {"reprice_mode": "fx_roundtrip", "fx_rate": 3.70}, author="ci-bot",
                            message="feat: price the catalog through the FX service for multi-currency (PR #530)")

    @staticmethod
    def _restore_prices() -> None:
        with psycopg.connect(**PG_ADMIN, connect_timeout=3, autocommit=True, application_name="chaos-reset") as conn:
            for sku, price in SUPPLIER_PRICES.items():
                conn.execute("UPDATE products SET price_sar = %s WHERE sku = %s AND price_sar <> %s", (price, sku, price))

    @staticmethod
    def _set_shoppers(count: int) -> None:
        path = os.path.join(CONFIG_DIR, "traffic.json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"shoppers": count}, fh)
        os.replace(path + ".tmp", path)

    def _inject_db_lock(self) -> None:
        conn = psycopg.connect(**PG_APP, connect_timeout=3, application_name="analytics-migration")
        conn.execute("SET statement_timeout = 0")
        conn.execute("LOCK TABLE products IN ACCESS EXCLUSIVE MODE")
        self._lock_conn = conn  # transaction left open on purpose

    # ---------------------------------------------------------- reset

    def _reset_all(self) -> None:
        for fault in [k for k, v in FAULTS.items() if v.get("blind")]:
            self._run_steps(FAULTS[fault]["_undo"])
        steps = [
            lambda: _http.delete(f"{TOXIPROXY_URL}/proxies/orders_db_primary/toxics/wan_latency"),
            lambda: _http.post("http://inventory:8000/__chaos/cpu_hog", params={"on": 0}),
            lambda: _http.post("http://payments:8000/__chaos/memory_leak", params={"on": 0}),
            lambda: _http.post("http://psp:8000/__chaos/outage", params={"on": 0}),
            lambda: _http.post("http://psp:8000/__chaos/carrier_hang", params={"on": 0}),
            lambda: self._set_shoppers(6),
            lambda: dockerctl.start("redis") if not dockerctl.inspect("redis").get("running") else None,
            self._release_db,
            self._flush_leak_keys,
            config_store.reset_baseline,
            self._restore_prices,
            lambda: dockerctl.start("auth") if not dockerctl.inspect("auth").get("running") else None,
            lambda: dockerctl.start("payments") if not dockerctl.inspect("payments").get("running") else None,
        ]
        for step in steps:
            try:
                step()
            except Exception:  # reset is best effort; each step is independent
                pass
        if "payments_memory_leak" in self.active or "inventory_cpu_hog" in self.active:
            for svc in ("payments", "inventory"):
                try:
                    dockerctl.restart(svc)
                except Exception:
                    pass

    def _release_db(self) -> None:
        for conn in self._held_conns:
            try:
                conn.close()
            except Exception:
                pass
        self._held_conns.clear()
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            except Exception:
                pass
            self._lock_conn = None
        with psycopg.connect(**PG_ADMIN, connect_timeout=3, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE application_name IN ('nightly-report-job', 'analytics-migration')")

    def _flush_leak_keys(self) -> None:
        batch = []
        for key in _redis.scan_iter(match="tmp:session-dump:*", count=500):
            batch.append(key)
            if len(batch) >= 200:
                _redis.unlink(*batch)
                batch.clear()
        if batch:
            _redis.unlink(*batch)


chaos = Chaos()
load_blind()
