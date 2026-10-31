"""The dependency graph of the Dukkan store. Triage uses it to tell root
causes from symptoms, the safety gate uses it to size the blast radius, and
the action planner uses it to fix the deepest dependency first."""

NODES = {
    "traffic":        {"kind": "users",    "label": "Shoppers"},
    "gateway":        {"kind": "service",  "label": "Gateway"},
    "auth":           {"kind": "service",  "label": "Auth"},
    "inventory":      {"kind": "service",  "label": "Inventory"},
    "orders":         {"kind": "service",  "label": "Orders"},
    "payments":       {"kind": "service",  "label": "Payments"},
    "psp":            {"kind": "external", "label": "PSP (mada)"},
    "redis":          {"kind": "infra",    "label": "Redis"},
    "postgres":       {"kind": "infra",    "label": "Postgres"},
    "orders-db-link": {"kind": "link",     "label": "Orders → DB primary link"},
    "events":         {"kind": "queue",    "label": "Order events queue"},
    "fulfillment":    {"kind": "service",  "label": "Fulfillment"},
    "carrier":        {"kind": "external", "label": "Freight carrier"},
    "catalog-sync":   {"kind": "service",  "label": "Catalog sync"},
    "supplier":       {"kind": "external", "label": "Supplier catalog"},
}

DEPENDS_ON = {
    "traffic": ["gateway"],
    "gateway": ["auth", "inventory", "orders", "payments"],
    "auth": [],
    "inventory": ["redis", "postgres"],
    "orders": ["orders-db-link", "events"],
    "orders-db-link": ["postgres"],
    "payments": ["redis", "psp"],
    "psp": [],
    "redis": [],
    "postgres": [],
    # fulfillment consumes the queue asynchronously: nothing a shopper waits on depends on it
    "events": ["redis"],
    "fulfillment": ["events", "postgres", "carrier"],
    "carrier": [],
    # a background job that writes product prices; no request waits on it
    "catalog-sync": ["supplier", "postgres"],
    "supplier": [],
}

SERVICES = ["gateway", "auth", "inventory", "orders", "payments", "psp", "fulfillment", "catalog-sync"]
STATELESS = {"gateway", "auth", "inventory", "orders", "payments", "psp", "fulfillment", "catalog-sync"}
CONTAINERS = SERVICES + ["redis", "postgres", "toxiproxy", "prometheus", "traffic"]


def dependents(node: str) -> list[str]:
    return [n for n, deps in DEPENDS_ON.items() if node in deps]


def upstream_closure(node: str) -> list[str]:
    """Everything that (transitively) depends on node: its blast radius."""
    seen: list[str] = []
    stack = dependents(node)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.append(current)
        stack.extend(dependents(current))
    return seen


def downstream_closure(node: str) -> list[str]:
    seen: list[str] = []
    stack = list(DEPENDS_ON.get(node, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.append(current)
        stack.extend(DEPENDS_ON.get(current, []))
    return seen


def depth(node: str) -> int:
    """Distance from the shoppers. Deeper nodes get fixed first."""
    best = {"traffic": 0}
    frontier = ["traffic"]
    while frontier:
        nxt = []
        for n in frontier:
            for d in DEPENDS_ON.get(n, []):
                if best.get(d, -1) < best[n] + 1:
                    best[d] = best[n] + 1
                    nxt.append(d)
        frontier = nxt
    return best.get(node, 0)


def as_dict() -> dict:
    return {"nodes": NODES, "depends_on": DEPENDS_ON}
