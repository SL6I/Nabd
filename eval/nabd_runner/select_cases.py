"""Pre-register the evaluation sample before any agent runs.

Stratified random sample: N cases from every Online Boutique fault category,
drawn with a fixed seed, written to cases.json. Run once; never edit by hand.

    python select_cases.py --per-category 5 --seed 2026
"""

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "cloud-opsbench" / "benchmark" / "boutique"
OUT = Path(__file__).with_name("cases.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-category", type=int, default=5)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    if OUT.exists():
        raise SystemExit(f"{OUT} already exists: the sample is pre-registered and must not be redrawn")

    rng = random.Random(args.seed)
    cases = []
    for category in sorted(p.name for p in BENCH.iterdir() if p.is_dir()):
        ids = sorted((p.name for p in (BENCH / category).iterdir() if p.is_dir()), key=int)
        for case_id in sorted(rng.sample(ids, args.per_category), key=int):
            cases.append({"system": "boutique", "category": category, "case": case_id})
    OUT.write_text(json.dumps({
        "drawn_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed, "per_category": args.per_category,
        "rule": "random.Random(seed).sample per category, categories in sorted order",
        "cases": cases,
    }, indent=1), encoding="utf-8")
    print(f"wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
