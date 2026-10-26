#!/usr/bin/env python3
"""DEV label pool and blind candidate sheets (T5 preparation). No grading happens here."""
import argparse, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(W, "v1_frozen")]
import route_v2 as R  # noqa: E402
import route as V1R  # noqa: E402
import eval_route as V1E  # noqa: E402
import eval_v2 as E  # noqa: E402

BIG = 10 ** 9
BATCH_BYTES = 90 * 1024 - 6 * 1024     # headroom under the 90 KB brief cap for the instructions
V2_SYSTEMS = [("v2_p0_off", {"profile": "P0"}),
              ("v2_p1_exp_phr_lcp5", {"profile": "P1", "w_exp": 500, "w_phr": 500, "lcp5": 1}),
              ("v2_p2_dom_intent", {"profile": "P2", "w_dom": 200, "intent_boost": 1})]
CLASSES = ["ALG", "DS", "GFX", "ML", "NUM", "MEM", "SYS"]


def read_sources(paths):
    """Return [{qid, text, source, source_ref, labelled, row}] in the order of the source files."""
    out = []
    for p in paths:
        base = os.path.basename(p)
        if p.endswith(".jsonl"):
            with open(p, encoding="utf-8") as f:
                for l in f:
                    if l.strip():
                        r = json.loads(l)
                        out.append({"qid": r["qid"], "text": r["text"], "source": r.get("source", "authored_blind"), "source_ref": r["qid"],
                                    "labelled": True, "row": r, "topic": r.get("class")})
        else:
            with open(p, encoding="utf-8") as f:
                for l in f:
                    if not l.strip():
                        continue
                    i, topic, text = l.rstrip("\n").split("\t")[:3]
                    out.append({"qid": "W-" + i, "text": text, "source": "w11_brainstorm", "source_ref": base + ":" + i,
                                "labelled": False, "row": None, "topic": topic})
    seen = set()
    for o in out:
        if o["qid"] in seen:
            raise ValueError("duplicate qid " + o["qid"])
        seen.add(o["qid"])
    return out


def systems_top(ix, v1ix, text):
    res = {}
    res["v1"] = [r["id"] for r in V1R.route(v1ix, text, 20, BIG)["results"]]
    res["b1"] = V1E.run_system(v1ix, [], "b1", text, 20, BIG)[0]
    for name, params in V2_SYSTEMS:
        res[name] = [r[0] for r in R.route(ix, text, params, k=20, budget=BIG)["r"]]
    return res


