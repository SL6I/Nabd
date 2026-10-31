"""Mizan learns: the system's own conservation laws, discovered from its healthy traffic.

Nobody writes these rules. While the store is healthy, Nabd counts every flow it can see
without knowing what the store does: successful requests per service and route (Prometheus),
inserts and updates per table (Postgres), write commands per type (Redis). It keeps every pair
of flows that moves in a fixed ratio, such as "1.00 update on table products for every
successful inventory /reserve". A ratio does not change with load, so a flash sale cannot break
it; a silent failure does, because one side keeps counting while the other quietly stops.

When a law breaks, the items are either waiting somewhere (a list that grows: lag, not loss) or
gone. Lost items are localized the way operators do it: the components on the healthy side of
the broken laws are the suspects, ranked by how many of their laws broke, by a configuration
change shortly before the break (Gandalf, NSDI 2020) and by log lines they never printed while
healthy (Lou et al., ATC 2010).

Method: flow-intensity invariants, Jiang, Chen & Yoshihira, ICAC 2006. Thresholds adapt online
with Adaptive Conformal Inference (Gibbs & Candès, NeurIPS 2021): a benign blip raises the bar
for that law instead of paging someone twice."""

import asyncio
import json
import math
import os
import re
import statistics
import time
from collections import deque

import httpx
import psycopg
import redis

from . import config_store, dockerctl, topology
from .bus import bus
from .settings import DATA_DIR, PG_ADMIN, PROM_URL, REDIS_HOST, runtime

SAMPLE_S = 5
WINDOW_S = 30           # flows are compared over 30 s, long enough for a request to finish its whole path
TRAIN_WINDOWS = 48      # four minutes of healthy windows before any law is trusted
MIN_ITEMS = 6           # fewer items than this in a window is too quiet to judge
MAX_TRAIN_ERR = 0.07    # a law must hold within 7% on 95% of the training windows: structure, not habit
MIN_CORR = 0.6          # the two flows must also rise and fall together, not just both be busy
CONFIRM = 3             # a law must break on three checks in a row (15 s)
MIN_EFFECT = 0.10       # and be off by at least 10%: a law learned very tight must not page anyone over 7%
TARGET_ALPHA = 0.01     # long-run share of false alarms per law (ACI target)
ACI_STEP = 0.005
_MODEL = os.path.join(DATA_DIR, "learned.json")

_http = httpx.Client(timeout=4.0)
_redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=3, socket_connect_timeout=2)
_MASKS = [(re.compile(r"\b(?:ord|ref|pay|hold)[-_][0-9a-z]+\b"), "<ID>"),
          (re.compile(r"(?<=[\s:=(-])[0-9a-f]{6,}\b"), "<HEX>"),
          (re.compile(r"\d+(?:\.\d+)?"), "<N>")]
_ACCESS = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) ")


# ---------------------------------------------------------------- what can be counted

def _route_ok(route: str) -> bool:
    # per-item routes (/v1/charges/pay_ab12…) are lookups, not flows
    return not any(len(seg) > 8 and re.search(r"\d", seg) for seg in route.split("/"))


def _http_flows() -> dict[str, float]:
    resp = _http.get(f"{PROM_URL}/api/v1/query",
                     params={"query": 'sum by (service, route) (http_requests_total{status=~"2.."})'})
    out = {}
    for r in resp.json().get("data", {}).get("result", []):
        svc, route = r["metric"].get("service"), r["metric"].get("route", "")
        if svc and _route_ok(route):
            out[f"http|{svc}|{route}"] = float(r["value"][1])
    return out


def _db_flows() -> dict[str, float]:
    with psycopg.connect(**PG_ADMIN, connect_timeout=3, application_name="nabd",
                         options="-c statement_timeout=2500") as conn:
        rows = conn.execute("SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables").fetchall()
    out = {}
    for table, ins, upd, dele in rows:
        for kind, v in (("insert", ins), ("update", upd), ("delete", dele)):
            if v:
                out[f"db|{table}|{kind}"] = float(v)
    return out


_WRITES: dict[str, bool] = {}


def _is_write(cmd: str) -> bool:
    """Redis says itself which commands write; reads (our own monitoring among them) are not flows."""
    if cmd not in _WRITES:
        try:
            info = _redis.execute_command("COMMAND", "INFO", cmd)
            entry = info.get(cmd) if isinstance(info, dict) else (info[0] if info else None)
            flags = entry.get("flags", []) if isinstance(entry, dict) else (entry[2] if entry else [])
            _WRITES[cmd] = any((f.decode() if isinstance(f, bytes) else str(f)) == "write" for f in flags)
        except (redis.RedisError, IndexError, TypeError):
            _WRITES[cmd] = False
    return _WRITES[cmd]


