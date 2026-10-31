"""Versioned service config with a deploy history, shared with the services
through the config volume. A bad deploy and its rollback both land here."""

import copy
import json
import os
import threading
import time

from .settings import CONFIG_DIR

BASELINE = {
    "payments": {"provider_url": "http://psp:8000/v1/charges", "timeout_ms": 2000},
    "orders": {"db_route": "primary", "write_mode": "sync", "batch_size": 8, "event_queue_max": 0},
    "inventory": {"cache_ttl_s": 30, "cache_host": "redis", "reserve_mode": "sync"},
    "auth": {"token_ttl_s": 3600},
    "catalog-sync": {"reprice_mode": "delta", "fx_rate": 3.75, "interval_s": 5},
}

_lock = threading.Lock()
_HISTORY = os.path.join(CONFIG_DIR, "deploys.json")


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_history() -> list[dict]:
    try:
        with open(_HISTORY, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_service(service: str, version: int, config: dict) -> None:
    _write_json(os.path.join(CONFIG_DIR, f"{service}.json"), {"version": version, **config})


def reset_baseline() -> None:
    """Every service back to v1, history rewritten as one quiet initial deploy each."""
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        history = []
        two_days_ago = time.time() - 2 * 86400
        for i, (service, config) in enumerate(BASELINE.items()):
            _write_service(service, 1, config)
            history.append({
                "id": f"dep-{1000 + i}", "service": service, "version": 1,
                "ts": two_days_ago + i * 600, "author": "release-bot",
                "message": f"release 2026.10: {service} baseline configuration",
                "diff": {}, "config": copy.deepcopy(config),
            })
        _write_json(_HISTORY, history)


def init() -> None:
    # a service whose file lacks a baseline key (an older layout) counts as missing
    missing = [s for s in BASELINE if not os.path.exists(os.path.join(CONFIG_DIR, f"{s}.json"))
               or not set(BASELINE[s]) <= set(current(s))]
    if not os.path.exists(_HISTORY) or missing:
        reset_baseline()


def current(service: str) -> dict:
    try:
        with open(os.path.join(CONFIG_DIR, f"{service}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, **BASELINE.get(service, {})}


def deploy(service: str, changes: dict, author: str, message: str, ts: float | None = None) -> dict:
    with _lock:
        cur = current(service)
        version = int(cur.get("version", 1)) + 1
        config = {k: v for k, v in cur.items() if k != "version"}
        diff = {k: [config.get(k), v] for k, v in changes.items() if config.get(k) != v}
        config.update(changes)
        _write_service(service, version, config)
        history = _read_history()
        entry = {
            "id": f"dep-{1000 + len(history)}", "service": service, "version": version,
            "ts": ts or time.time(), "author": author, "message": message,
            "diff": diff, "config": copy.deepcopy(config),
        }
        history.append(entry)
        _write_json(_HISTORY, history)
        return entry


def rollback_preview(service: str) -> dict:
    """What a rollback would change, without changing anything."""
    history = [h for h in _read_history() if h["service"] == service]
    cur_version = int(current(service).get("version", 1))
    previous = [h for h in history if h["version"] < cur_version]
    if not previous:
        raise ValueError(f"{service} has no earlier version to roll back to")
    target = previous[-1]
    cur = {k: v for k, v in current(service).items() if k != "version"}
    return {"current_version": cur_version, "restored_version": target["version"], "current": cur,
            "changes": {k: v for k, v in target["config"].items() if cur.get(k) != v}}


def rollback(service: str) -> dict:
    """Re-deploy the config of the version before the current one."""
    preview = rollback_preview(service)
    entry = deploy(service, preview["changes"], author="nabd",
                   message=f"rollback {service} to the config of v{preview['restored_version']}")
    entry["rolled_back_from"] = preview["current_version"]
    entry["restored_version"] = preview["restored_version"]
    return entry


def history(service: str | None = None, limit: int = 10) -> list[dict]:
    items = _read_history()
    if service and service != "all":
        items = [h for h in items if h["service"] == service]
    return items[-limit:]
