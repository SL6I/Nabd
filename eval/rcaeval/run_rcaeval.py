"""External check: Nabd's learned laws on public microservice incidents it has never seen.

RCAEval (Pham et al., WWW 2025, arXiv 2412.17015) publishes recorded fault injections on two
open-source shops we did not build:
  RE1-OB  Online Boutique, 125 cases (5 services x 5 faults: cpu, mem, disk, delay, loss x 5 runs)
  RE1-SS  Sock Shop,       125 cases (the same design)
Each case is a table of per-service metrics at 1 s and the moment the fault was injected.

The task is root-cause localization: rank the services, the injected one should come first.
We run, on exactly the same inputs:
  laws      the method in nabd/app/learned.py, offline: learn every pair of metrics that moves
            together on the healthy part, then rank services by the share of their laws that
            broke after the injection (Jiang, Chen & Yoshihira, ICAC 2006)
  laws+z    the same, with a fallback for metrics that hold no law at all: they are ranked by
            how far, and how persistently, they left their own healthy range
  baro      BARO's root-cause scorer (Pham et al., FSE 2024), the RCAEval reference method,
            from the authors' package fse-baro
  nsigma    the simplest baseline: largest z-score after the injection

The parameters are fixed in PARAMS and were chosen before running on RE1-SS. RE1-OB served as
the development set, RE1-SS as the held-out set. Both are reported.

    python eval/rcaeval/run_rcaeval.py            # expects eval/rcaeval/data/RE1-OB and RE1-SS
"""

import argparse
import glob
import json
import os
import time
from collections import defaultdict

import numpy as np
import pandas as pd

PARAMS = {
    "window_s": 10,        # metrics are averaged over 10 s windows, as Nabd judges windows
    "min_corr": 0.6,       # the same correlation floor a learned law needs in Nabd
    "confirm": 3,          # a law breaks only after 3 windows in a row, as in Nabd
    "q": 0.99,             # threshold = slack x the 99th percentile of the law's healthy error
    "slack": 1.25,         # the same slack Nabd applies to its ACI quantile
}

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")


# ---------------------------------------------------------------- loading


def load_case(case_dir):
    path = os.path.join(case_dir, "simple_data.csv")
    if not os.path.exists(path):
        path = os.path.join(case_dir, "data.csv")
    df = pd.read_csv(path)
    df = df[pd.to_numeric(df["time"], errors="coerce").notna()]
    with open(os.path.join(case_dir, "inject_time.txt")) as f:
        inject = int(float(f.read().strip()))
    return df, inject


def service_of(col):
    return col.split("_")[0]


def windows(df, inject, w):
    """Average every metric over w-second windows, split at the injection."""
    t = df["time"].to_numpy()
    X = df[[c for c in df.columns if "time" not in c]].apply(pd.to_numeric, errors="coerce")
    X = X.ffill().fillna(0.0)
    bins = np.floor((t - inject) / w).astype(int)  # windows before the injection are negative
    G = X.groupby(bins).mean()
    return G[G.index < 0], G[G.index >= 0]


# ---------------------------------------------------------------- learned laws


