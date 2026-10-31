"""Blind faults without a teammate: a different model writes them, and nobody who wrote Nabd reads them.

A second model (DeepSeek, not the model that wrote Nabd) gets only what a new teammate would get:
the public README, this folder's guide, the database schema and a sheet of the store's public facts.
It writes faults in the step format of faults.example.json. Each fault then has to pass a safety
check, so a fault can break the store but never destroy it, reach outside it, or touch Nabd itself.

The faults are written to faults.json. This script never prints their content: only how many were
asked for, kept and rejected, and the file's SHA-256, so the file can be shown unchanged afterwards.

    python eval/blind/generate_blind.py            # reads NABD_LLM_* from .env
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(HERE, "faults.json")

STEP_TYPES = {"sql", "config", "redis", "toxic", "kill", "http", "shoppers", "sleep"}
CATEGORIES = {"bad_config_deploy", "process_crash", "db_lock_contention", "db_connection_exhaustion",
              "network_degradation", "cache_memory_exhaustion", "cpu_saturation", "memory_leak",
              "dependency_failure", "silent_data_loss", "corrupted_data", "poison_message", "other"}
KILLABLE = {"gateway", "auth", "inventory", "orders", "payments", "psp", "fulfillment", "catalog-sync", "redis"}
HTTP_HOSTS = {"gateway", "auth", "inventory", "orders", "payments", "psp", "toxiproxy"}
CONFIG_SERVICES = {"payments", "orders", "inventory", "auth", "catalog-sync"}

BAD_SQL = [
    r"\bDROP\s+(TABLE|SCHEMA|DATABASE|INDEX|EXTENSION|ROLE|USER|OWNED|VIEW|SEQUENCE)\b",
    r"\bTRUNCATE\b",
    r"\b(ALTER|CREATE)\s+(ROLE|USER|SYSTEM|DATABASE|EXTENSION)\b",
    r"\bGRANT\b", r"\bREVOKE\b",
    r"\bCOPY\b[^;]*\bPROGRAM\b",
    r"\bDROP\s+COLUMN\b",
    r"\bpg_(terminate|cancel)_backend\b", r"\blo_(import|export)\b", r"\bdblink\b",
    r"\bpg_read_(server_)?file\b", r"\bpg_ls_dir\b",
]
BAD_REDIS = {"FLUSHALL", "FLUSHDB", "SHUTDOWN", "DEBUG", "REPLICAOF", "SLAVEOF", "MODULE", "EVAL", "EVALSHA",
             "SCRIPT", "FUNCTION", "ACL", "MIGRATE", "SAVE", "BGSAVE", "BGREWRITEAOF", "FAILOVER"}

FACTS = """\
# Public facts about the Dukkan store (what a new teammate is told on day one)

Services (HTTP on port 8000 inside the Docker network; hostnames as written):
- gateway: the public API (/api/login, /api/products, /api/checkout); calls auth, inventory, payments, orders
- auth: /login, /verify (sessions in Redis)
- inventory: /products, /reserve (stock in Postgres, product cache in Redis)
- payments: /charge, calls the external payment provider (psp)
- psp: a stub of the external payment provider (/v1/charges, /v1/refunds), and also the freight carrier
  (/v1/freight/quote) and the supplier price list (/v1/supplier/catalog)
- orders: /orders, writes the order to Postgres, then publishes an event for fulfillment
- fulfillment: a worker that takes order events from Redis and books shipping (calls the carrier for pallet goods)
- catalog-sync: a worker that copies supplier prices into the products table
- traffic: simulated shoppers
Infrastructure: postgres (port 5432), redis (6379), toxiproxy (API on toxiproxy:8474) and prometheus.
orders reaches Postgres through the Toxiproxy proxy "orders_db_primary" (its "primary" route); the
"standby" route goes straight to Postgres.

Config (deployed with a `config` step; services read it live):
- orders: db_route "primary" | "standby"; write_mode "sync" | "batched"; batch_size (int, default 8);
  event_queue_max (int; 0 = unbounded, n > 0 caps the event list at n items)
- inventory: cache_ttl_s (int, 30); cache_host (hostname, "redis"); reserve_mode "sync" | "deferred"
- payments: provider_url ("http://psp:8000/v1/charges"); timeout_ms (int, 2000)
- auth: token_ttl_s (int, 3600)
- catalog-sync: reprice_mode "delta" | "fx_roundtrip"; fx_rate (float, 3.75); interval_s (int, 5)

