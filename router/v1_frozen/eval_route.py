#!/usr/bin/env python3
"""Metrics, baselines, latency and determinism checks for the router pilot."""
import sys

BASE = set(sys.modules)
import argparse, hashlib, json, math, os, random, re, time  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route as R  # noqa: E402
import build_index as B  # noqa: E402

SYSTEMS = ["router", "router-no-expand", "router-bm25f", "b1", "b2"]
BIG = 10 ** 9


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(math.ceil(p / 100.0 * len(v))) - 1)], 3) if v else None


def b1_scores(ix, terms):
    st, N = ix.stats, ix.stats["N"]
    avg = (st["avg_len"]["title"] + st["avg_len"]["purpose"]) or 1.0
    sc = {}
    for t in terms:
        df = st["df"].get(t, 0)
        if not df:
            continue
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
        tf = {}
        for f in ("title", "purpose"):
            for d, n in ix.posting(t).get(f, []):
                tf[d] = tf.get(d, 0) + n
        for d in sorted(tf):
            ln = ix.docs[d]["fields"]["title_len"] + ix.docs[d]["fields"]["purpose_len"]
            x = tf[d] * (K1 + 1) / (tf[d] + K1 * (1 - 0.75 + 0.75 * ln / avg))
            sc[d] = sc.get(d, 0.0) + idf * x
    return {d: round(v, 9) for d, v in sc.items()}


K1 = 1.2


def rank_ids(ix, sc, allowed=None):
    ds = range(len(ix.docs)) if allowed is None else allowed
    return sorted(ds, key=lambda d: (-sc.get(d, 0.0), R.natkey(ix.docs[d]["id"])))


def subject_of(ix, subj, terms):
    best, bs = None, 0
    for s in subj:
        w = set(ix.toks(s["subject"] + " " + " ".join(s["keywords"])))
        o = len(w & set(terms))
        if o > bs:
            best, bs = s["subject_id"], o
    return best


def run_system(ix, subj, name, q, k, budget):
    """Return (ranked ids, abstain, answer bytes or None)."""
    if name.startswith("router"):
        kw = {"no_expand": name == "router-no-expand"}
        if name == "router-bm25f":
            kw["channels"] = ("bm25f",)
        a = R.route(ix, q, k, budget, **kw)
        return [r["id"] for r in a["results"]], a["abstain"], len(R.dumps(a).encode("utf-8")) + 1
    terms = sorted(set(ix.toks(q)))
    sc = b1_scores(ix, terms)
    if name == "b1":
        ids = [d for d in rank_ids(ix, sc) if sc.get(d, 0) > 0][:k]
    else:
        sid = subject_of(ix, subj, terms)
        ids = [d for d in rank_ids(ix, sc, [d for d, x in enumerate(ix.docs) if x["subject"] == sid])][:k] if sid else []
    return [ix.docs[d]["id"] for d in ids], not ids, None


def quality(rows):
    """rows: list of (ranked ids, gold {id: grade})."""
    if not rows:
        return None
    r1, r5, mrr, nd = [], [], [], []
    for ids, gold in rows:
        rel = {i for i, g in gold.items() if g >= 2}
        r1.append(len(rel & set(ids[:1])) / len(rel))
        r5.append(len(rel & set(ids[:5])) / len(rel))
        mrr.append(next((1.0 / (j + 1) for j, i in enumerate(ids) if i in rel), 0.0))
        dcg = sum((2 ** gold.get(i, 0) - 1) / math.log2(j + 2) for j, i in enumerate(ids[:5]))
        ideal = sorted(gold.values(), reverse=True)[:5]
        idcg = sum((2 ** g - 1) / math.log2(j + 2) for j, g in enumerate(ideal))
        nd.append(dcg / idcg if idcg else 0.0)
    m = lambda v: round(sum(v) / len(v), 4)
    return {"recall_at_1": m(r1), "recall_at_5": m(r5), "mrr": m(mrr), "ndcg_at_5": m(nd), "n": len(rows)}