def learned_laws(train, test, p=PARAMS):
    """Learn pairwise laws on healthy windows, judge them after the injection.

    A law between two metrics is the reduced-major-axis line through their standardized values:
    moving together along it (more load, both up) never breaks it; drifting off it does.
    Returns per-metric (laws, broken, magnitude) and the per-metric univariate persistence.
    """
    mu, sd = train.mean(), train.std(ddof=0)
    keep = sd[sd > 1e-9].index
    tr = ((train[keep] - mu[keep]) / sd[keep]).to_numpy()
    te = ((test[keep] - mu[keep]) / sd[keep]).to_numpy()
    cols = list(keep)
    n = len(cols)

    r = np.corrcoef(tr, rowvar=False) if n > 1 else np.zeros((1, 1))
    r = np.nan_to_num(r)
    laws = np.zeros(n)
    broken = np.zeros(n)
    mag = np.zeros(n)
    ii, jj = np.triu_indices(n, 1)
    sel = np.abs(r[ii, jj]) >= p["min_corr"]
    ii, jj = ii[sel], jj[sel]
    rr = r[ii, jj]
    s = np.sign(rr)
    scale = np.sqrt(np.maximum(2 * (1 - np.abs(rr)), 1e-6))
    d_tr = (tr[:, jj] - s * tr[:, ii]) / scale
    d_te = (te[:, jj] - s * te[:, ii]) / scale
    thr = p["slack"] * np.quantile(np.abs(d_tr), p["q"], axis=0)
    thr = np.maximum(thr, 1e-6)
    over = np.abs(d_te) / thr  # > 1 means the law is off this window
    k = p["confirm"]
    blame_i = np.full(len(ii), 0.5)
    if len(te) >= k and len(ii):
        # the weakest of k consecutive windows: a law is broken only if all k are off
        run = np.min(np.stack([over[i:len(over) - k + 1 + i] for i in range(k)]), axis=0)
        sustained = run.max(axis=0)
        # which side moved: over the k windows where the law was most off, compare how far each
        # metric left its own healthy range. A broken law names the side that moved.
        at = run.argmax(axis=0)
        idx = at[None, :] + np.arange(k)[:, None]
        zi = np.abs(te[idx, ii[None, :]]).mean(axis=0)
        zj = np.abs(te[idx, jj[None, :]]).mean(axis=0)
        blame_i = zi / (zi + zj + 1e-9)
    else:
        sustained = np.zeros(len(ii))
    is_broken = sustained > 1.0
    blame = np.zeros(n)
    np.add.at(blame, ii, np.where(is_broken, blame_i * sustained, 0))
    np.add.at(blame, jj, np.where(is_broken, (1 - blame_i) * sustained, 0))
    np.add.at(laws, ii, 1)
    np.add.at(laws, jj, 1)
    np.add.at(broken, ii, is_broken)
    np.add.at(broken, jj, is_broken)
    np.add.at(mag, ii, np.where(is_broken, sustained, 0))
    np.add.at(mag, jj, np.where(is_broken, sustained, 0))

    # univariate persistence: the weakest |z| over k consecutive windows, at its peak
    az = np.abs(te)
    if len(te) >= k:
        uni = np.min(np.stack([az[i:len(az) - k + 1 + i] for i in range(k)]), axis=0).max(axis=0)
    else:
        uni = az.max(axis=0)
    return {"cols": cols, "laws": laws, "broken": broken, "mag": mag, "uni": uni, "blame": blame,
            "n_laws": int(len(ii)), "n_broken": int(is_broken.sum())}


def rank_laws(L, fallback, how="share"):
    """Services by the share of their laws that broke (Jiang); ties by how far they broke.

    how="blame": by the broken laws that name the metric as the side that moved, weighted by how
    far each law broke.
    """
    per = {}
    for c, l, b, m, u, bl in zip(L["cols"], L["laws"], L["broken"], L["mag"], L["uni"], L["blame"]):
        if l > 0 and how == "blame":
            score = (bl, u)
        elif l > 0:
            score = (b / l, m / max(b, 1))
        elif fallback:
            # no law to judge it by: place it by its own departure, below any metric with a
            # majority of broken laws, on the same 0..1 scale (8 sigma held for 30 s counts as 1)
            score = (min(u / 8.0, 1.0) * 0.5, u)
        else:
            continue
        svc = service_of(c)
        per[svc] = max(per.get(svc, (0, 0)), score)
    return [s for s, _ in sorted(per.items(), key=lambda kv: kv[1], reverse=True)]


# ---------------------------------------------------------------- baselines


def to_service_ranks(ranks):
    out = []
    for r in ranks:
        s = service_of(r)
        if s not in out:
            out.append(s)
    return out


def run_baro(df, inject):
    from baro.root_cause_analysis import robust_scorer
    return to_service_ranks(robust_scorer(df.copy(), inject_time=inject)["ranks"])


def run_nsigma(df, inject):
    from baro.root_cause_analysis import nsigma
    return to_service_ranks(nsigma(df.copy(), inject_time=inject)["ranks"])


# ---------------------------------------------------------------- evaluation


def score(rows):
    out = {}
    for k in (1, 2, 3, 4, 5):
        out[f"AC@{k}"] = float(np.mean([r["truth"] in r["ranks"][:k] for r in rows])) if rows else 0.0
    out["Avg@5"] = float(np.mean([out[f"AC@{k}"] for k in (1, 2, 3, 4, 5)]))
    out["n"] = len(rows)
    return out


METHODS = ["laws", "laws+z", "laws-blame", "baro", "nsigma"]