Built-in fault hooks: POST http://<service>:8000/__chaos/<name>?on=1 (on=0 turns it off), where <name> is
cpu_hog, memory_leak, outage (only meaningful on psp) or carrier_hang (only on psp).

Redis keys: q:order-events (list, orders -> fulfillment), q:order-events:processing (the item being worked),
q:produced and q:consumed (counters), fulfillment:heartbeat, fulfillment:current.

Topology nodes a fault can be blamed on (truth.node): traffic, gateway, auth, inventory, orders, payments,
psp, fulfillment, catalog-sync, carrier, supplier, events, redis, postgres, orders-db-link.

Actions Nabd may take (truth.fixes, written "action:target"):
restart_service:<service>, start_service:<service>, flush_redis_keys:<prefix>, rollback_config:<service>,
switch_db_route:orders, terminate_db_sessions:<application_name>, replay_orphan_payments:orders,
refund_failed_payments:payments, replay_lost_events:orders, quarantine_stuck_message:fulfillment,
restore_catalog_prices:inventory
"""

SYSTEM = """You are a chaos engineer red-teaming an autonomous SRE agent called Nabd, which watches an online
store called Dukkan. You did not build Nabd and you want to find where it fails. You write blind faults in
a JSON step format. Nobody on Nabd's team will read them before they run. Answer with JSON only."""

ASK = """Write {n} new blind faults for the Dukkan store, as one JSON object {{"faults": [...]}} in exactly the
format of faults.example.json.

Mix:
- at least 3 silent faults: every request still succeeds, but data is lost, corrupted, duplicated or stuck;
- at least 1 loud fault that breaks requests in a way the README's catalog does not list;
- at least 1 whose right answer is to call a human ("expect": "escalate") because nothing inside the store fixes it;
- at least 1 that looks like one component but is really another.
Do not repeat a fault the README already lists; change the mechanism, not just the numbers.

Rules:
- Use only the step types in the guide. Each fault must take effect within 30 s and stay active until undone.
- "undo" must remove the fault completely and be safe to run twice (DROP ... IF EXISTS, on=0, deleting the
  toxic, the old config values, and so on).
- Never destroy data or the schema: no DROP TABLE, TRUNCATE, DELETE or UPDATE without WHERE on orders or
  fulfillments, no roles or grants. Never call Nabd (port 7000) or anything outside the store. Never kill
  postgres, nabd, prometheus, toxiproxy or traffic.
- "truth" must use the nodes, categories and "action:target" fixes listed in the facts; leave "fixes"
  empty when nothing in the action list fixes it.
- Add a short "note" saying what the fault does.

--- eval/blind/README.md ---
{guide}

--- eval/blind/faults.example.json ---
{example}

--- facts ---
{facts}

--- infra/postgres/init.sql (the schema) ---
{schema}

--- README.md (the project) ---
{readme}
"""


def env() -> dict:
    out = {}
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as fh:
        for line in fh:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v.strip().strip('"').strip("'")
    return out


def read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def ask_model(n: int) -> list[dict]:
    e = env()
    key, base, model = e.get("NABD_LLM_API_KEY"), e.get("NABD_LLM_BASE_URL"), e.get("NABD_MODEL")
    if not key or not base:
        sys.exit("NABD_LLM_API_KEY and NABD_LLM_BASE_URL must be set in .env")
    prompt = ASK.format(n=n, guide=read("eval", "blind", "README.md"), example=read("eval", "blind", "faults.example.json"),
                        facts=FACTS, schema=read("infra", "postgres", "init.sql"), readme=read("README.md"))
    req = {"model": model, "max_tokens": 32000, "system": SYSTEM, "messages": [{"role": "user", "content": prompt}],
           "thinking": {"type": "disabled"}}  # long private reasoning used the whole budget before any answer
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    resp = httpx.post(base.rstrip("/") + "/v1/messages", timeout=900, headers=headers, json=req)
    if resp.status_code == 400:  # an endpoint that does not accept the thinking switch
        req.pop("thinking")
        resp = httpx.post(base.rstrip("/") + "/v1/messages", timeout=900, headers=headers, json=req)
    if resp.status_code != 200:
        sys.exit(f"model call failed: HTTP {resp.status_code}")  # the body may quote the prompt; not printed
    body = resp.json()
    blocks = body.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    # shape only, never content: enough to tell a cut-off answer from a malformed one
    print(f"  model answer: stop={body.get('stop_reason')} blocks={[b.get('type') for b in blocks]} "
          f"text={len(text)} chars, output tokens={body.get('usage', {}).get('output_tokens')}")
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print("  no JSON object in the answer")
        return []
    try:
        return json.loads(m.group(0)).get("faults", [])
    except ValueError as exc:
        print(f"  JSON did not parse ({type(exc).__name__} at char {getattr(exc, 'pos', '?')} of {len(m.group(0))})")
        return []