def digest_map(ix):
    out = {}
    for s in range((ix.N + 255) // 256):
        with open(os.path.join(ix.d, "digest", "%d.jsonl" % s), encoding="utf-8") as f:
            for l in f:
                if l.strip():
                    r = json.loads(l)
                    out[r["id"]] = r
    return out


def sheet_text(qid, text, ids, dg):
    md = ["# Candidate sheet %s\n\nTask: %s\n" % (qid, text)]
    for i in ids:
        d = dg[i]
        md.append("## %s\n- title: %s\n- purpose: %s" % (i, d["t"], d["p"]))
        for o in d["o"]:
            md.append("- statement: " + o)
        md.append("- triggers: " + "; ".join(t.replace("-", " ") for t in d["g"]) if d["g"] else "- triggers: -")
        md.append("")
    return "\n".join(md)


INSTR = """# Annotator instructions

You receive {n} candidate sheet(s) (`sheet_<qid>`), one per task query. Each lists candidate documents with title, purpose, up to two
exact statements and up to six retrieval triggers. Work independently; do not consult any other annotator.

Grade every candidate for its query on this scale:

| Grade | Meaning |
|---|---|
| 0 | Not relevant to the task. |
| 1 | Related background: same area, would not by itself help solve the task. |
| 2 | Useful: contains a result, method or bound that a practitioner could apply to the task. |
| 3 | Directly solves the task as stated. |

Rules:
- Judge only from the sheet text; the document may be abstract mathematics, so grade the usable content, not the field label.
- If nothing in the pool is useful (all grades 0-1), that is a valid outcome; do not inflate grades.
- You may propose additional ids found by your own search; list them under the query with a grade.
- Do not guess which system proposed a candidate; order carries no information.
- Assign every query one class from: ALG (algorithms), DS (data structures), GFX (graphics and geometry), ML (machine learning and statistics),
  NUM (numerical computing), MEM (agent memory, retrieval and context management), SYS (systems, concurrency and networking).

Output: one JSON object with two keys and nothing else:
`{{"grades": {{qid: {{id: grade}}}}, "class": {{qid: "ALG"}}}}`
covering every query of this brief and every pooled id (plus your proposed ids), for example
`{{"grades": {{"Q-ALG-01": {{"PM-003.2": 1, "PM-010.1": 0}}}}, "class": {{"Q-ALG-01": "ALG"}}}}`. Output the JSON only.
"""


def prep(a):
    srcs = read_sources(a.dev_src)
    ix, v1ix = R.Index(a.index), V1R.Index(a.v1_index)
    dg = digest_map(ix)
    os.makedirs(a.out, exist_ok=True)
    pool, systems = {}, {}
    sheets = {}
    for s in srcs:
        tops = systems_top(ix, v1ix, s["text"])
        union = set()
        for v in tops.values():
            union |= set(v)
        graded = E.judged_ids(s["row"]) if s["row"] else set()
        cand = sorted(union - graded, key=lambda i: ix.ids.index(i))
        pool[s["qid"]] = {"text": s["text"], "source": s["source"], "source_ref": s["source_ref"], "topic": s["topic"], "labelled": s["labelled"],
                          "n_union": len(union), "n_already_graded": len(union & graded), "candidates": cand}
        systems[s["qid"]] = {k: v for k, v in sorted(tops.items())}
        if cand:
            sheets[s["qid"]] = sheet_text(s["qid"], s["text"], cand, dg)
            with open(os.path.join(a.out, "sheet_%s.md" % s["qid"]), "w", encoding="utf-8", newline="\n") as f:
                f.write(sheets[s["qid"]] + "\n")
    # batches: whole sheets only, <= BATCH_BYTES each
    batches, cur, size = [], [], 0
    for qid in sorted(sheets):
        n = len(sheets[qid].encode("utf-8")) + 2
        if cur and size + n > BATCH_BYTES:
            batches.append(cur)
            cur, size = [], 0
        cur.append(qid)
        size += n
    if cur:
        batches.append(cur)
    bdir = os.path.join(a.out, "batches")
    os.makedirs(bdir, exist_ok=True)
    manifest = []
    for k, qs in enumerate(batches):
        name = "batch_%02d" % k
        body = INSTR.format(n=len(qs)) + "\n---\n\n" + "\n\n---\n\n".join(sheets[q] for q in qs) + "\n"
        with open(os.path.join(bdir, name + ".md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        manifest.append({"batch": name, "qids": qs, "bytes": len(body.encode("utf-8"))})
    with open(os.path.join(a.out, "ANNOTATOR_INSTRUCTIONS.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(INSTR.format(n="all"))
    meta = {"schema": "ctx.pool.v1", "index_sha256": ix.ixid, "v1_index_sha256": v1ix.man["index_sha256"],
            "systems": ["v1", "b1"] + [n for n, _ in V2_SYSTEMS], "queries": pool,
            "totals": {"queries": len(pool), "labelled": sum(1 for q in pool.values() if q["labelled"]),
                       "candidates": sum(len(q["candidates"]) for q in pool.values()), "sheets": len(sheets), "batches": len(batches)}}
    for name, obj in (("pool.json", meta), ("pool_systems.json", systems), ("batches.json", manifest)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return meta["totals"], max(m["bytes"] for m in manifest) if manifest else 0


# ---- T5 merge: two seats (+ optional third) -> eval/dev.jsonl, agreement.json, disagreements.json -------------------
_ALIAS = re.compile(r"^(?:Q-)?(?:W-)?([RHM]\d+)$")


def norm_qid(q, pool):
    """Seats sometimes dropped/added prefixes (`Q-W-H09`, `H15`, `Q-M09`, `R13`); map only when the result is a pooled qid."""
    if q in pool:
        return q
    m = _ALIAS.match(q)
    if m and "W-" + m.group(1) in pool:
        return "W-" + m.group(1)
    return None


def load_seat(labels, seat, pool):
    """Merge `<seat>_batch_NN.json` files -> (grades {qid:{id:int}}, class {qid:str}, aliased, unknown_qids, conflicts)."""
    pat = re.compile(r"^%s_batch_\d\d\.json$" % seat)
    grades, cls, aliased, unknown, dup = {}, {}, [], [], []
    for fn in sorted(os.listdir(labels)):
        if not pat.match(fn):
            continue
        with open(os.path.join(labels, fn), encoding="utf-8") as f:
            d = json.load(f)
        for q, g in d.get("grades", {}).items():
            nq = norm_qid(q, pool)
            if nq is None:
                unknown.append(q)
                continue
            if nq != q:
                aliased.append(q + "->" + nq)
            if nq in grades:
                dup.append(nq)
            grades.setdefault(nq, {}).update({i: int(v) for i, v in g.items()})
        for q, c in d.get("class", {}).items():
            nq = norm_qid(q, pool)
            if nq is not None:
                cls[nq] = c
    return grades, cls, aliased, unknown, dup


def kappa(pairs):
    """Cohen kappa for two binary raters over [(a, b)]; None when undefined."""
    n = len(pairs)
    if not n:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 else round((po - pe) / (1 - pe), 4)


def final_grade(g1, g2, g3):
    """third seat if present, else the agreed grade / min of the two when both >= 2 (binarised agreement -> min); None = needs a third seat."""
    if g3 is not None:
        return g3
    if g1 is None or g2 is None:
        return None
    return min(g1, g2) if (g1 >= 2) == (g2 >= 2) else None


def load_a3(path):
    if not os.path.isfile(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("grades", d if "class" not in d else {}), d.get("class", {})


def merge(a):
    with open(os.path.join(a.pool, "pool.json"), encoding="utf-8") as f:
        pmeta = json.load(f)
    pool = pmeta["queries"]
    ix = R.Index(a.index)
    known = set(digest_map(ix))
    dg = digest_map(ix)
    g1, c1, al1, un1, du1 = load_seat(a.labels, "a1", pool)
    g2, c2, al2, un2, du2 = load_seat(a.labels, "a2", pool)
    a3g, a3c = load_a3(os.path.join(a.labels, "a3_disagreements.json"))
    with open(a.dev_base, encoding="utf-8") as f:
        base = [json.loads(l) for l in f if l.strip()]
    base_by = {b["qid"]: b for b in base}
    pairs, disagree, missing, unconfirmed, invalid = [], [], [], [], []
    final = {}           # qid -> {id: grade}
    resolved = {}        # qid -> set of resolution tags
    hist = {"a1": {}, "a2": {}}
    for q in sorted(pool):
        cand = pool[q]["candidates"]
        x1, x2 = g1.get(q, {}), g2.get(q, {})
        for s, x in (("a1", x1), ("a2", x2)):
            for v in x.values():
                hist[s][str(v)] = hist[s].get(str(v), 0) + 1
        ids = list(cand) + sorted((set(x1) | set(x2)) - set(cand))
        for i in ids:
            if i not in known:
                invalid.append([q, i])
                continue
            v1, v2 = x1.get(i), x2.get(i)
            v3 = a3g.get(q, {}).get(i)
            if i in cand and (v1 is None or v2 is None):
                missing.append([q, i, v1, v2])
            if i not in cand:
                # free-search proposal: a pair graded by one seat only is unconfirmed; >= 2 goes to the third seat
                if (v1 is None) != (v2 is None) and max(v1 or 0, v2 or 0) >= 2 and v3 is None:
                    unconfirmed.append([q, i, v1, v2])
                    disagree.append([q, i, v1, v2, "proposal"])
                    continue
            if v1 is not None and v2 is not None:
                if i in cand:
                    pairs.append((int(v1 >= 2), int(v2 >= 2)))
                if (v1 >= 2) != (v2 >= 2):
                    disagree.append([q, i, v1, v2, "grade"])
            fg = final_grade(v1, v2, v3)
            if fg is None:
                fg = min(x for x in (v1, v2) if x is not None) if v3 is None and (v1 is not None or v2 is not None) else 0
                resolved.setdefault(q, set()).add("pending_a3" if v1 is not None and v2 is not None else "single_seat")
            elif v3 is not None:
                resolved.setdefault(q, set()).add("a3")
            else:
                resolved.setdefault(q, set()).add("agreement")
            final.setdefault(q, {})[i] = fg
    # classes: v1 rows keep theirs; others = graders' majority, tie -> third seat, else pending
    cls, ctie = {}, []
    for q in sorted(pool):
        if q in base_by:
            cls[q] = base_by[q].get("class")
            continue
        c = [x for x in (c1.get(q), c2.get(q)) if x in CLASSES]
        if q in a3c and a3c[q] in CLASSES:
            cls[q] = a3c[q]
        elif len(c) == 2 and c[0] == c[1]:
            cls[q] = c[0]
        else:
            ctie.append([q, c1.get(q), c2.get(q)])
            cls[q] = (c[0] if c else "ALG")
    # rows
    rows = []
    for q in [b["qid"] for b in base] + sorted(set(pool) - set(base_by)):
        fin = final.get(q, {})
        if q in base_by:
            r = json.loads(json.dumps(base_by[q]))          # v1 labels kept as given; new pairs are added
            have = {g["id"]: g["grade"] for g in r["gold"]}
            for i, v in fin.items():
                if i not in have and v >= 1:
                    r["gold"].append({"id": i, "grade": v})
            adj = r["adjudication"]
        else:
            r = {"qid": q, "text": pool[q]["text"], "schema": "ctx.bench.v1", "source": pool[q]["source"], "split": "DEV",
                 "source_ref": pool[q]["source_ref"], "task": {}, "class": cls[q], "gold": [{"id": i, "grade": v} for i, v in fin.items() if v >= 1]}
            adj = r["adjudication"] = {"a1": {}, "a2": {}, "kappa_unit": "pooled_pair"}
        adj["a1"].update(g1.get(q, {}))
        adj["a2"].update(g2.get(q, {}))
        tags = sorted(resolved.get(q, {"agreement"}))
        adj["resolved_by"] = "+".join(tags) if q not in base_by or resolved.get(q) else adj.get("resolved_by", "agreement")
        r["gold"] = sorted(r["gold"], key=lambda g: (-g["grade"], g["id"]))
        r["answerable"] = any(g["grade"] >= 2 for g in r["gold"])
        rows.append(r)
    n = len(pairs)
    po = round(sum(1 for x, y in pairs if x == y) / n, 4) if n else None
    percls = {}
    for r in rows:
        d = percls.setdefault(r["class"], {"answerable": 0, "unanswerable": 0})
        d["answerable" if r["answerable"] else "unanswerable"] += 1
    ans = sum(1 for r in rows if r["answerable"])
    pending = sorted({q for q, t in resolved.items() if "pending_a3" in t})
    agree = {"schema": "ctx.t5-agreement.v1", "index_sha256": ix.ixid, "pool_sha256": hashlib.sha256(open(os.path.join(a.pool, "pool.json"), "rb").read()).hexdigest(),
             "kappa_unit": "pooled_pair", "binarised_at": 2, "pairs": n, "raw_agreement": po, "cohen_kappa": kappa(pairs),
             "both_positive": sum(1 for x, y in pairs if x and y), "a1_only_positive": sum(1 for x, y in pairs if x and not y),
             "a2_only_positive": sum(1 for x, y in pairs if y and not x), "both_negative": sum(1 for x, y in pairs if not x and not y),
             "grade_histogram": hist, "disagreements": len(disagree), "disagreements_grade": sum(1 for d in disagree if d[4] == "grade"),
             "unconfirmed_proposals": len(unconfirmed), "missing_pairs": len(missing), "invalid_ids": len(invalid),
             "qid_aliases": sorted(al1 + al2), "unknown_qids": sorted(set(un1 + un2)), "class_ties": ctie,
             "a3_present": bool(a3g), "queries": len(rows), "answerable": ans, "unanswerable": len(rows) - ans, "per_class": percls,
             "queries_pending_a3": pending, "thin_dev": ans < 20 or len(rows) - ans < 20}
    return rows, agree, disagree, missing, unconfirmed, dg, cls


def write_json(p, obj):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")


def run_merge(a):
    rows, agree, disagree, missing, _unc, dg, _cls = merge(a)
    with open(a.out_dev, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    write_json(a.agreement, agree)
    need = len(disagree) + len(agree["class_ties"]) if not agree["a3_present"] else 0
    if not agree["a3_present"] and (disagree or agree["class_ties"]):
        items = []
        texts = {}
        with open(os.path.join(a.pool, "pool.json"), encoding="utf-8") as f:
            texts = {q: v["text"] for q, v in json.load(f)["queries"].items()}
        for q, i, v1, v2, kind in disagree:
            d = dg.get(i, {})
            items.append({"qid": q, "id": i, "kind": kind, "a1": v1, "a2": v2, "query": texts[q], "title": d.get("t"), "purpose": d.get("p"),
                          "statements": d.get("o", []), "triggers": d.get("g", [])})
        write_json(a.disagreements, {"schema": "ctx.t5-disagreements.v1", "n_pairs": len(disagree), "pairs": items,
                                     "class_ties": [{"qid": q, "a1": x, "a2": y, "query": texts[q]} for q, x, y in agree["class_ties"]],
                                     "answer_format": "eval/labels/a3_disagreements.json = {\"grades\": {qid: {id: 0-3}}, \"class\": {qid: CLASS}}"})
    print(json.dumps({k: agree[k] for k in ("pairs", "raw_agreement", "cohen_kappa", "disagreements", "answerable", "unanswerable", "thin_dev",
                                             "a3_present", "missing_pairs", "invalid_ids")}, sort_keys=True))
    return need if not agree["a3_present"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--dev-src", action="append")
    ap.add_argument("--index", required=True)
    ap.add_argument("--v1-index")
    ap.add_argument("--out")
    ap.add_argument("--pool", default=os.path.join(W, "eval", "pool"))
    ap.add_argument("--labels", default=os.path.join(W, "eval", "labels"))
    ap.add_argument("--dev-base", default=os.path.join(W, "eval", "dev30.jsonl"))
    ap.add_argument("--out-dev", default=os.path.join(W, "eval", "dev.jsonl"))
    ap.add_argument("--agreement", default=os.path.join(W, "eval", "agreement.json"))
    ap.add_argument("--disagreements", default=os.path.join(W, "eval", "disagreements.json"))
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    paths = (a.dev_src or []) + [a.pool, a.labels, a.dev_base, a.out_dev, a.index]
    for p in paths:
        if "/bench/" in os.path.abspath(p) + "/":
            print("E_BLIND: /bench/ path", file=sys.stderr)
            return 4
    if a.merge:
        need = run_merge(a)
        return 0 if not need else 10
    if not (a.dev_src and a.v1_index and a.out):
        return 2
    totals, mx = prep(a)
    print(json.dumps(totals, sort_keys=True), "max_batch_bytes=%d" % mx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
