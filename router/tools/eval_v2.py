#!/usr/bin/env python3
"""Evaluation harness v2 (V2-14): metrics, brackets, abstention, permutation test, TEST lock/freeze."""
import argparse, glob, hashlib, itertools, json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(W, "v1_frozen")]
import route_v2 as R  # noqa: E402
import route as V1R  # noqa: E402  (v1, frozen copy)
import eval_route as V1E  # noqa: E402

BIG = 10 ** 9
SYSTEMS = ["v1", "v1-unbudgeted", "b1", "v2", "v2-default"]


def sha_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_bench(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def judged_ids(b):
    s = {g["id"] for g in b["gold"]}
    for k in ("a1", "a2"):
        s |= set(b.get("adjudication", {}).get(k, {}))
    return s


# ---- metrics --------------------------------------------------------------------------------------
def per_query(ids, gold, extra_rel=()):
    rel = {i for i, g in gold.items() if g >= 2} | set(extra_rel)
    r1 = len(rel & set(ids[:1])) / len(rel)
    r5 = len(rel & set(ids[:5])) / len(rel)
    mrr = next((1.0 / (j + 1) for j, i in enumerate(ids) if i in rel), 0.0)
    dcg = sum((2 ** gold.get(i, 0) - 1) / math.log2(j + 2) for j, i in enumerate(ids[:5]))
    ideal = sorted(gold.values(), reverse=True)[:5]
    idcg = sum((2 ** g - 1) / math.log2(j + 2) for j, g in enumerate(ideal))
    return {"r1": r1, "r5": r5, "mrr": mrr, "ndcg5": dcg / idcg if idcg else 0.0}


def mean(v):
    return round(sum(v) / len(v), 4) if v else None


def quality(rows):
    """rows: [(ids, gold)] -> aggregated dict (None if empty)."""
    if not rows:
        return None
    pq = [per_query(i, g) for i, g in rows]
    return {"n": len(rows), "recall_at_1": mean([x["r1"] for x in pq]), "recall_at_5": mean([x["r5"] for x in pq]),
            "mrr": mean([x["mrr"] for x in pq]), "ndcg_at_5": mean([x["ndcg5"] for x in pq])}


def bracket(rows):
    """rows: [(ids, gold, judged)] -> {metric: [raw, all-unjudged-relevant]} (exact per query)."""
    lo, hi = [], []
    for ids, gold, judged in rows:
        unj = [i for i in ids[:5] if i not in judged and i not in gold]
        lo.append(per_query(ids, gold))
        hi.append(per_query(ids, gold, unj))
    f = lambda k, v: mean([x[k] for x in v])
    return {"recall_at_5": [f("r5", lo), f("r5", hi)], "mrr": [f("mrr", lo), f("mrr", hi)]}


def abstain_metrics(per):
    """per: [(abstain, answerable)] -> precision/recall/F1 (positive = unanswerable) and pass-through."""
    un = [a for a, ans in per if not ans]
    an = [a for a, ans in per if ans]
    ab = [(a, ans) for a, ans in per if a]
    tp = sum(1 for a, ans in ab if not ans)
    prec = tp / len(ab) if ab else None
    rec = tp / len(un) if un else None
    f1 = (2 * prec * rec / (prec + rec)) if prec and rec else 0.0
    return {"precision": None if prec is None else round(prec, 4), "recall": None if rec is None else round(rec, 4),
            "f1": round(f1, 4), "pass_through": round(sum(1 for a in an if not a) / len(an), 4) if an else None,
            "n_unanswerable": len(un), "n_answerable": len(an), "n_abstained": len(ab)}


def sign_flip_p(deltas):
    """Two-sided exact sign-flip permutation p on the mean (2^n flips if n <= 20, else 100000 from Random(0))."""
    d = [x for x in deltas]
    if not d or all(abs(x) < 1e-12 for x in d):
        return 1.0
    obs = abs(sum(d))
    n = len(d)
    if n <= 20:
        tot = ge = 0
        for signs in itertools.product((1, -1), repeat=n):
            tot += 1
            ge += abs(sum(s * x for s, x in zip(signs, d))) >= obs - 1e-12
        return round(ge / tot, 6)
    rnd = random.Random(0)
    ge = 0
    for _ in range(100000):
        ge += abs(sum(x if rnd.random() < 0.5 else -x for x in d)) >= obs - 1e-12
    return round((ge + 1) / 100001, 6)


def guard_bench(path, rows):
    """g_blind: DEV-side tools refuse /bench/ paths and TEST rows. Returns an error string or None."""
    if "/bench/" in os.path.abspath(path):
        return "E_BLIND: /bench/ path"
    if any(r.get("split") == "TEST" for r in rows):
        return "E_BLIND: TEST rows"
    return None


def jq(ids, gold):
    """(RR@20 + recall@5)/2 for one query as an exact Fraction."""
    from fractions import Fraction
    rel = {i for i, g in gold.items() if g >= 2}
    rr = next((Fraction(1, j + 1) for j, i in enumerate(ids[:20]) if i in rel), Fraction(0))
    return (rr + Fraction(len(rel & set(ids[:5])), len(rel))) / 2


def objective_J(ix, bench, params, k=20, budget=2000):
    """Exact mean J over the answerable queries of `bench` for budgeted v2 answers (Fraction) and per-class means."""
    from fractions import Fraction
    vals, byc = [], {}
    for b in bench:
        if not b["answerable"]:
            continue
        a = R.route(ix, b["text"], params, k=k, budget=budget)
        v = jq([r[0] for r in a["r"]], {g["id"]: g["grade"] for g in b["gold"]})
        vals.append(v)
        byc.setdefault(b.get("class", "?"), []).append(v)
    mean_ = sum(vals, Fraction(0)) / len(vals) if vals else Fraction(0)
    return mean_, {c: sum(v, Fraction(0)) / len(v) for c, v in sorted(byc.items())}


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(math.ceil(p / 100.0 * len(v))) - 1)] if v else None