def anchors_ok(root, ix):
    bad, total, cache = [], 0, {}
    for d in ix.docs:
        for r in d["read"]:
            total += 1
            p = os.path.join(root, r["path"])
            if p not in cache:
                cache[p] = open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else None
            if cache[p] is None or ('id="%s"' % r["anchor"]) not in cache[p]:
                bad.append([d["id"], r["anchor"]])
        cache = {k: v for k, v in cache.items() if k.endswith(d["read"][0]["path"])}
    return {"checked": total, "unresolved": len(bad), "examples": bad[:5], "pass": not bad}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--bench", required=True)
    ap.add_argument("--split", default="ALL", choices=["TEST", "DEV", "ALL"])
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--out", required=True)
    ap.add_argument("--vocab", default="standards/vocabulary.json")
    ap.add_argument("--freeze")
    ap.add_argument("--cold")
    ap.add_argument("--no-gold", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not (os.path.isdir(a.index) and os.path.isfile(a.bench)):
        return 3
    bench = [json.loads(l) for l in open(a.bench, encoding="utf-8") if l.strip()]
    sel = [b for b in bench if a.split in ("ALL", b["split"])]
    systems = a.systems.split(",")
    t0 = time.perf_counter()
    ix = R.Index(a.index)
    load_ms = round((time.perf_counter() - t0) * 1000, 3)
    subj = B.load(os.path.join(a.root, "catalogue", "subjects.json"))["subjects"]
    freeze = a.freeze or os.path.join(os.path.dirname(os.path.abspath(a.out)), "freeze.json")
    out = {"schema": "ctx.bench-run.v1", "router": R.VERSION, "index_sha256": ix.man["index_sha256"], "split": a.split,
           "bench_sha256": sha(a.bench), "freeze_sha256": sha(freeze) if os.path.isfile(freeze) else None,
           "n_queries": len(sel), "tokens_measured_by": "bytes_upper_bound"}
    gold = {b["qid"]: {g["id"]: g["grade"] for g in b["gold"]} for b in bench}
    answerable = {q for q, g in gold.items() if any(v >= 2 for v in g.values())}
    systems_out = {}
    for s in systems:
        per, lat, sizes = [], [], []
        for b in sel:
            ids, ab, nb = run_system(ix, subj, s, b["text"], 5, 2000)
            ids_u, _, _ = run_system(ix, subj, s, b["text"], 5, BIG)
            per.append({"qid": b["qid"], "ids": ids, "ids_unbudgeted": ids_u, "abstain": ab, "answer_bytes": nb})
            if nb is not None:
                sizes.append(nb)
            for _ in range(a.repeat):
                t = time.perf_counter()
                run_system(ix, subj, s, b["text"], 5, 2000)
                lat.append((time.perf_counter() - t) * 1000)
        m = {"latency_ms_warm_p50": pct(lat, 50), "latency_ms_warm_p99": pct(lat, 99),
             "tokens_per_answer_p50": pct(sizes, 50), "tokens_per_answer_max": max(sizes) if sizes else None}
        if not a.no_gold:
            aq = [p for p in per if p["qid"] in answerable]
            m["quality"] = quality([(p["ids"], gold[p["qid"]]) for p in aq])
            m["quality_unbudgeted"] = quality([(p["ids_unbudgeted"], gold[p["qid"]]) for p in aq])
            un = [p for p in per if p["qid"] not in answerable]
            ab = [p for p in per if p["abstain"]]
            m["abstain_precision"] = round(sum(p["qid"] not in answerable for p in ab) / len(ab), 4) if ab else None
            m["unanswerable_n"] = len(un)
        else:
            m["abstain_count"] = sum(p["abstain"] for p in per)
        systems_out[s] = {"metrics": m, "per_query": per}
    out["systems"] = systems_out
    out["index_load_ms"] = load_ms
    # determinism: twice + shuffled posting order / doc iteration
    d1 = [R.dumps(R.route(ix, b["text"])) for b in sel]
    d2 = [R.dumps(R.route(ix, b["text"])) for b in sel]
    rnd = random.Random(20240601)
    for k, v in ix.post.items():
        if k.startswith("f:"):
            for term in v.values():
                for f in term.values():
                    rnd.shuffle(f)
        else:
            for f in v.values():
                rnd.shuffle(f)
    d3 = [R.dumps(R.route(ix, b["text"])) for b in sel]
    h = lambda x: hashlib.sha256("\n".join(x).encode()).hexdigest()
    out["determinism"] = {"repeat_identical": h(d1) == h(d2), "shuffled_identical": h(d1) == h(d3), "answers_sha256": h(d1)}
    out["rebuild_identity"] = {"exit_code": B.main(["--root", a.root, "--out", a.index, "--vocab", a.vocab, "--check"])}
    out["anchor_resolvability"] = anchors_ok(a.root, ix)
    sizes = [os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(a.index) for f in fs]
    out["file_sizes"] = {"files": len(sizes), "total_bytes": sum(sizes), "max_bytes": max(sizes),
                         "frac_le_384kb": round(sum(s <= 384 * 1024 for s in sizes) / len(sizes), 4),
                         "frac_le_1mb": round(sum(s <= 1 << 20 for s in sizes) / len(sizes), 4)}
    local = {"route", "build_index", "eval_route"}
    std = getattr(sys, "stdlib_module_names", set())
    mods = sorted({m.split(".")[0] for m in set(sys.modules) - BASE} - set(std) - local)
    out["stdlib_check"] = {"non_stdlib_modules": [m for m in mods if not m.startswith("_")], "pass": not [m for m in mods if not m.startswith("_")]}
    out["nodes"] = {"indexed": len(ix.docs), "inline": ix.man["inline"], "detail": ix.man["detail"], "edges": ix.man["edges"]}
    if a.cold and os.path.isfile(a.cold):
        c = json.load(open(a.cold))["ms"]
        out["latency_ms_cold"] = {"p50": pct(c, 50), "p99": pct(c, 99), "n": len(c)}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
    return 0 if out["determinism"]["shuffled_identical"] and out["anchor_resolvability"]["pass"] else 5


if __name__ == "__main__":
    sys.exit(main())
