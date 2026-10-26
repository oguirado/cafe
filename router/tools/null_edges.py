#!/usr/bin/env python3
"""Null test for the typed-edge channel (V2-05, T7): 200 degree-preserving rewired graphs, in memory only."""
import argparse, json, os, random, sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import route_v2 as R  # noqa: E402
import eval_v2 as E  # noqa: E402

SYM = {0, 5, 7}          # shared_technique, shared_constant, contradicts (stored in both directions)
NGRAPHS = 200
P_DEN = NGRAPHS + 1


def split_relations(ed):
    """Expanded {str(doc): [[t, rc]]} -> {rc: sorted list of pairs}; symmetric relations as (min, max)."""
    per = {}
    for s, lst in ed.items():
        for t, rc in lst:
            a, b = int(s), t
            if rc in SYM:
                a, b = min(a, b), max(a, b)
            per.setdefault(rc, set()).add((a, b))
    return {rc: sorted(v) for rc, v in sorted(per.items())}


def expand(per):
    ed = {}
    for rc, pairs in per.items():
        for a, b in pairs:
            ed.setdefault(str(a), set()).add((b, rc))
            if rc in SYM:
                ed.setdefault(str(b), set()).add((a, rc))
    return {k: [list(x) for x in sorted(v)] for k, v in ed.items()}


def rewire_relation(pairs, rc, rng):
    """10*|E| double-edge swap attempts preserving in/out (or total) degree; no self-loops or duplicates."""
    L = list(pairs)
    S = set(L)
    n = len(L)
    acc = 0
    if n < 2:
        return L, 0
    for _ in range(10 * n):
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j:
            continue
        (a, b), (c, d) = L[i], L[j]
        if rc in SYM and rng.random() < 0.5:
            c, d = d, c
        n1, n2 = (a, d), (c, b)
        if rc in SYM:
            n1, n2 = (min(n1), max(n1)), (min(n2), max(n2))
        if a == d or c == b or n1 == n2 or n1 in S or n2 in S:
            continue
        old1, old2 = L[i], L[j]
        S.discard(old1)
        S.discard(old2)
        S.add(n1)
        S.add(n2)
        L[i], L[j] = n1, n2
        acc += 1
    return sorted(L), acc


def rewire(per, seed):
    rng = random.Random(seed)
    out, acc = {}, 0
    for rc in sorted(per):
        out[rc], a = rewire_relation(per[rc], rc, rng)
        acc += a
    return out, acc


def p_value(count_ge):
    return count_ge + 1, P_DEN


def ppm(x):
    return int((x * 1000000) // 1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--bench", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    bench = [json.loads(l) for l in open(a.bench, encoding="utf-8") if l.strip()]
    err = E.guard_bench(a.bench, bench)
    if err:
        print(err, file=sys.stderr)
        return 4
    ix = R.Index(a.index)
    base = {"profile": "P0", "w_exp": 0, "w_phr": 0, "lcp5": 0, "w_dom": 0, "intent_boost": 0}
    j0, _ = E.objective_J(ix, bench, dict(base, w_edge=0))
    j1, _ = E.objective_J(ix, bench, dict(base, w_edge=250))
    delta = j1 - j0
    per = split_relations(ix.all_edges())
    nulls, swaps = [], 0
    for seed in range(NGRAPHS):
        rp, acc = rewire(per, seed)
        swaps += acc
        ix.edges_override = expand(rp)
        jn, _ = E.objective_J(ix, bench, dict(base, w_edge=250))
        nulls.append(jn - j0)
    ix.edges_override = None
    cnt = sum(1 for x in nulls if x >= delta)
    pn, pd = p_value(cnt)
    enabled = bool(delta > 0 and Fraction(pn, pd) < Fraction(5, 100))
    rec = {"schema": "ctx.null-edges.v1", "index_sha256": ix.ixid, "bench_sha256": E.sha_file(a.bench), "graphs": NGRAPHS, "seeds": [0, NGRAPHS - 1],
           "edges_per_relation": {R_: len(v) for R_, v in per.items()}, "swaps_accepted_total": swaps,
           "j_off_ppm": ppm(j0), "j_on_ppm": ppm(j1), "delta_j_ppm": ppm(delta), "null_delta_ppm": [ppm(x) for x in nulls],
           "count_ge": cnt, "p_num": pn, "p_den": pd, "enabled": enabled}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, sort_keys=True, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