def _redis_flows() -> dict[str, float]:
    out = {}
    for name, stats in _redis.info("commandstats").items():
        cmd = name.removeprefix("cmdstat_")
        if "|" in cmd or not _is_write(cmd):
            continue
        out[f"redis|{cmd}"] = float(stats["calls"])
    return out


def _buffers() -> dict[str, int]:
    """Every list in Redis is a place where items can wait."""
    out = {}
    for key in _redis.scan_iter(count=500, _type="list"):
        out[key.decode(errors="replace")] = _redis.llen(key)
        if len(out) >= 20:
            break
    return out


def sample() -> tuple[dict[str, float], dict[str, int]]:
    flows: dict[str, float] = {}
    for src in (_http_flows, _db_flows, _redis_flows):
        try:
            flows.update(src())
        except Exception:  # one source down must not blind the others
            pass
    try:
        buffers = _buffers()
    except redis.RedisError:
        buffers = {}
    return flows, buffers


def node_of(flow: str) -> str:
    kind, *rest = flow.split("|")
    return rest[0] if kind == "http" else "postgres" if kind == "db" else "redis"


def label(flow: str) -> str:
    kind, *rest = flow.split("|")
    if not rest:
        return flow
    if kind == "http":
        return f"{rest[0]} {rest[1]}"
    if kind == "db":
        return f"{rest[1]}s on table {rest[0]}"
    return f"Redis {rest[0].upper()}"


def _signature(msg: str) -> str:
    for rx, sub in _MASKS:
        msg = rx.sub(sub, msg)
    return msg[:120]


def log_signatures(service: str, since_ts: float) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in dockerctl.logs(service, since_ts, tail=4000):
        try:
            msg = json.loads(line).get("msg", "")
        except ValueError:
            msg = line
        if msg:
            sig = _signature(str(msg))
            out[sig] = out.get(sig, 0) + 1
    return out


# ---------------------------------------------------------------- the laws

class Law:
    def __init__(self, x: str, y: str, k: float, cal: list[float], alpha: float = TARGET_ALPHA) -> None:
        self.x, self.y, self.k, self.cal = x, y, k, sorted(cal)
        self.alpha = alpha
        self.streak = 0
        self.stall = 0
        self.e = 0.0
        self.status = "holds"
        self.deficit = 0.0
        self.since: float | None = None
        self.onset: float | None = None  # the first miss of the current streak

    def threshold(self) -> float:
        q = self.cal[min(len(self.cal) - 1, int(math.ceil((1 - self.alpha) * len(self.cal))) - 1)]
        return 1.25 * q + 0.04

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "k": round(self.k, 4), "cal": [round(c, 4) for c in self.cal],
                "alpha": round(self.alpha, 5)}

    def sentence(self) -> str:
        return f"{self.k:.2f} {label(self.y)} for every {label(self.x)}"


