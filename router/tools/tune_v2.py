#!/usr/bin/env python3
"""DEV tuning (T8): Stage A ranking grid with parsimony + promotion, Stage B total abstention fit (X2). DEV only, never /bench/ or TEST."""
import argparse, hashlib, itertools, json, os, sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import eval_v2 as E  # noqa: E402
import route_v2 as R  # noqa: E402

BIG = 10 ** 9


def configs(grid, edge_on):
    v = grid["values"]
    vals = [v[k] if (k != "w_edge" or edge_on) else [0] for k in grid["order"]]
    return [dict(zip(grid["order"], c)) for c in itertools.product(*vals)]


def switches(cfg, defaults):
    return sum(1 for k, d in defaults.items() if cfg[k] != d)


def key(cfg):
    return json.dumps(cfg, sort_keys=True)


def mean(v):
    return sum(v, Fraction(0)) / len(v) if v else Fraction(0)


def select(table, classes, defaults, grid_order, delta, floor):
    """table: [(cfg, {qid: J})] in grid order. Returns (chosen cfg, report). Parsimony then promotion over the all-off default."""
    js = [(c, mean(list(q.values()))) for c, q in table]
    best = max(j for _, j in js)
    cand = [i for i, (c, j) in enumerate(js) if j >= best - delta]
    cand.sort(key=lambda i: (switches(table[i][0], defaults), i))
    dflt = next(i for i, (c, _) in enumerate(table) if c == defaults)

    def cls_means(i):
        out = {}
        for q, v in table[i][1].items():
            out.setdefault(classes[q], []).append(v)
        return {c: mean(v) for c, v in out.items()}
    d_mean, d_cls = js[dflt][1], cls_means(dflt)
    trail = []
    for i in cand:
        if i == dflt:
            return table[i][0], {"candidates": len(cand), "promotion": "default_is_best_parsimonious", "trail": trail}
        dj = js[i][1] - d_mean
        cm = cls_means(i)
        worst = min((cm[c] - d_cls[c] for c in cm if c in d_cls), default=Fraction(0))
        trail.append({"cfg": table[i][0], "delta_j": float(dj), "worst_class_delta": float(worst)})
        if dj > 0 and worst >= floor:
            return table[i][0], {"candidates": len(cand), "promotion": "accepted", "trail": trail}
    return defaults, {"candidates": len(cand), "promotion": "none_accepted_keep_default", "trail": trail}


def f1_frac(tp, fp, fn):
    return Fraction(2 * tp, 2 * tp + fp + fn) if (2 * tp + fp + fn) else Fraction(0)