def check(f: dict) -> str | None:
    """Why a fault is unsafe or malformed, as a category only (the content is never shown)."""
    if not isinstance(f, dict) or not isinstance(f.get("inject"), list) or not f["inject"]:
        return "format"
    truth = f.get("truth") or {}
    if truth.get("category") not in CATEGORIES or not truth.get("node") or f.get("expect", "recover") not in (
            "recover", "escalate"):
        return "truth"
    for fix in truth.get("fixes", []):
        if not isinstance(fix, str) or ":" not in fix:
            return "truth"
    needs_undo = False
    for st in f["inject"] + list(f.get("undo") or []):
        kind = st.get("type") if isinstance(st, dict) else None
        if kind not in STEP_TYPES:
            return "format"
        if kind == "sql":
            sql = str(st.get("sql", ""))
            if any(re.search(p, sql, re.I) for p in BAD_SQL):
                return "safety"
            for stmt in re.split(r";", sql):
                if re.search(r"\b(DELETE\s+FROM|UPDATE)\s+(public\.)?(orders|fulfillments)\b", stmt, re.I) \
                        and not re.search(r"\bWHERE\b", stmt, re.I):
                    return "safety"
                if re.search(r"\bDELETE\s+FROM\b", stmt, re.I) and not re.search(r"\bWHERE\b", stmt, re.I):
                    return "safety"
            needs_undo = True
        elif kind == "redis":
            cmd = st.get("command")
            if not isinstance(cmd, list) or not cmd or str(cmd[0]).upper() in BAD_REDIS:
                return "safety"
            if str(cmd[0]).upper() == "CONFIG" and any(str(c).lower() in ("dir", "dbfilename") for c in cmd):
                return "safety"
            needs_undo = True
        elif kind == "kill":
            if st.get("service") not in KILLABLE:
                return "safety"
        elif kind == "http":
            host = re.match(r"https?://([^/:]+)", str(st.get("url", "")))
            if not host or host.group(1) not in HTTP_HOSTS:
                return "safety"
            needs_undo = True
        elif kind == "toxic":
            if st.get("proxy") != "orders_db_primary" or not isinstance(st.get("toxic"), dict):
                return "format"
            needs_undo = True
        elif kind == "config":
            if st.get("service") not in CONFIG_SERVICES or not isinstance(st.get("changes"), dict):
                return "format"
        elif kind == "shoppers":
            if not 0 <= int(st.get("count", -1)) <= 60:
                return "safety"
    if needs_undo and not f.get("undo"):
        return "undo"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="replace an existing faults.json")
    args = ap.parse_args()
    if os.path.exists(OUT) and not args.force:
        sys.exit("faults.json already exists; pass --force to replace it")

    kept, rejected, asked = [], {}, 0
    for _ in range(2):  # a second round tops up what the safety check removed
        want = args.n - len(kept)
        if want <= 0:
            break
        asked += want
        for f in ask_model(want):
            why = check(f)
            if why:
                rejected[why] = rejected.get(why, 0) + 1
            elif len(kept) < args.n:
                kept.append(f)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"faults": kept}, fh, ensure_ascii=False, indent=1)
    digest = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    e = env()
    with open(os.path.join(HERE, "provenance.json"), "w", encoding="utf-8") as fh:
        json.dump({"written_by": e.get("NABD_MODEL"), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "asked": asked, "kept": len(kept), "rejected": rejected, "sha256": digest,
                   "read_by_nabd_authors": False}, fh, indent=1)
    print(f"asked for {asked} faults, kept {len(kept)}, rejected {sum(rejected.values())} {rejected or ''}")
    print(f"faults.json sha256 {digest}")


if __name__ == "__main__":
    main()
