#!/usr/bin/env python3
"""Deterministic offline index builder for the surgical context router (v0)."""
import argparse, hashlib, json, os, re, shutil, sys, tempfile, unicodedata

STOP = set("""a about above after again against all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where which while who whom why will
with would you your via using use used given let thus hence one two may must shall within without upon per""".split())
W = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
FIELDS = ["title", "purpose", "tags", "subject", "outputs", "inputs", "family", "exclusions"]
CAP = 384 * 1024


def toks(text):
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"\\([a-zA-Z]+)", r" \1 ", t)
    return [w for w in re.split(r"[^0-9a-z]+", t) if len(w) > 1 and w not in STOP]


def strs(x):
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k in sorted(x) for s in strs(x[k])]
    if isinstance(x, list):
        return [s for v in x for s in strs(v)]
    return []


def idkey(i):
    m = re.match(r"PM-(\d+)(?:\.(\d+))?$", i)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (10 ** 9, i)


def dumps(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def wj(path, o):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(o) + "\n")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build(root, out, vocab_path):
    cat = os.path.join(root, "catalogue")
    rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(cat, "routes.tsv"), encoding="utf-8")][1:]
    rows = sorted((r for r in rows if r and r[0]), key=lambda r: idkey(r[0]))
    subj = {s["subject_id"]: s for s in load(os.path.join(cat, "subjects.json"))["subjects"]}
    meas = load(os.path.join(cat, "measurements.json"))
    pages = {}

    def page(p):
        if p not in pages:
            d = {}
            for line in open(os.path.join(root, p), encoding="utf-8"):
                if line.strip():
                    c = json.loads(line)
                    d[c["id"]] = c
            pages[p] = d
        return pages[p]

    docs, post, inline, detail = [], {}, 0, 0
    fam_members, fam_title = {}, {}
    for ix, (i, sid, paper, pg) in enumerate(rows):
        card = page(pg)[i]
        if card.get("schema") == "pm.connector-ref.v1":
            cpath = card["detail"]
            card = load(os.path.join(root, cpath))
            ctok = meas["details"][i]["tokens"]
            detail += 1
        else:
            cpath, ctok = pg, meas["pages"][pg]["tokens"]
            inline += 1
        s = subj[int(sid)]
        fam = card["family"]
        if fam not in fam_title:
            fam_title[fam] = load(os.path.join(cat, "families", fam + ".json"))["title"]
        fam_members.setdefault(fam, []).append(ix)
        tags = {"subject/s%d" % int(sid): 0.3}
        for d in card.get("domains", []):
            if d.get("applies"):
                w = W.get(d.get("strength"), 0.3)
                tags[d["domain"]] = max(tags.get(d["domain"], 0), w)
                if d.get("proposed_domain"):
                    pd = re.sub(r"[^a-z0-9_]+", "_", d["proposed_domain"].casefold()).strip("_")
                    if re.match(r"[a-z]", pd or "0"):
                        tags[pd] = max(tags.get(pd, 0), w)
        tf = {f: {} for f in FIELDS}
        ln = {}
        texts = {"title": card.get("title", ""), "purpose": card.get("purpose", ""),
                 "subject": s["subject"] + " " + " ".join(s["keywords"]),
                 "outputs": " ".join(o.get("exact_statement", "") for o in card.get("outputs", [])),
                 "inputs": " ".join(strs(card.get("inputs", {}))), "family": fam_title[fam],
                 "exclusions": " ".join(e.get("query", "") for e in card.get("exclusions", []))}
        for f, t in texts.items():
            ts = toks(t)
            ln[f] = len(ts)
            for w in ts:
                tf[f][w] = tf[f].get(w, 0) + 1
        ln["tags"] = 0
        for tg in sorted(tags):
            for w in toks(tg.replace("_", " ").replace("/", " ")):
                tf["tags"][w] = round(tf["tags"].get(w, 0) + tags[tg], 3)
                ln["tags"] += 1
        for f in FIELDS:
            for w, v in tf[f].items():
                post.setdefault(w, {}).setdefault(f, []).append([ix, v])
        outs = card.get("outputs", [])
        anc = outs[0].get("statement_anchor") if outs else None
        ptok = card.get("context", {}).get("tokens", 0)
        read = [{"path": paper, "anchor": a, "tokens": ptok} for a in ["pm-s0", anc, "pm-s5"] if a]
        docs.append({"id": i, "kind": "paper", "fields": {f + "_len": ln[f] for f in FIELDS}, "tags": tags, "family": fam,
                     "subject": int(sid), "status": card.get("status"), "read": read, "card": {"path": cpath, "tokens": ctok}})
    n = len(docs)
    avg = {f: round(sum(d["fields"][f + "_len"] for d in docs) / n, 6) for f in FIELDS}
    df = {w: len({x[0] for f in v for x in v[f]}) for w, v in post.items()}
    graph = {}
    edges = 0
    for fam, mem in fam_members.items():
        for a in mem:
            graph[a] = [b for b in mem if b != a]
        edges += len(mem) * (len(mem) - 1) // 2
    files = {}
    with open(os.path.join(out, "docs.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for d in docs:
            f.write(dumps(d) + "\n")
    byp = {}
    for w in sorted(post):
        byp.setdefault(w[:2], {})[w] = post[w]
    for xx, terms in sorted(byp.items()):
        chunks, cur, size = [], {}, 2
        for w in sorted(terms):
            sz = len(dumps({w: terms[w]}).encode("utf-8")) + 1
            if cur and size + sz > CAP - 16:
                chunks.append(cur)
                cur, size = {}, 2
            cur[w] = terms[w]
            size += sz
        chunks.append(cur)
        for k, c in enumerate(chunks):
            wj(os.path.join(out, "postings", xx + (str(k) if k else "") + ".json"), c)
    wj(os.path.join(out, "stats.json"), {"N": n, "avg_len": avg, "df": df, "stopwords": sorted(STOP)})
    wj(os.path.join(out, "graph.json"), {str(k): sorted(v) for k, v in sorted(graph.items())})
    wj(os.path.join(out, "vocabulary.json"), load(vocab_path))
    for dp, _, fs in os.walk(out):
        for fn in fs:
            p = os.path.join(dp, fn)
            files[os.path.relpath(p, out).replace(os.sep, "/")] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    files.pop("manifest.json", None)
    idx = hashlib.sha256("".join("%s\t%s\n" % (k, files[k]) for k in sorted(files)).encode()).hexdigest()
    wj(os.path.join(out, "manifest.json"), {"schema": "ctx.index-manifest.v1", "files": files, "index_sha256": idx,
                                           "nodes": n, "inline": inline, "detail": detail, "edges": edges})
    return idx


def same_tree(a, b):
    fa = sorted(os.path.relpath(os.path.join(d, f), a) for d, _, fs in os.walk(a) for f in fs)
    fb = sorted(os.path.relpath(os.path.join(d, f), b) for d, _, fs in os.walk(b) for f in fs)
    return fa == fb and all(open(os.path.join(a, f), "rb").read() == open(os.path.join(b, f), "rb").read() for f in fa)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--vocab", default="standards/vocabulary.json")
    ap.add_argument("--check", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not os.path.isfile(os.path.join(a.root, "catalogue", "routes.tsv")) or not os.path.isfile(a.vocab):
        print("input missing", file=sys.stderr)
        return 3
    if a.check:
        tmp = tempfile.mkdtemp(prefix="ctxidx")
        try:
            build(a.root, tmp, a.vocab)
            ok = os.path.isdir(a.out) and same_tree(tmp, a.out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print("byte-identical" if ok else "MISMATCH")
        return 0 if ok else 5
    if os.path.isdir(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    print(build(a.root, a.out, a.vocab))
    return 0


if __name__ == "__main__":
    sys.exit(main())