# ---- systems --------------------------------------------------------------------------------------
class Systems:
    def __init__(self, ix, v1ix, params):
        self.ix, self.v1ix, self.params, self.pr = ix, v1ix, params[0], params[1]

    def run(self, name, q):
        """Return (ids budgeted, ids unbudgeted, abstain, answer_bytes)."""
        if name in ("v1", "v1-unbudgeted"):
            a = V1R.route(self.v1ix, q, 20, 2000)
            u = V1R.route(self.v1ix, q, 20, BIG)
            ids, idu = [r["id"] for r in a["results"]], [r["id"] for r in u["results"]]
            nb = len(V1R.dumps(a).encode("utf-8")) + 1
            if name == "v1-unbudgeted":
                return idu, idu, a["abstain"], len(V1R.dumps(u).encode("utf-8")) + 1
            return ids, idu, a["abstain"], nb
        if name == "b1":
            ids, ab, _ = V1E.run_system(self.v1ix, [], "b1", q, 20, BIG)
            return ids, ids, ab, None
        params, pr = (self.params, self.pr) if name == "v2" else ({}, "0" * 16)
        a = R.route(self.ix, q, params, pr, k=20, budget=2000)
        u = R.route(self.ix, q, params, pr, k=20, budget=BIG)
        return [r[0] for r in a["r"]], [r[0] for r in u["r"]], a["abstain"]["flag"], len(R.dumps(a).encode("utf-8")) + 1


def evaluate(bench, systems, names):
    gold = {b["qid"]: {g["id"]: g["grade"] for g in b["gold"]} for b in bench}
    judged = {b["qid"]: judged_ids(b) for b in bench}
    cls = {b["qid"]: b.get("class", "?") for b in bench}
    ans = {b["qid"]: bool(b["answerable"]) for b in bench}
    out, pqs = {}, {}
    for s in names:
        per = []
        for b in bench:
            ids, idu, ab, nb = systems.run(s, b["text"])
            per.append({"qid": b["qid"], "ids": ids, "ids_unbudgeted": idu, "abstain": bool(ab), "answer_bytes": nb})
        pqs[s] = per
        A = [p for p in per if ans[p["qid"]]]
        sizes = [p["answer_bytes"] for p in per if p["answer_bytes"] is not None]
        unj = [[i for i in p["ids"][:5] if i not in judged[p["qid"]] and i not in gold[p["qid"]]] for p in A]
        m = {"quality": quality([(p["ids"], gold[p["qid"]]) for p in A]),
             "quality_unbudgeted": quality([(p["ids_unbudgeted"], gold[p["qid"]]) for p in A]),
             "bracket": bracket([(p["ids"], gold[p["qid"]], judged[p["qid"]]) for p in A]) if A else None,
             "bracket_unbudgeted": bracket([(p["ids_unbudgeted"], gold[p["qid"]], judged[p["qid"]]) for p in A]) if A else None,
             "abstain": abstain_metrics([(p["abstain"], ans[p["qid"]]) for p in per]),
             "answer_bytes": {"p50": pct(sizes, 50), "max": max(sizes) if sizes else None,
                              "share_le_2000": round(sum(x <= 2000 for x in sizes) / len(sizes), 4) if sizes else None},
             "unjudged_top5": {"docs": sum(len(u) for u in unj), "queries": sum(1 for u in unj if u)},
             "per_class": {}}
        for c in sorted(set(cls.values())):
            rows = [(p["ids"], gold[p["qid"]]) for p in A if cls[p["qid"]] == c]
            if rows:
                m["per_class"][c] = quality(rows)
        out[s] = {"metrics": m, "per_query": per}
    return out, pqs


