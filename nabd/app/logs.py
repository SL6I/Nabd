"""Log compression with Drain3: thousands of raw lines become a handful of
templates with counts, so the LLM reads the shape of the logs, not the bulk."""

import json
import re
import time

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from . import dockerctl

LEVEL_RANK = {"error": 0, "warn": 1, "info": 2, "debug": 3}
ACCESS_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) ")

# Running totals for the War Room's "what the LLM did not have to read" meter.
stats = {"raw_lines": 0, "raw_chars": 0, "templates": 0, "summary_chars": 0}


def _miner() -> TemplateMiner:
    cfg = TemplateMinerConfig()
    cfg.profiling_enabled = False
    cfg.drain_sim_th = 0.5
    cfg.masking_instructions = [
        MaskingInstruction(r"(?<=[\s:=(-])[0-9a-f]{6,}\b", "HEX"),
        MaskingInstruction(r"\b(?:ord|ref|pay|hold)[-_][0-9a-z]+\b", "ID"),
        MaskingInstruction(r"\d+(?:\.\d+)?", "NUM"),
    ]
    return TemplateMiner(config=cfg)


def _parse(line: str) -> dict:
    try:
        rec = json.loads(line)
        if isinstance(rec, dict) and "msg" in rec:
            return rec
    except json.JSONDecodeError:
        pass
    return {"level": "info", "msg": line.strip(), "ts": ""}


def compress(service: str, window_s: int = 60, only_problems: bool = False, since_ts: float | None = None) -> dict:
    window_s = max(10, min(int(window_s or 60), 600))
    lines = dockerctl.logs(service, since_ts or (time.time() - window_s))
    miner = _miner()
    clusters: dict[int, dict] = {}
    raw_chars = 0
    for line in lines:
        raw_chars += len(line)
        rec = _parse(line)
        msg = str(rec.get("msg", ""))[:400]
        if not msg:
            continue
        result = miner.add_log_message(msg)
        cid = result["cluster_id"]
        level = str(rec.get("level", "info")).lower()
        entry = clusters.setdefault(cid, {"count": 0, "level": level, "first_seen": rec.get("ts", ""),
                                          "sample": msg})
        entry["count"] += 1
        entry["last_seen"] = rec.get("ts", "")
        if LEVEL_RANK.get(level, 2) < LEVEL_RANK.get(entry["level"], 2):
            entry["level"], entry["sample"] = level, msg
    templates = []
    for cluster in miner.drain.clusters:
        entry = clusters.get(cluster.cluster_id)
        if not entry:
            continue
        if only_problems and entry["level"] not in ("error", "warn"):
            continue
        templates.append({"template": cluster.get_template(), **entry})
    # access-log lines ("POST /charge 502 31ms") say that something failed; the other lines say why
    templates.sort(key=lambda t: (LEVEL_RANK.get(t["level"], 2), bool(ACCESS_LINE.match(t["template"])), -t["count"]))
    templates = templates[:14]
    summary_chars = sum(len(t["template"]) + len(t["sample"]) + 40 for t in templates)
    stats["raw_lines"] += len(lines)
    stats["raw_chars"] += raw_chars
    stats["templates"] += len(templates)
    stats["summary_chars"] += summary_chars
    return {
        "service": service,
        "window_s": window_s,
        "raw_lines": len(lines),
        "templates_found": len(miner.drain.clusters),
        "problem_lines": sum(e["count"] for e in clusters.values() if e["level"] in ("error", "warn")),
        "templates": templates,
        "est_tokens_raw": raw_chars // 4,
        "est_tokens_summary": summary_chars // 4,
    }
