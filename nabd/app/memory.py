"""Incident memory. Each resolved incident is stored with a fingerprint of
its signals; a new incident that looks like an old one is fast-tracked to
the fix that worked last time, with zero LLM tokens."""

import json
import os
import threading
import time

from .settings import DATA_DIR

_PATH = os.path.join(DATA_DIR, "memory.json")


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(_PATH, encoding="utf-8") as fh:
                self.items = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            self.items = []

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _PATH)

    def search(self, signals: set[str], limit: int = 3) -> list[dict]:
        scored = []
        for item in self.items:
            score = jaccard(signals, set(item["signals"]))
            if score > 0:
                scored.append({**item, "similarity": round(score, 2)})
        return sorted(scored, key=lambda i: i["similarity"], reverse=True)[:limit]

    def remember(self, incident_id: str, title: str, signals: set[str], roots: list[dict], mttr_s: float | None) -> None:
        with self._lock:
            self.items.append({
                "incident": incident_id, "title": title, "ts": time.time(),
                "signals": sorted(signals), "roots": roots, "mttr_s": mttr_s, "reused": 0,
            })
            self.items = self.items[-200:]
            self._save()

    def mark_reused(self, incident_id: str) -> None:
        with self._lock:
            for item in self.items:
                if item["incident"] == incident_id:
                    item["reused"] = item.get("reused", 0) + 1
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.items = []
            self._save()


store = MemoryStore()