def thresholds(vals, hi):
    s = sorted(set(vals))
    return [0] + [(a + b) // 2 for a, b in zip(s, s[1:])] + [hi]


def fit_abstention(rows, floor=Fraction(4, 5)):
    """rows: [(fixed_abstain, exact, m, cov, peak, answerable)]. Returns dict(m_min,tau_cov,tau_peak,f1) or None (tau null)."""
    an = [r for r in rows if r[5]]
    un = [r for r in rows if not r[5]]
    if not an or not un:
        raise ValueError("E_CALIBRATION")
    tc = thresholds([r[3] for r in rows if not r[0]], BIG + 1)
    tp_ = thresholds([r[4] for r in rows if not r[0]], BIG + 1)
    best = None
    for mm in (1, 2):
        for c in tc:
            for p in tp_:
                def ab(r):
                    if r[0]:
                        return True
                    if r[1]:
                        return False
                    return r[2] < mm or r[3] < c or r[4] < p
                a_ab = sum(1 for r in an if ab(r))
                if Fraction(len(an) - a_ab, len(an)) < floor:
                    continue
                tp = sum(1 for r in un if ab(r))
                f1 = f1_frac(tp, a_ab, len(un) - tp)
                k = (-f1, a_ab, mm, c, p)
                if best is None or k < best[0]:
                    best = (k, dict(m_min=mm, tau_cov=c, tau_peak=p, f1=f1, abstained_answerable=a_ab, tp=tp))
    return best[1] if best else None


def sha_file(p):
    return E.sha_file(p)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--bench", required=True)
    ap.add_argument("--null")
    ap.add_argument("--grid")
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--unjudged-out")
    ap.add_argument("--stop-on-unjudged", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if R.is_bench_path(a.bench):
        print("E_BLIND: /bench/ path", file=sys.stderr)
        return 4
    rows = E.read_bench(a.bench)
    err = E.guard_bench(a.bench, rows)
    if err:
        print(err, file=sys.stderr)
        return 4
    ans = [r for r in rows if r["answerable"]]
    if not ans or len(rows) - len(ans) < 1:
        print("E_CALIBRATION", file=sys.stderr)
        return 5
    if not (a.grid and a.out and a.null):
        return 2
    grid = json.load(open(a.grid, encoding="utf-8"))
    null = json.load(open(a.null, encoding="utf-8"))
    ix = R.Index(a.index)
    edge_on = bool(null["enabled"])
    cfgs = configs(grid, edge_on)
    classes = {b["qid"]: b.get("class", "?") for b in ans}
    gold = {b["qid"]: {g["id"]: g["grade"] for g in b["gold"]} for b in ans}
    table = []
    for c in cfgs:
        per = {}
        for b in ans:
            r = R.route(ix, b["text"], c, k=20, budget=2000)
            per[b["qid"]] = E.jq([x[0] for x in r["r"]], gold[b["qid"]])
        table.append((c, per))
    delta = Fraction(1, 2 * len(ans))
    floor = Fraction(grid["promotion_class_floor"])
    chosen, prom = select(table, classes, grid["defaults"], grid["order"], delta, floor)
    # leave-one-class-out (informational)
    loo = {}
    for c in sorted(set(classes.values())):
        keep = [q for q in classes if classes[q] != c]
        if not keep:
            continue
        t2 = [(cf, {q: v for q, v in per.items() if q in keep}) for cf, per in table]
        ch, _ = select(t2, classes, grid["defaults"], grid["order"], Fraction(1, 2 * len(keep)), floor)
        loo[c] = ch
    ranked = sorted(range(len(table)), key=lambda i: (-mean(list(table[i][1].values())), switches(table[i][0], grid["defaults"]), i))
    top10 = [{"cfg": table[i][0], "J": round(float(mean(list(table[i][1].values()))), 6)} for i in ranked[:10]]
    # unjudged top-5 of the chosen config (one adjudication round is the orchestrator's; pairs are emitted)
    unj = []
    for b in ans:
        r = R.route(ix, b["text"], chosen, k=20, budget=2000)
        j = E.judged_ids(b)
        for x in r["r"][:5]:
            if x[0] not in j:
                unj.append({"qid": b["qid"], "id": x[0]})
    if a.unjudged_out:
        with open(a.unjudged_out, "w", encoding="utf-8", newline="\n") as f:
            for u in unj:
                f.write(json.dumps(u, sort_keys=True) + "\n")
    # Stage B
    feats = []
    for b in rows:
        r = R.route(ix, b["text"], chosen, k=20, budget=2000)
        ab = r["abstain"]
        feats.append((ab["flag"] and ab["reason"] in ("no_query_term", "no_candidate"), ab["reason"] == "exact_id", ab["m"], ab["cov"], ab["peak"], bool(b["answerable"])))
    fit = fit_abstention(feats)
    params = {"schema": "ctx.router-params.v2", "profile": chosen["profile"], "w_exp": chosen["w_exp"], "w_phr": chosen["w_phr"], "lcp5": chosen["lcp5"],
              "w_dom": chosen["w_dom"], "w_edge": chosen["w_edge"], "intent_boost": chosen["intent_boost"],
              "m_min": fit["m_min"] if fit else 1, "tau_cov": fit["tau_cov"] if fit else None, "tau_peak": fit["tau_peak"] if fit else None,
              "fitted_on": "DEV", "dev_sha256": sha_file(a.bench), "grid_sha256": sha_file(a.grid), "index_sha256": ix.ixid,
              "null_test": {"delta_j_ppm": null["delta_j_ppm"], "p_num": null["p_num"], "p_den": 201, "enabled": edge_on}}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(params, sort_keys=True, indent=1) + "\n")
    rep = {"schema": "ctx.tune-report.v1", "configs": len(cfgs), "edge_in_grid": edge_on, "n_answerable": len(ans), "delta": str(delta),
           "J_default": float(mean(list(next(p for c, p in table if c == grid["defaults"]).values()))),
           "J_best": float(max(mean(list(p.values())) for _, p in table)), "chosen": chosen, "promotion": prom, "top10": top10,
           "leave_one_class_out": loo, "unjudged_top5_pairs": len(unj), "unjudged_queries": len({u["qid"] for u in unj}),
           "abstention_fit": None if not fit else {k: (float(v) if isinstance(v, Fraction) else v) for k, v in fit.items()},
           "thin_dev": len(ans) < 20 or len(rows) - len(ans) < 20}
    if a.report:
        with open(a.report, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, sort_keys=True, indent=1) + "\n")
    if unj and a.stop_on_unjudged:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