def run_dataset(root, name):
    cases = sorted(glob.glob(os.path.join(root, name, "*", "*", "inject_time.txt")))
    rows = []
    healthy = []
    for path in cases:
        case_dir = os.path.dirname(path)
        folder = os.path.basename(os.path.dirname(case_dir))
        truth, fault = folder.rsplit("_", 1)
        df, inject = load_case(case_dir)
        train, test = windows(df, inject, PARAMS["window_s"])
        L = learned_laws(train, test)
        base = {"dataset": name, "case": f"{folder}/{os.path.basename(case_dir)}", "truth": truth, "fault": fault}
        rows.append({**base, "method": "laws", "ranks": rank_laws(L, False), "laws": L["n_laws"], "broken": L["n_broken"]})
        rows.append({**base, "method": "laws+z", "ranks": rank_laws(L, True)})
        rows.append({**base, "method": "laws-blame", "ranks": rank_laws(L, True, "blame")})
        try:
            rows.append({**base, "method": "baro", "ranks": run_baro(df, inject)})
        except Exception as e:  # noqa: BLE001 - report, don't hide
            rows.append({**base, "method": "baro", "ranks": [], "error": str(e)[:200]})
        try:
            rows.append({**base, "method": "nsigma", "ranks": run_nsigma(df, inject)})
        except Exception as e:  # noqa: BLE001
            rows.append({**base, "method": "nsigma", "ranks": [], "error": str(e)[:200]})

        # detection: one model learned on the first half of the healthy period judges both the
        # second half (nothing injected) and the same length after the injection
        half = len(train) // 2
        H = learned_laws(train.iloc[:half], train.iloc[half:])
        F = learned_laws(train.iloc[:half], test.iloc[:len(train) - half])
        healthy.append({"case": base["case"], "laws": H["n_laws"],
                        "healthy_broken": H["n_broken"] / max(H["n_laws"], 1),
                        "fault_broken": F["n_broken"] / max(F["n_laws"], 1)})
    return rows, healthy


def table(rows, group_key=None):
    lines = []
    if group_key is None:
        lines.append("| Method | AC@1 | AC@3 | Avg@5 | cases |")
        lines.append("|---|---|---|---|---|")
        for m in METHODS:
            s = score([r for r in rows if r["method"] == m])
            lines.append(f"| {m} | {s['AC@1']:.2f} | {s['AC@3']:.2f} | {s['Avg@5']:.2f} | {s['n']} |")
    else:
        keys = sorted({r[group_key] for r in rows})
        lines.append("| Method | " + " | ".join(keys) + " |")
        lines.append("|---" * (len(keys) + 1) + "|")
        for m in METHODS:
            cells = [f"{score([r for r in rows if r['method'] == m and r[group_key] == k])['Avg@5']:.2f}" for k in keys]
            lines.append(f"| {m} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--datasets", default="RE1-OB,RE1-SS")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    detection = {}
    all_rows, md = [], ["# Learned laws on RCAEval (external check)", "",
                        f"Parameters (fixed before the held-out run): `{json.dumps(PARAMS)}`", ""]
    for name in args.datasets.split(","):
        t0 = time.time()
        rows, healthy = run_dataset(args.data, name)
        all_rows += rows
        hb = np.array([h["healthy_broken"] for h in healthy])
        fb = np.array([h["fault_broken"] for h in healthy])
        auroc = float(np.mean([(f > hb).mean() + 0.5 * (f == hb).mean() for f in fb]))
        mean_laws = np.mean([h["laws"] for h in healthy]) if healthy else 0
        det = {"auroc": auroc, "healthy_median": float(np.median(hb)), "fault_median": float(np.median(fb)),
               "healthy_p90": float(np.quantile(hb, 0.9))}
        md += [f"## {name}", "", table(rows), "", "Avg@5 by fault type:", "", table(rows, "fault"), "",
               f"Detection: one model learned on the first half of the healthy period (about "
               f"{mean_laws:.0f} laws) judges the second half and the same length after the injection. "
               f"Share of laws broken: median {det['healthy_median']:.1%} healthy vs "
               f"{det['fault_median']:.1%} after the fault; AUROC {det['auroc']:.2f}.", "",
               f"_{time.time() - t0:.0f} s_", ""]
        detection[name] = det
        print(f"{name}: {len(rows) // len(METHODS)} cases in {time.time() - t0:.0f} s")
        print(table(rows))
        print(table(rows, "fault"))
        print("detection", json.dumps(det))
    os.makedirs(RESULTS, exist_ok=True)
    base = os.path.join(RESULTS, f"rcaeval-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"params": PARAMS, "detection": detection, "rows": all_rows}, f, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("wrote", base + ".md")


if __name__ == "__main__":
    main()
