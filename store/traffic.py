"""Traffic: simulated shoppers that browse and check out all day long. The
number of shoppers comes from /config/traffic.json, so a flash sale can raise it."""

import json
import random
import threading
import time

import httpx

GATEWAY = "http://gateway:8000"
SHOPPERS = ["abdullah", "nora", "khalid", "fouad", "sara", "faisal"]
SKUS = ["DATE-SUKKARI-1KG", "COFFEE-KHAWLANI", "DALLAH-BRASS", "OUD-BUKHOOR", "SHEMAGH-RED", "PRAYER-MAT"]
DEFAULT_SHOPPERS = len(SHOPPERS)


def wanted() -> int:
    try:
        with open("/config/traffic.json", encoding="utf-8") as fh:
            return max(0, int(json.load(fh).get("shoppers", DEFAULT_SHOPPERS)))
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULT_SHOPPERS


def name_of(i: int) -> str:
    return SHOPPERS[i] if i < len(SHOPPERS) else f"guest{i}"


def shopper(i: int) -> None:
    name = name_of(i)
    client = httpx.Client(timeout=4.0)
    time.sleep(random.uniform(0, 3))
    while i < wanted():
        try:
            token = client.post(f"{GATEWAY}/api/login", json={"user": name}).json().get("token", "")
            for _ in range(random.randint(1, 3)):
                client.get(f"{GATEWAY}/api/products")
                time.sleep(random.uniform(0.2, 0.6))
                client.post(f"{GATEWAY}/api/checkout",
                            json={"token": token, "sku": random.choice(SKUS), "qty": random.randint(1, 3)})
                time.sleep(random.uniform(0.3, 0.9))
        except Exception:
            time.sleep(1.0)


if __name__ == "__main__":
    running: dict[int, threading.Thread] = {}
    last = None
    while True:
        target = wanted()
        if target != last:
            print(f"traffic: {target} shoppers", flush=True)
            last = target
        for i in range(target):
            if i not in running or not running[i].is_alive():
                running[i] = threading.Thread(target=shopper, args=(i,), daemon=True)
                running[i].start()
        time.sleep(2)