def perm_p(pqs, bench, a, b, key):
    gold = {x["qid"]: {g["id"]: g["grade"] for g in x["gold"]} for x in bench}
    ans = {x["qid"] for x in bench if x["answerable"]}
    pa = {p["qid"]: p for p in pqs[a]}
    pb = {p["qid"]: p for p in pqs[b]}
    d = []
    for q in sorted(ans):
        ka = per_query(pa[q]["ids"], gold[q])[key]
        kb = per_query(pb[q]["ids"], gold[q])[key]
        d.append(ka - kb)
    return sign_flip_p(d)


# ---- freeze / TEST guards ---------------------------------------------------------------------------
def freeze_files():
    fs = sorted(glob.glob(os.path.join(W, "tools", "*.py")))
    for sub in ("standards", "v1_frozen"):
        fs += sorted(os.path.join(a, f) for a, _, x in os.walk(os.path.join(W, sub)) for f in x)
    fs += [os.path.join(W, "out", n) for n in ("index/manifest.json", "params.json")] + [os.path.join(W, "eval", "dev.jsonl")]
    return fs


def freeze_map():
    return {os.path.relpath(p, W): sha_file(p) for p in freeze_files() if os.path.isfile(p)}


def write_freeze(path):
    m = freeze_map()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"schema": "ctx.freeze.v1", "files": m}, sort_keys=True, indent=1) + "\n")
    return m


def check_freeze(path):
    if not os.path.isfile(path):
        return "E_NO_FREEZE"
    fz = json.load(open(path))["files"]
    now = freeze_map()
    bad = [k for k in fz if now.get(k) != fz[k]]
    return ("E_FROZEN_CHANGED " + ",".join(bad[:5])) if bad else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench")
    ap.add_argument("--split", choices=["DEV", "TEST"], default="DEV")
    ap.add_argument("--index", default=os.path.join(W, "out", "index"))
    ap.add_argument("--v1-index", default=os.path.join(W, "out", "v1_index"))
    ap.add_argument("--params", default=os.path.join(W, "out", "params.json"))
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--out")
    ap.add_argument("--freeze", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    out_dir = os.path.dirname(os.path.abspath(a.out)) if a.out else os.path.join(W, "out")
    fpath = os.path.join(out_dir, "freeze.json")
    if a.freeze:
        write_freeze(a.out or fpath)
        return 0
    if not a.bench or not a.out:
        return 2
    if a.split == "DEV":
        if "/bench/" in os.path.abspath(a.bench):
            print("E_BLIND: refusing a /bench/ path for DEV", file=sys.stderr)
            return 4
    else:
        err = check_freeze(fpath)
        if err:
            print(err, file=sys.stderr)
            return 4
        lock = os.path.join(out_dir, "run_TEST.lock")
        if os.path.exists(lock):
            print("E_LOCKED", file=sys.stderr)
            return 4
        with open(lock, "w") as f:
            f.write("locked\n")
    bench = [b for b in read_bench(a.bench) if b["split"] == a.split]
    if not bench:
        print("E_EMPTY_SPLIT", file=sys.stderr)
        return 3
    try:
        ix, v1ix = R.Index(a.index), V1R.Index(a.v1_index)
        params = R.load_params(None if a.params == "none" else a.params)
    except (R.IndexError_, OSError, ValueError, KeyError):
        print("index or params invalid", file=sys.stderr)
        return 3
    names = a.systems.split(",")
    sysx = Systems(ix, v1ix, params)
    res, pqs = evaluate(bench, sysx, names)
    na = sum(1 for b in bench if b["answerable"])
    run = {"schema": "ctx.bench-run.v2", "router": R.VERSION, "index_sha256": ix.ixid,
           "params_sha256": None if a.params == "none" or not os.path.isfile(a.params) else sha_file(a.params), "split": a.split,
           "bench_sha256": sha_file(a.bench), "freeze_sha256": sha_file(fpath) if os.path.isfile(fpath) else None,
           "n_queries": len(bench), "systems": res, "thin_dev": a.split == "DEV" and (na < 20 or len(bench) - na < 20),
           "perm_p": {}, "abstain_f1": {s: res[s]["metrics"]["abstain"]["f1"] for s in names},
           "bytes_share_le_budget": {s: res[s]["metrics"]["answer_bytes"]["share_le_2000"] for s in names},
           "brackets": {s: res[s]["metrics"]["bracket"] for s in names},
           "unjudged_top5": {s: res[s]["metrics"]["unjudged_top5"] for s in names},
           "classes": {s: res[s]["metrics"]["per_class"] for s in names},
           "notes": ["v1_index_sha256=" + v1ix.man["index_sha256"],
                     "n_answerable=%d" % na]}
    if "v2" in names:
        for other in ("v1", "v1-unbudgeted"):
            if other in names:
                for key in ("r5", "mrr"):
                    run["perm_p"]["v2_vs_%s_%s" % (other, key)] = perm_p(pqs, bench, "v2", other, key)
    os.makedirs(out_dir, exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(run, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
