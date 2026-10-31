"""Runtime settings. Everything comes from the environment so the same image
runs in the demo, in the benchmark and on someone else's laptop."""

import os

PROM_URL = os.getenv("PROM_URL", "http://prometheus:9090")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
TOXIPROXY_URL = os.getenv("TOXIPROXY_URL", "http://toxiproxy:8474")
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "nabd")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
PG_ADMIN = dict(host="postgres", port=5432, dbname="dukkan", user="postgres", password="postgres")
PG_APP = dict(host="postgres", port=5432, dbname="dukkan", user="dukkan", password="dukkan")

CONFIG_DIR = os.getenv("CONFIG_DIR", "/config")
DATA_DIR = os.getenv("DATA_DIR", "/data")

MODEL = os.getenv("NABD_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("NABD_EFFORT", "medium")
# Nabd's own names win, so a shell that already exports ANTHROPIC_* for another tool cannot redirect it.
LLM_API_KEY = os.getenv("NABD_LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
LLM_BASE_URL = os.getenv("NABD_LLM_BASE_URL") or None
PROVIDER = "anthropic" if not LLM_BASE_URL else ("deepseek" if "deepseek" in LLM_BASE_URL else "compatible")
CLAUDE_AVAILABLE = bool(LLM_API_KEY or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def _llm_name() -> str:
    family = MODEL.replace("claude-", "").rsplit("-", 2)
    if PROVIDER == "anthropic" and len(family) == 3 and family[1].isdigit() and family[2].isdigit():
        return f"Claude {family[0].title()} {family[1]}.{family[2]}"
    if PROVIDER == "deepseek":
        words = MODEL.replace("deepseek-", "").split("-")
        return "DeepSeek " + " ".join(w.upper() if w[:1] == "v" and w[1:].isdigit() else w.title() for w in words)
    return MODEL


LLM_NAME = _llm_name()

# USD per million tokens: (input, output, cache read)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


_OVERRIDE = os.getenv("NABD_PRICE_IN"), os.getenv("NABD_PRICE_OUT"), os.getenv("NABD_PRICE_CACHE", "0")
PRICE_KNOWN = MODEL in PRICES or all(_OVERRIDE[:2])


def token_cost(input_tokens: int, output_tokens: int, cache_read: int = 0, model: str = MODEL) -> float:
    """USD for the given tokens. 0.0 when the model's price is unknown; the UI then shows tokens only."""
    if all(_OVERRIDE[:2]):
        price_in, price_out, price_cache = (float(p) for p in _OVERRIDE)
    else:
        price_in, price_out, price_cache = PRICES.get(model, (0.0, 0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out + cache_read * price_cache) / 1_000_000


class Runtime:
    """Settings the War Room can change while Nabd is running."""

    def __init__(self) -> None:
        self.mode = "autopilot"        # autopilot: medium-risk actions run on their own
        self.engine = "auto"           # auto | core | claude
        self.auto_approve = False      # benchmark only: stands in for the on-call engineer
        self.approval_timeout_s = 180
        self.business_invariants = True  # Mizan's hand-written books (payments, queue, shipping, prices)
        self.learned_invariants = True   # laws Mizan learned from healthy traffic

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "engine": self.engine,
            "auto_approve": self.auto_approve,
            "claude_available": CLAUDE_AVAILABLE,
            "llm_name": LLM_NAME,
            "provider": PROVIDER,
            "price_known": PRICE_KNOWN,
            "model": MODEL,
            "effort": EFFORT,
            "business_invariants": self.business_invariants,
            "learned_invariants": self.learned_invariants,
        }


runtime = Runtime()