class Learner:
    def __init__(self) -> None:
        self.state = "warming"
        self.snaps: deque = deque(maxlen=64)
        self.rows: list[dict[str, float]] = []
        self.laws: list[Law] = []
        self.flows: list[str] = []
        self.signatures: dict[str, list[str]] = {}
        self.learned_at: float | None = None
        self.train_started: float | None = None
        self.enabled = True
        self.quiet_until = 0.0  # Nabd's own repairs write extra work on purpose (see hush)
        self.latest: dict = {"state": "warming"}
        self._load()

    # ------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            with open(_MODEL, encoding="utf-8") as fh:
                m = json.load(fh)
            self.laws = [Law(d["x"], d["y"], d["k"], d["cal"], d.get("alpha", TARGET_ALPHA)) for d in m["laws"]]
            self.flows, self.signatures, self.learned_at = m["flows"], m.get("signatures", {}), m["learned_at"]
            self.state = "watching" if self.laws else "warming"
            if self.laws:  # judged only once a full window is in, but known right away
                self.latest = {"state": "watching", "learned_at": self.learned_at, "laws": len(self.laws),
                               "flows": len(self.flows), "judged": 0, "broken": 0, "lagging": 0, "stalled": 0}
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_MODEL, "w", encoding="utf-8") as fh:
            json.dump({"learned_at": self.learned_at, "flows": self.flows, "signatures": self.signatures,
                       "laws": [law.to_dict() for law in self.laws]}, fh)

    def relearn(self) -> None:
        self.state, self.rows, self.laws, self.train_started = "learning", [], [], None
        self.latest = {"state": "learning", "progress": 0.0}

    # ------------------------------------------------ windows
    def _deltas(self) -> tuple[dict[str, float], dict[str, int]] | None:
        """Increments over the last WINDOW_S, robust to counters that reset when a service restarts."""
        if len(self.snaps) < 2:
            return None
        now_ts, now_f, now_b = self.snaps[-1]
        # the newest snapshot that is at least a window old: the deque runs oldest → newest
        old = next((s for s in reversed(self.snaps) if now_ts - s[0] >= WINDOW_S - 1), None)
        if old is None or old is self.snaps[-1]:
            return None
        _, old_f, old_b = old
        flows = {}
        for f, v in now_f.items():
            prev = old_f.get(f)
            if prev is None:
                continue
            flows[f] = v - prev if v >= prev else v  # a reset counter restarted from zero
        buffers = {k: now_b.get(k, 0) - old_b.get(k, 0) for k in set(now_b) | set(old_b)}
        return flows, buffers

    # ------------------------------------------------ learning
    def _fit(self) -> None:
        rows = self.rows
        n = len(rows)
        flows = sorted({f for r in rows for f in r})
        active = [f for f in flows
                  if sum(1 for r in rows if f in r) >= 0.9 * n
                  and statistics.mean(r.get(f, 0) for r in rows) >= 2 * MIN_ITEMS]
        laws = []
        for i, x in enumerate(active):
            for y in active[i + 1:]:
                pts = [(r[x], r[y]) for r in rows if x in r and y in r]
                sxx = sum(a * a for a, _ in pts)
                if not sxx:
                    continue
                k = sum(a * b for a, b in pts) / sxx
                if k <= 0.05:
                    continue
                errs = [abs(b - k * a) / max(1.0, k * a) for a, b in pts]
                if sorted(errs)[int(0.95 * (len(errs) - 1))] > MAX_TRAIN_ERR:
                    continue
                # a poller and the traffic can both look steady; only a real coupling makes them move together
                try:
                    corr = statistics.correlation([a for a, _ in pts], [b for _, b in pts])
                except statistics.StatisticsError:
                    continue
                if corr >= MIN_CORR:
                    laws.append(Law(x, y, k, errs))
        self.laws, self.flows = laws, active
        since = self.train_started or time.time() - TRAIN_WINDOWS * SAMPLE_S
        self.signatures = {s: sorted(log_signatures(s, since)) for s in topology.SERVICES}
        self.learned_at = time.time()
        self.state = "watching"
        self._save()
        bus.publish("info", title=f"Mizan learned {len(laws)} laws from {len(active)} flows of healthy traffic")

    # ------------------------------------------------ watching
    def _evaluate(self, flows: dict[str, float], buffers: dict[str, int]) -> None:
        now = time.time()
        # a list that fills or drains moves items between the two sides of a law: that is lag, not loss.
        # One item can stand for several counted operations on the consumer side, hence the loose factor.
        moved = sum(abs(d) for d in buffers.values())
        waiting = sum(max(0, d) for d in buffers.values())
        for law in self.laws:
            x, y = flows.get(law.x), flows.get(law.y)
            if x is None or y is None:
                law.streak, law.status = 0, "unseen"
                continue
            expected = law.k * x
            if max(expected, y) < MIN_ITEMS:
                law.streak, law.status, law.e = 0, "quiet", 0.0
                continue
            law.e = (y - expected) / max(1.0, expected)
            law.deficit = abs(y - expected)
            exceed = abs(law.e) > max(law.threshold(), MIN_EFFECT)
            # ...but only when the sizes match: a busy queue must not excuse an unrelated, smaller loss
            lag = exceed and 0.25 * law.deficit <= moved <= 4 * law.deficit + 10
            if exceed and not lag:
                law.streak += 1
                if law.streak == 1:
                    law.onset = now
            else:
                law.streak = 0
                law.onset = None
            # waiting is not loss, but a consumer that has stopped while its list grows is a stall
            law.stall = law.stall + 1 if lag and waiting > 0 and min(x, y) < 0.2 * max(expected, x) else 0
            if law.streak < 2:  # ACI: isolated misses teach the threshold; a persistent break must not
                law.alpha = min(0.25, max(1e-4, law.alpha + ACI_STEP * (TARGET_ALPHA - (1.0 if exceed else 0.0))))
            was = law.status
            law.status = ("broken" if law.streak >= CONFIRM else "stalled" if law.stall >= 2 * CONFIRM
                          else "lagging" if lag else "holds")
            if law.status == "broken" and was != "broken":
                law.since = now
            elif law.status != "broken":
                law.since = None
        broken = [law for law in self.laws if law.status == "broken"]
        lagging = [law for law in self.laws if law.status == "lagging"]
        stalled = [law for law in self.laws if law.status == "stalled"]
        judged = [law for law in self.laws if law.status in ("holds", "broken", "lagging", "stalled")]
        self.latest = {
            "state": "watching", "learned_at": self.learned_at, "laws": len(self.laws), "flows": len(self.flows),
            "judged": len(judged), "broken": len(broken), "lagging": len(lagging), "stalled": len(stalled),
            "worst": [self._describe(law) for law in sorted(broken, key=lambda l: -abs(l.e))[:4]],
            "stalls": [self._describe(law) for law in stalled[:2]],
            "first_break": min((law.since for law in broken if law.since), default=None),
            "waiting": waiting, "growing": sorted(k for k, d in buffers.items() if d > 0),
        }
        self.latest["suspects"] = self.localize(read_logs=False)[:3] if broken else []

    def _describe(self, law: Law) -> dict:
        short = law.e < 0
        return {"x": law.x, "y": law.y, "k": round(law.k, 2), "e": round(law.e, 3),
                "law": law.sentence(),
                "now": f"{label(law.y)} is {abs(law.e):.0%} {'short of' if short else 'over'} what "
                       f"{label(law.x)} implies",
                "deficit_side": law.y if short else law.x, "healthy_side": law.x if short else law.y,
                "missing": round(law.deficit)}

    async def run(self, healthy) -> None:
        while True:
            started = time.time()
            if self.enabled:
                try:
                    flows, buffers = await asyncio.to_thread(sample)
                    self.snaps.append((started, flows, buffers))
                    d = self._deltas()
                    if d is not None:
                        if self.state == "warming":
                            self.relearn()
                        if self.state == "learning":
                            if healthy():
                                self.train_started = self.train_started or started - WINDOW_S
                                self.rows.append(d[0])
                            self.latest = {"state": "learning", "progress": round(len(self.rows) / TRAIN_WINDOWS, 2)}
                            if len(self.rows) >= TRAIN_WINDOWS:
                                await asyncio.to_thread(self._fit)
                        elif self.state == "watching":
                            self._evaluate(*d)
                except Exception as exc:  # learning must never take the agent down
                    bus.publish("info", level="warn", title=f"learned invariants: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(1.0, SAMPLE_S - (time.time() - started)))

    # ------------------------------------------------ what the rest of Nabd sees
    def broken(self) -> list[Law]:
        return [law for law in self.laws if law.status == "broken"] if self.on() else []

    def on(self) -> bool:
        return self.enabled and runtime.learned_invariants

    def holding(self) -> set[tuple[str, str]]:
        """The laws that hold right now: what a fix must not break."""
        if not self.on():
            return set()
        return {(law.x, law.y) for law in self.laws if law.status in ("holds", "lagging") and not law.streak}

    def regressions(self, held: set[tuple[str, str]], after: float, nodes: set[str]) -> list[Law]:
        """Laws that held before a change, count the changed component's own requests, and started
        failing after `after`."""
        return [law for law in self.broken() if (law.x, law.y) in held and law.onset is not None
                and law.onset >= after and {node_of(law.x), node_of(law.y)} & nodes]

    def node_health(self) -> dict[str, str]:
        if not self.on() or time.time() < self.quiet_until:
            return {}
        out = {}
        if self.latest.get("broken") and self.latest.get("suspects"):
            out[self.latest["suspects"][0]["service"]] = "crit"
        if self.latest.get("stalled"):
            out[_queue_node()] = "crit"
        return out

    def localize(self, read_logs: bool = True) -> list[dict]:
        """Who sits on the healthy side of the broken laws, ranked with change and log evidence."""
        broken = self.broken()
        if not broken:
            return []
        first = min(law.since for law in broken if law.since) if any(law.since for law in broken) else time.time()
        touch: dict[str, int] = {}
        broken_touch: dict[str, int] = {}
        for law in self.laws:
            for n in {node_of(law.x), node_of(law.y)}:
                touch[n] = touch.get(n, 0) + 1
        candidates: set[str] = set()
        deficits: set[str] = set()
        for law in broken:
            short = law.e < 0
            healthy_side, deficit_side = (law.x, law.y) if short else (law.y, law.x)
            for n in {node_of(law.x), node_of(law.y)}:
                broken_touch[n] = broken_touch.get(n, 0) + 1
            candidates.add(node_of(healthy_side))
            deficits.add(deficit_side)
            if deficit_side.startswith("http|"):  # a service whose own success count fell
                candidates.add(node_of(deficit_side))
        # whoever normally moves with the flow that fell short is a suspect too, even before its own law breaks:
        # the first laws to break are often the busiest pairs, not the ones next to the cause
        for law in self.laws:
            for a, b in ((law.x, law.y), (law.y, law.x)):
                if a in deficits and b.startswith("http|"):
                    candidates.add(node_of(b))
        candidates -= {"postgres", "redis"}  # stores record what they are sent; they are not where items vanish
        ranked = []
        deploys = config_store.history(None, 40)
        for svc in candidates:
            evidence = []
            score = broken_touch.get(svc, 0) / max(1, touch.get(svc, 1))
            evidence.append(f"{broken_touch.get(svc, 0)} of the {touch.get(svc, 0)} laws involving {svc} broke")
            change = [d for d in deploys if d["service"] == svc and d.get("author") != "nabd"
                      and first - 1800 <= d["ts"] <= first + 30]
            if change:
                d = change[-1]
                gap = max(0.0, first - d["ts"])
                score += 1.0 if gap <= 600 else 0.6
                diff = ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in d["diff"].items())
                evidence.append(f"config change {d['id']} on {svc} {gap / 60:.1f} min before the first broken law: {diff}")
            ranked.append({"service": svc, "score": score, "evidence": evidence, "change": change[-1]["id"] if change else None})
        ranked.sort(key=lambda c: (-c["score"], c["change"] is None))  # a tie goes to the changed service
        for c in ranked[:4] if read_logs else []:  # new log lines are the expensive check: top few only
            known = set(self.signatures.get(c["service"], []))
            seen = log_signatures(c["service"], first - 15)
            novel = [(sig, n) for sig, n in seen.items() if sig not in known and n >= 3 and not _ACCESS.match(sig)]
            if novel:
                c["score"] += 0.5
                sig, n = max(novel, key=lambda t: t[1])
                c["evidence"].append(f"{c['service']} logs something it never logged while healthy: {n}× “{sig[:90]}”")
        ranked.sort(key=lambda c: (-c["score"], c["change"] is None))  # a tie goes to the changed service
        return ranked

    def hush(self, seconds: float = 2 * WINDOW_S) -> None:
        """Nabd just rebuilt orders or replayed events: that burst breaks ratios on purpose. Page nobody about it
        until the burst has left the window. (Seen live: a repair of 421 orders opened a second incident.)"""
        self.quiet_until = max(self.quiet_until, time.time() + seconds)

    def alerts(self) -> list[dict]:
        if not self.on() or time.time() < self.quiet_until:
            return []
        out = []
        broken = self.broken()
        if broken:
            top = (self.latest.get("worst") or [{}])[0]
            suspects = self.latest.get("suspects") or []
            node = suspects[0]["service"] if suspects else node_of(top.get("healthy_side", broken[0].x))
            first = self.latest.get("first_break") or time.time()
            out.append({"name": "LearnedLawBroken", "service": node, "dependency": "learned", "severity": "critical",
                        "summary": f"{len(broken)} law{'s' if len(broken) != 1 else ''} Mizan learned from healthy traffic "
                                   f"broke: {top.get('now', '')}",
                        "active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(first)),
                        "key": f"LearnedLawBroken:{node}", "source": "mizan"})
        if self.latest.get("stalled"):
            st = self.latest["stalls"][0]
            out.append({"name": "LearnedFlowStalled", "service": _queue_node(), "dependency": "learned",
                        "severity": "critical",
                        "summary": f"work keeps arriving but {label(st['deficit_side'])} stopped; "
                                   f"{', '.join(self.latest.get('growing') or ['a list'])} keeps growing",
                        "active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "key": "LearnedFlowStalled", "source": "mizan"})
        return out

    def summary(self) -> dict:
        return {**self.latest, "enabled": self.on()}


def _queue_node() -> str:
    """Where waiting work is drawn on the map: a queue node if the topology has one, else the store."""
    return next((n for n, meta in topology.NODES.items() if meta["kind"] == "queue"), "redis")


learner = Learner()
