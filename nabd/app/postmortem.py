"""Blameless postmortems in English and Arabic, written the moment an
incident closes and saved next to the incident memory."""

import json
import os
import time

from . import i18n, telemetry
from .claude_engine import engine as claude
from .settings import DATA_DIR, MODEL

_DIR = os.path.join(DATA_DIR, "postmortems")


def _fmt_s(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts)) + " UTC"


async def _impact(start: float, end: float) -> dict:
    window = max(15, int(end - start) + 10)
    failed = await telemetry.prom_query(
        f'sum(increase(http_requests_total{{service="gateway",route="/api/checkout",status=~"5.."}}[{window}s]))')
    total = await telemetry.prom_query(
        f'sum(increase(http_requests_total{{service="gateway",route="/api/checkout"}}[{window}s]))')
    f = round(failed[0]["value"]) if failed and failed[0].get("value") is not None else None
    t = round(total[0]["value"]) if total and total[0].get("value") is not None else None
    return {"failed_checkouts": f, "total_checkouts": t, "window_s": window}


def _md_en(pm: dict) -> str:
    lines = [f"# {pm['id']}: {pm['title']}", "",
             f"**Status:** {pm['status']} · **Severity:** {pm['severity']} · "
             f"**Time to detect:** {_fmt_s(pm['durations']['detect_s'])} · "
             f"**Time to recover:** {_fmt_s(pm['durations']['mttr_s'])}", ""]
    if pm.get("narrative", {}).get("en"):
        lines += ["## Summary", pm["narrative"]["en"], ""]
    imp = pm["impact"]
    if imp.get("total_checkouts"):
        lines += ["## Impact", f"{imp['failed_checkouts']} of {imp['total_checkouts']} checkouts failed while the "
                               f"incident was open." + (" Every checkout looked successful: the damage was silent "
                                                        "and only the books showed it." if pm.get("silent") else ""), ""]
    lines.append("## Root causes")
    for rc in pm["root_causes"]:
        lines += [f"### {rc['title']}", f"*{rc['node']} · {rc['category']} · confidence {rc['confidence']:.0%}*", "",
                  rc["explanation"], ""]
        lines += [f"- {e}" for e in rc["evidence"]]
        if rc.get("skeptic"):
            s = rc["skeptic"]
            lines += ["", f"> Skeptic: {s['challenge']} **{s['verdict']}**. {s['reasoning']}"]
        lines.append("")
    if pm["ruled_out"]:
        lines.append("## Ruled out")
        lines += [f"- {r['item']}: {r['reason']}" for r in pm["ruled_out"]]
        lines.append("")
    if pm.get("repairs"):
        lines.append("## Repairs to the books (Mizan)")
        lines += [f"- {r['description']}: {r['result']}" for r in pm["repairs"]]
        lines.append("")
    if pm.get("handoffs"):
        lines.append("## Handed to humans")
        lines += [f"- **{x['what']}**. Why: {x['why']}." + (f" Ask: {x['ask']}." if x.get("ask") else "")
                  for x in pm["handoffs"]]
        lines.append("")
    lines.append("## Actions")
    for a in pm["actions"]:
        lines.append(f"- {a['description']} · risk {a.get('risk', 'n/a')} · {a['status']}"
                     + (f" · approved by {a['approved_by']}" if a.get("approved_by") else "")
                     + (f" · corrected from “{a['corrected_from']}”" if a.get("corrected_from") else "")
                     + (f" · {a['result']}" if a.get("result") else ""))
    lines += ["", "## Timeline"] + [f"- `{t['t']}` {t['text']}" for t in pm["timeline"]]
    lines += ["", "## Prevention"] + [f"- {p['en']}" for p in pm["prevention"]]
    e = pm["engine"]
    lines += ["", "## How Nabd decided",
              f"Route: **{e['route']}** ({e['reason']}). LLM calls: {e['llm_calls']}, tokens in/out "
              f"{e['tokens']['input']}/{e['tokens']['output']} (cache reads {e['tokens']['cache_read']}), "
              f"cost ${e['cost_usd']:.4f}."]
    return "\n".join(lines)


