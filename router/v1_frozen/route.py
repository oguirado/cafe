#!/usr/bin/env python3
"""Deterministic offline router v0: BM25F + vocabulary tags + family-graph PPR, fused by RRF."""
import argparse, hashlib, json, math, os, re, sys, unicodedata

VERSION = "route.py/0.1"
K1 = 1.2
WB = {"title": (3.0, 0.3), "purpose": (2.5, 0.3), "tags": (2.0, 0.0), "subject": (1.5, 0.0), "outputs": (1.0, 0.75),
      "inputs": (0.5, 0.75), "family": (1.0, 0.3)}
EXC = (-0.5, 0.75)
FIELDS = ["title", "purpose", "tags", "subject", "outputs", "inputs", "family"]
SW = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
CAP_EXP = 8
ALPHA, ITERS, RRF_K, SEEDS = 0.85, 30, 60, 10


def dumps(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Index:
    def __init__(self, d):
        self.d = d
        self.man = jload(os.path.join(d, "manifest.json"))
        self.stats = jload(os.path.join(d, "stats.json"))
        self.vocab = jload(os.path.join(d, "vocabulary.json"))["tags"]
        self.docs = [json.loads(l) for l in open(os.path.join(d, "docs.jsonl"), encoding="utf-8") if l.strip()]
        g = jload(os.path.join(d, "graph.json"))
        self.graph = {int(k): v for k, v in g.items()}
        self.stop = set(self.stats["stopwords"])
        self.post = {}
        self._files = {}

    def toks(self, text):
        t = unicodedata.normalize("NFKC", text).casefold()
        t = re.sub(r"\\([a-zA-Z]+)", r" \1 ", t)
        return [w for w in re.split(r"[^0-9a-z]+", t) if len(w) > 1 and w not in self.stop]

    def posting(self, term):
        if term not in self.post:
            xx = term[:2]
            if xx not in self._files:
                self._files[xx] = sorted(f for f in os.listdir(os.path.join(self.d, "postings"))
                                         if re.fullmatch(re.escape(xx) + r"\d*\.json", f))
            self.post[term] = {}
            for f in self._files[xx]:
                c = self._cache(f)
                if term in c:
                    self.post[term] = c[term]
                    break
        return self.post[term]

    def _cache(self, f):
        k = "f:" + f
        if k not in self.post:
            self.post[k] = jload(os.path.join(self.d, "postings", f))
        return self.post[k]


def natkey(i):
    m = re.match(r"PM-(\d+)(?:\.(\d+))?$", i)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (10 ** 9, i)


def expand(ix, terms):
    ts, out = set(terms), []
    for t in ix.vocab:
        if ts & set(t["synonyms"]):
            for w in ix.toks(t["label"].replace("_", " ")):
                if w not in ts and w not in out:
                    out.append(w)
    return sorted(out)[:CAP_EXP]


def bm25f(ix, terms, exp, explain=False):
    st, N = ix.stats, ix.stats["N"]
    sc, why = {}, {}
    contrib = {}
    for t, mult, fields in [(t, 1.0, FIELDS) for t in terms] + [(t, 0.5, ["tags", "subject"]) for t in exp]:
        df = st["df"].get(t, 0)
        if not df:
            continue
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
        p = ix.posting(t)
        acc = {}
        for f in fields:
            w, b = WB[f]
            avg = st["avg_len"][f] or 1.0
            for d, tf in p.get(f, []):
                ln = ix.docs[d]["fields"][f + "_len"]
                acc[d] = acc.get(d, 0.0) + w * tf / (1 - b + b * ln / avg)
                why.setdefault(d, {}).setdefault(f, set()).add(t)
        for d in sorted(acc):
            c = mult * idf * acc[d] / (K1 + acc[d])
            sc[d] = sc.get(d, 0.0) + c
            if explain:
                contrib.setdefault(d, {})[t] = round(contrib.get(d, {}).get(t, 0.0) + c, 4)
        # exclusion penalty, after saturation
        if mult == 1.0:
            w, b = EXC
            avg = st["avg_len"].get("exclusions") or 1.0
            for d, tf in p.get("exclusions", []):
                ln = ix.docs[d]["fields"]["exclusions_len"]
                x = tf / (1 - b + b * ln / avg)
                sc[d] = sc.get(d, 0.0) + w * idf * x / (K1 + x)
    return {d: max(0.0, round(v, 9)) for d, v in sc.items()}, why, contrib


def tag_scores(ix, qterms):
    q = set(qterms)
    syn = {t["tag"]: set(t["synonyms"]) for t in ix.vocab}
    out = {}
    for i, d in enumerate(ix.docs):
        s = 0.0
        for tg in sorted(d["tags"]):
            words = set(re.split(r"[_/]+", tg)) | syn.get(tg, set())
            if q & words:
                s += d["tags"][tg]
        if s > 0:
            out[i] = round(s, 9)
    return out


def ppr(ix, seeds_sc):
    seeds = sorted(sorted(seeds_sc, key=lambda d: (-seeds_sc[d], d))[:SEEDS])
    tot = sum(seeds_sc[d] for d in seeds)
    if not seeds or tot <= 0:
        return {}
    r = {d: seeds_sc[d] / tot for d in seeds}
    x = dict(r)
    for _ in range(ITERS):
        nx, dang = {}, 0.0
        for d in sorted(x):
            nb = ix.graph.get(d, [])
            if nb:
                share = ALPHA * x[d] / len(nb)
                for m in nb:
                    nx[m] = nx.get(m, 0.0) + share
            else:
                dang += ALPHA * x[d]
        for d, v in r.items():
            nx[d] = nx.get(d, 0.0) + (1 - ALPHA) * v + dang * v
        x = nx
    return {d: v for d, v in x.items() if v > 0}


def ranks(sc, bm):
    order = sorted(sc, key=lambda d: (-round(sc[d], 9), -bm.get(d, 0.0), d))
    return {d: r + 1 for r, d in enumerate(order)}


def route(ix, q, k=5, budget=2000, channels=("bm25f", "tags", "graph"), no_expand=False, explain=False):
    terms = sorted(set(ix.toks(q)))
    exp = [] if no_expand else expand(ix, terms)
    bm, why, contrib = bm25f(ix, terms, exp, explain)
    bm = {d: v for d, v in bm.items() if v > 0}
    chans = {}
    if "bm25f" in channels:
        chans["bm25f"] = ranks(bm, bm)
    if "tags" in channels:
        chans["tags"] = ranks(tag_scores(ix, terms + exp), bm)
    if "graph" in channels:
        chans["graph"] = ranks(ppr(ix, bm), bm)
    fused = {}
    for c, rk in chans.items():
        for d, r in rk.items():
            fused[d] = fused.get(d, 0.0) + 1.0 / (RRF_K + r)
    fused = {d: round(v, 9) for d, v in fused.items()}
    order = sorted(fused, key=lambda d: (-fused[d], -bm.get(d, 0.0), natkey(ix.docs[d]["id"])))
    exact = []
    for m in re.finditer(r"PM-\d{3}(?:\.\d+)?", q):
        for i, d in enumerate(ix.docs):
            if (d["id"] == m.group(0) or d["family"] == m.group(0)) and i not in exact:
                exact.append(i)
    order = exact + [d for d in order if d not in exact]
    abstain = not any(ix.stats["df"].get(t, 0) > 0 for t in terms + exp)
    res, total = [], 0
    for d in order[:k]:
        dd = ix.docs[d]
        reasons = [{"field": "id", "terms": [dd["id"]]}] if d in exact else []
        reasons += [{"field": f, "terms": sorted(why[d][f])} for f in FIELDS if f in why.get(d, {})]
        if explain and d in contrib:
            reasons = reasons[:5] + [{"field": "contrib", "terms": ["%s:%.4f" % (t, v) for t, v in sorted(contrib[d].items())][:12]}]
        reasons = reasons[:6]
        r = {"rank": len(res) + 1, "id": dd["id"], "kind": dd["kind"], "score": "%.9f" % fused.get(d, 0.0),
             "channel_ranks": {c: chans[c].get(d) for c in sorted(chans)}, "reasons": reasons,
             "read": dd["read"], "cost_tokens": 0}
        c = 0
        for _ in range(6):
            n = len(dumps(dict(r, cost_tokens=c)).encode("utf-8"))
            if n == c:
                break
            c = n
        r["cost_tokens"] = c
        if total + c > budget and res:
            break
        total += c
        res.append(r)
    return {"schema": "ctx.route-answer.v1", "router": VERSION, "index_sha256": ix.man["index_sha256"],
            "query": {"text": q, "sha256": hashlib.sha256(q.encode("utf-8")).hexdigest(), "terms": terms, "expanded": exp},
            "params": {"k": k, "budget_tokens": budget, "channels": [c for c in ("bm25f", "tags", "graph") if c in chans]},
            "results": res, "abstain": abstain, "total_tokens": total}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--index")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--channels", default="bm25f,tags,graph")
    ap.add_argument("--no-expand", action="store_true")
    ap.add_argument("--explain", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    chans = tuple(c for c in a.channels.split(",") if c in ("bm25f", "tags", "graph"))
    if not chans or not 1 <= a.k <= 50:
        return 2
    d = a.index or os.path.join(a.root, "catalogue", "router")
    try:
        ix = Index(d)
    except (OSError, ValueError, KeyError):
        print("index missing or invalid", file=sys.stderr)
        return 3
    sys.stdout.write(dumps(route(ix, a.q, a.k, a.budget, chans, a.no_expand, a.explain)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
