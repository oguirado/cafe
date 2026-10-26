#!/usr/bin/env python3
"""Labelling pool and blind candidate sheets (router top-20 + B1 + B2, ordered by id)."""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route as R  # noqa: E402
import build_index as B  # noqa: E402
import eval_route as E  # noqa: E402


def card(root, ix, i, cache):
    p = ix.docs[i]["card"]["path"]
    if p.startswith("catalogue/details/"):
        return B.load(os.path.join(root, p))
    if p not in cache:
        cache[p] = {}
        for line in open(os.path.join(root, p), encoding="utf-8"):
            if line.strip():
                c = json.loads(line)
                cache[p][c["id"]] = c
    return cache[p][ix.docs[i]["id"]]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--bench", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    ix = R.Index(a.index)
    subj = B.load(os.path.join(a.root, "catalogue", "subjects.json"))["subjects"]
    os.makedirs(a.out, exist_ok=True)
    pool, cache = {}, {}
    for l in open(a.bench, encoding="utf-8"):
        b = json.loads(l)
        ids = set()
        for s in ("router", "b1", "b2"):
            ids |= set(E.run_system(ix, subj, s, b["text"], 20, E.BIG)[0])
        ids = sorted(ids, key=R.natkey)
        pool[b["qid"]] = ids
        pos = {d["id"]: i for i, d in enumerate(ix.docs)}
        md = ["# Candidate sheet %s\n\nTask: %s\n" % (b["qid"], b["text"])]
        for i in ids:
            c = card(a.root, ix, pos[i], cache)
            tags = sorted({d["domain"] for d in c.get("domains", []) if d.get("applies")} |
                          {d["proposed_domain"] for d in c.get("domains", []) if d.get("applies") and d.get("proposed_domain")})
            md.append("## %s\n- title: %s\n- purpose: %s\n- domains: %s" % (i, c.get("title", ""), c.get("purpose", ""), ", ".join(tags) or "-"))
            for o in c.get("outputs", [])[:3]:
                md.append("- statement: " + " ".join(o.get("exact_statement", "").split())[:300])
            md.append("")
        with open(os.path.join(a.out, "sheet_%s.md" % b["qid"]), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(md))
    with open(os.path.join(a.out, "pool.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(pool, sort_keys=True, indent=1) + "\n")
    print(sum(len(v) for v in pool.values()) / len(pool))
    return 0


if __name__ == "__main__":
    sys.exit(main())