def _md_ar(pm: dict) -> str:
    status = "تم التعافي" if pm["status"] == "resolved" else "تم التصعيد لمهندس"
    lines = [f"# {pm['id']}: تقرير ما بعد الحادثة", "",
             f"**الحالة:** {status} · **زمن الاكتشاف:** {_fmt_s(pm['durations']['detect_s'])} · "
             f"**زمن التعافي:** {_fmt_s(pm['durations']['mttr_s'])}", ""]
    if pm.get("narrative", {}).get("ar"):
        lines += ["## الملخص", pm["narrative"]["ar"], ""]
    imp = pm["impact"]
    if imp.get("total_checkouts"):
        lines += ["## الأثر", f"فشلت {imp['failed_checkouts']} عملية شراء من أصل {imp['total_checkouts']} أثناء الحادثة.", ""]
    lines.append("## الأسباب الجذرية")
    for rc in pm["root_causes"]:
        lines += [f"### {i18n.CATEGORY_AR.get(rc['category'], rc['category'])} في {i18n.NODE_AR.get(rc['node'], rc['node'])}",
                  f"الثقة: {rc['confidence']:.0%}", ""]
        lines += [f"- {e}" for e in rc["evidence"]]
        lines.append("")
    lines.append("## الإجراءات")
    for a in pm["actions"]:
        lines.append(f"- {i18n.action_past_ar(a['name'], a['params'])} · {i18n.STATUS_AR.get(a['status'], a['status'])}"
                     + (f" · {a['count']} عنصر" if a.get("count") else "")
                     + (f" · {round(a['amount_sar']):,} ريال" if a.get("amount_sar") else ""))
    if pm.get("handoffs"):
        lines += ["", "## تم تسليمه لمهندس"] + [f"- {x['what']}" for x in pm["handoffs"]]
    lines += ["", "## الوقاية"] + [f"- {p['ar']}" for p in pm["prevention"]]
    return "\n".join(lines)


async def build(inc, recovered: bool) -> dict:
    end = inc.resolved_at or time.time()
    start = inc.started_at
    kept = [h for h in inc.hypotheses if h.get("kept", True)]
    roots = [h for h in kept if h.get("phase") != "heal"]  # repairs to the books are not causes
    if not roots:
        title = "Unexplained failure"
    elif len(roots) == 1:
        title = roots[0]["title"]
    else:
        title = f"{len(roots)} concurrent root causes: " + ", ".join(r["node"] for r in roots)
    pm = {
        "id": inc.id, "title": title, "kind": inc.kind,
        "status": "resolved" if recovered else "escalated",
        "severity": "SEV-1" if len(roots) > 1 or any(a["severity"] == "critical" for a in inc.alerts.values()) else "SEV-2",
        "started_at": _clock(start), "detected_at": _clock(inc.opened_at), "resolved_at": _clock(end),
        "durations": {"detect_s": round(inc.opened_at - start, 1),
                      "mttr_s": round(end - start, 1) if recovered else None},
        "impact": await _impact(start, end),
        "root_causes": [{k: h.get(k) for k in ("id", "title", "node", "category", "explanation", "evidence",
                                               "confidence", "skeptic", "action", "source")} for h in roots],
        "ruled_out": inc.ruled_out,
        "actions": inc.actions,
        "verification": inc.verification,
        "timeline": [{"t": f"+{_fmt_s(ts - start)}", "text": text} for ts, text in inc.timeline],
        "prevention": [{"en": i18n.PREVENTION[c][0], "ar": i18n.PREVENTION[c][1]}
                       for c in dict.fromkeys(r["category"] for r in kept) if c in i18n.PREVENTION],
        "repairs": [{"description": a["description"], "result": a.get("result"), "count": a.get("count"),
                     "amount_sar": a.get("amount_sar")}
                    for a in inc.actions if a["status"] == "done" and a["name"] in (
                        "replay_orphan_payments", "refund_failed_payments", "replay_lost_events",
                        "quarantine_stuck_message", "restore_catalog_prices")],
        "handoffs": inc.handoffs, "budget": inc.budget(),
        "silent": bool(inc.alerts) and all(a.get("source") == "mizan" for a in inc.alerts.values()),
        "engine": {"route": inc.route, "reason": inc.route_reason, "llm_calls": inc.llm_calls,
                   "tokens": inc.tokens, "cost_usd": inc.cost, "model": MODEL if inc.llm_calls else None},
        "memory_hit": inc.memory_hit,
        "alerts_seen": len(inc.alerts),
    }
    if claude.available and inc.route == "claude":
        pm["narrative"] = await claude.narrative(inc, {
            "title": title, "status": pm["status"], "mttr": _fmt_s(pm["durations"]["mttr_s"]),
            "impact": pm["impact"], "root_causes": [{k: r[k] for k in ("title", "explanation")} for r in roots],
            "actions": [a["description"] for a in inc.actions]}) or {}
        pm["engine"]["cost_usd"] = inc.cost
    pm["markdown"] = {"en": _md_en(pm), "ar": _md_ar(pm)}
    os.makedirs(_DIR, exist_ok=True)
    with open(os.path.join(_DIR, f"{inc.id}.json"), "w", encoding="utf-8") as fh:
        json.dump(pm, fh, ensure_ascii=False, indent=1, default=str)
    return pm


def load_all() -> list[dict]:
    if not os.path.isdir(_DIR):
        return []
    out = []
    for name in sorted(os.listdir(_DIR), reverse=True):
        try:
            with open(os.path.join(_DIR, name), encoding="utf-8") as fh:
                pm = json.load(fh)
            out.append({k: pm.get(k) for k in ("id", "title", "status", "severity", "started_at", "durations",
                                                "engine", "kind")})
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load(incident_id: str) -> dict | None:
    path = os.path.join(_DIR, f"{os.path.basename(incident_id)}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
