#!/usr/bin/env python3
"""Deterministic offline index builder v2 (V2-01..V2-03, V2-05, V2-08..V2-10, V2-13)."""
import argparse, hashlib, json, os, re, shutil, sys, unicodedata
from decimal import Decimal, getcontext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blocks as BL  # noqa: E402

BUILDER = "build_index_v2.py/2.0"
CAP = 393216
getcontext().prec = 28

STOP = set("""a about above after again against all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where which while who whom why will
with would you your via using use used given let thus hence one two may must shall within without upon per""".split())
ZONES = ["title", "purpose", "trig", "task", "qa", "syn", "kw", "out", "pre", "subj", "fam", "inp"]
NEG = "neg"
BPPM = {"title": 300000, "purpose": 750000, "trig": 300000, "task": 750000, "qa": 750000, "syn": 300000, "kw": 300000,
        "out": 750000, "pre": 750000, "subj": 300000, "fam": 300000, "inp": 750000, "neg": 750000}
RELS = ["shared_technique", "uses", "generalises", "prerequisite", "specialises", "shared_constant", "supersedes", "contradicts"]
SOFT = {"software", "algorithms", "tools", "data_structures"}
PRIOR = {"strong": 1000, "moderate": 600, "weak": 300}
CONSUMED = {"pm.search", "pm.edges", "pm.domains"}


class BuildError(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s %s" % (code, detail))
        self.code = code


def toks(text):  # identical to v1 build_index.toks (tested)
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


def canon(o):
    return (dumps(o) + "\n").encode("utf-8")


def readb(p):
    with open(p, "rb") as f:
        return f.read()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def idf_ppm(N, df):
    df = max(df, 1)
    v = (Decimal(1) + (Decimal(N - df) + Decimal("0.5")) / (Decimal(df) + Decimal("0.5"))).ln()
    return int((v * 1000000).to_integral_value(rounding="ROUND_FLOOR"))


# ---- projections (V2-01 registry) ---------------------------------------------------------------
def proj_search_v1(b):
    g = lambda k: b.get(k) or []
    return {"tasks": [(t.get("task", ""), t.get("contract_anchor")) for t in g("engineering_tasks")],
            "syn": [[s.get("term", "")] + list(s.get("aliases") or []) for s in g("synonyms")],
            "kw": list(g("keywords")), "trig": list(g("retrieval_triggers")),
            "qa": [(q.get("question", ""), q.get("answer_anchor")) for q in g("questions_answered")],
            "pre": [[p.get("concept", "")] + list(p.get("requires_concepts") or []) for p in g("prerequisite_chains")],
            "neg": [e.get("query", "") for e in g("exclusions")]}


def proj_edges_v2(b):
    return [(e["target_pm_id"], e["relation"], e.get("direction")) for e in (b.get("edges") or [])]


def proj_domains_v2(b):
    pr = 0
    for d in b.get("domains") or []:
        if d.get("applies") and d.get("domain") in SOFT:
            pr = max(pr, PRIOR.get(d.get("strength"), 0))
    return pr


PROJ = {("pm.search", 1): proj_search_v1, ("pm.edges", 2): proj_edges_v2, ("pm.domains", 2): proj_domains_v2}


def project(parsed, path):
    out = {}
    for name in sorted(parsed):
        m = re.match(r"(pm\.[a-z_]+)\.v(\d+)$", name)
        if m and m.group(1) in CONSUMED:
            fn = PROJ.get((m.group(1), int(m.group(2))))
            if fn is None:
                raise BuildError("E_SCHEMA_UNKNOWN", "%s %s" % (path, name))
            out[m.group(1)] = fn(parsed[name])
    out.setdefault("pm.search", proj_search_v1({}))
    out.setdefault("pm.edges", [])
    out.setdefault("pm.domains", 0)
    return out


# ---- records ------------------------------------------------------------------------------------
class Rec:
    """One indexed node: zone token lists, phrases, edges, anchors, digest."""
    def __init__(self, id_, fam, status, prior):
        self.id, self.fam, self.status, self.prior = id_, fam, status, prior
        self.z = {z: [] for z in ZONES + [NEG]}
        self.phr = set()
        self.edges = []      # (target id, relation, direction)
        self.aq = []         # [anchor, "q"|"t", [terms]]
        self.oa = None
        self.an = None       # non-paper anchors [[a, role, intent?]]
        self.dg = {}


def add_zone(rec, zone, strings, phrases=False):
    for s in strings:
        t = toks(s)
        rec.z[zone].extend(t)
        if phrases and 2 <= len(t) <= 4:
            rec.phr.add(tuple(t))


def aq_terms(text):
    seen, out = set(), []
    for t in toks(text):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:24]


def status_mask(s):
    s = (s or "").upper()
    return 1 if s == "ACTIVE" else 2 if s == "SEALED" else 0


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_papers(root, out_sources, thes_sha=None):
    cat = os.path.join(root, "catalogue")
    rtxt = readb(os.path.join(cat, "routes.tsv"))
    out_sources["catalogue/routes.tsv"] = sha(rtxt)
    rows = [l.split("\t") for l in rtxt.decode("utf-8").split("\n")][1:]
    rows = sorted((r for r in rows if r and r[0]), key=lambda r: idkey(r[0]))
    sjb = readb(os.path.join(cat, "subjects.json"))
    out_sources["catalogue/subjects.json"] = sha(sjb)
    subj = {s["subject_id"]: s for s in json.loads(sjb)["subjects"]}
    pages, fam_title, recs = {}, {}, []
    for i, sid, paper, pg in rows:
        if pg not in pages:
            pb = readb(os.path.join(root, pg))
            out_sources[pg] = sha(pb)
            pages[pg] = {}
            for line in pb.decode("utf-8").split("\n"):
                if line.strip():
                    c = json.loads(line)
                    pages[pg][c["id"]] = c
        card = pages[pg][i]
        if card.get("schema") == "pm.connector-ref.v1":
            dp = card["detail"]
            db = readb(os.path.join(root, dp))
            out_sources[dp] = sha(db)
            card = json.loads(db)
        s = subj[int(sid)]
        fam = card["family"]
        if fam not in fam_title:
            fp = "catalogue/families/%s.json" % fam
            fb = readb(os.path.join(root, fp))
            out_sources[fp] = sha(fb)
            fam_title[fam] = json.loads(fb)["title"]
        ppath = os.path.join(root, paper)
        raw, anch = BL.read_raw(ppath)
        parsed = {}
        for name, r in raw.items():
            try:
                parsed[name] = json.loads(r)
            except ValueError as e:
                raise BuildError("E_BLOCK_JSON", "%s %s %s" % (paper, name, e))
        pj = project(parsed, paper)
        h = hashlib.sha256()
        for name in sorted(raw):
            if name.rsplit(".v", 1)[0] in CONSUMED:
                h.update(name.encode() + b"\0" + raw[name].encode("utf-8") + b"\n")
        out_sources[paper + "#pm.blocks"] = h.hexdigest()
        sb = pj["pm.search"]
        rec = Rec(i, fam, status_mask(card.get("status")), pj["pm.domains"])
        outs = card.get("outputs", [])
        add_zone(rec, "title", [card.get("title", "")])
        add_zone(rec, "purpose", [card.get("purpose", "")])
        add_zone(rec, "trig", [t.replace("-", " ") for t in sb["trig"]], True)
        add_zone(rec, "task", [t for t, _ in sb["tasks"]])
        add_zone(rec, "qa", [q for q, _ in sb["qa"]])
        for grp in sb["syn"]:
            add_zone(rec, "syn", grp, True)
        add_zone(rec, "kw", sb["kw"])
        add_zone(rec, "out", [o.get("exact_statement", "") for o in outs])
        for grp in sb["pre"]:
            add_zone(rec, "pre", grp)
        add_zone(rec, "subj", [s["subject"] + " " + " ".join(s["keywords"])])
        add_zone(rec, "fam", [fam_title[fam]])
        add_zone(rec, "inp", strs(card.get("inputs", {})))
        add_zone(rec, NEG, [e.get("query", "") for e in card.get("exclusions", [])] + sb["neg"])
        rec.edges = pj["pm.edges"]
        aq = [[a, "q", aq_terms(q)] for q, a in sb["qa"] if a in anch] + [[a, "t", aq_terms(t)] for t, a in sb["tasks"] if a in anch]
        rec.aq = aq[:16]
        oa = outs[0].get("statement_anchor") if outs else None
        rec.oa = oa if oa in anch else None
        rec.dg = {"t": card.get("title", ""), "p": " ".join(card.get("purpose", "").split())[:300],
                  "o": [" ".join(o.get("exact_statement", "").split())[:200] for o in outs[:2]],
                  "g": [t for t in sb["trig"]][:6]}
        recs.append(rec)
    return recs


def read_nodes(path, out_sources):
    raw = readb(path)
    out_sources["nodes/" + os.path.basename(path)] = sha(raw)
    nodes = [json.loads(l) for l in raw.decode("utf-8").split("\n") if l.strip()]
    nodes.sort(key=lambda n: n["id"])
    recs = []
    for n in nodes:
        st = {"active": 1, "sealed": 2}.get(n["status"], 0)
        rec = Rec(n["id"], n.get("family", n["id"]), st, 0)
        dp = 0
        for d in n.get("domains", []):
            if d.get("domain") in SOFT:
                dp = max(dp, PRIOR.get(d.get("strength"), 0))
        rec.prior = dp
        s = n["search"]
        add_zone(rec, "title", [n["title"]])
        add_zone(rec, "purpose", [n["purpose"]])
        add_zone(rec, "trig", [t.replace("-", " ") for t in s["triggers"]], True)
        add_zone(rec, "task", s.get("tasks", []))
        add_zone(rec, "qa", s.get("questions", []))
        add_zone(rec, "syn", s.get("problem_names", []), True)
        add_zone(rec, "kw", s["keywords"])
        add_zone(rec, NEG, s.get("exclusions", []))
        rec.an = [[a["a"], a["role"]] + ([a["intent"]] if a.get("intent") else []) for a in n["anchors"]]
        rec.oa = n["anchors"][0]["a"]
        rec.dg = {"t": n["title"], "p": n["purpose"][:300], "o": [], "g": s["triggers"][:6]}
        recs.append(rec)
    return recs


# ---- writing ------------------------------------------------------------------------------------
def split_map(m, cap):
    """Split sorted-key dict into chunks whose canonical bytes <= cap - 16."""
    chunks, cur, size = [], {}, 3
    for k in sorted(m):
        sz = len((dumps(k) + ":" + dumps(m[k])).encode("utf-8")) + 1
        if cur and size + sz > cap - 16:
            chunks.append(cur)
            cur, size = {}, 3
        cur[k] = m[k]
        size += sz
    chunks.append(cur)
    return chunks


def put_map(files, shards, name, m, cap):
    ch = split_map(m, cap)
    if len(ch) == 1:
        files[name + ".json"] = canon(ch[0])
        return
    shards[name] = []
    for k, c in enumerate(ch):
        fn = "%s.%d.json" % (name, k)
        files[fn] = canon(c)
        shards[name].append([sorted(c)[0], fn])


def build_files(recs, sources, thesaurus, corpus, cap, edges_typed_raw):
    N = len(recs)
    pos = {r.id: i for i, r in enumerate(recs)}
    zones = ZONES + [NEG]
    lens = {z: [len(r.z[z]) for r in recs] for z in zones}
    A = {z: (1000 * sum(lens[z])) // N if N else 0 for z in zones}
    tf = {}   # term -> zone -> [(doc, tf)]
    for d, r in enumerate(recs):
        for z in zones:
            c = {}
            for t in r.z[z]:
                c[t] = c.get(t, 0) + 1
            for t in sorted(c):
                tf.setdefault(t, {}).setdefault(z, []).append((d, c[t]))
    post, byprefix = {}, {}
    posdocs = {}
    for t in sorted(tf):
        docs = {d for z in ZONES for d, _ in tf[t].get(z, [])}
        posdocs[t] = docs
        post[t] = {"d": len(docs), "i": idf_ppm(N, len(docs)),
                   "z": {z: [x for dt in tf[t][z] for x in dt] for z in zones if z in tf[t]}}
    files, shards = {}, {}
    for t, v in post.items():
        byprefix.setdefault(t[:2], {})[t] = v
    for xx in sorted(byprefix):
        put_map(files, shards, "post/" + xx, byprefix[xx], cap)
    phr = {}
    for d, r in enumerate(recs):
        for p in r.phr:
            phr.setdefault(" ".join(p), set()).add(d)
    byp = {}
    for k in sorted(phr):
        byp.setdefault(k[:2], {})[k] = sorted(phr[k])
    for xx in sorted(byp):
        put_map(files, shards, "phr/" + xx, byp[xx], cap)
    cls = {}
    for t in sorted(post):
        if len(t) >= 5 and post[t]["d"] > 0:
            cls.setdefault(t[:5], []).append(t)
    pfx = {}
    for p5, ts in cls.items():
        u = set()
        for t in ts:
            u |= posdocs[t]
        pfx[p5] = {"d": len(u), "i": idf_ppm(N, len(u)), "t": ts}
    if pfx:
        put_map(files, shards, "pfx", pfx, cap)
    famn = sorted({r.fam for r in recs})
    edges, contra = {}, {}
    for d, r in enumerate(recs):
        for tgt, rel, direction in r.edges:
            if tgt not in pos or rel not in RELS:
                continue
            t, rc = pos[tgt], RELS.index(rel)
            pairs = []
            if rel == "supersedes":
                pairs.append((t, d))
            elif direction == "symmetric":
                pairs += [(d, t), (t, d)]
            else:
                pairs.append((d, t))
            for a, b in pairs:
                edges.setdefault(str(a), set()).add((b, rc))
                if rc == 7:
                    contra.setdefault(str(a), set()).add(b)
    put_map(files, shards, "edges", {k: [list(x) for x in sorted(v)] for k, v in edges.items()}, cap)
    cols = {"A": A, "idf1": idf_ppm(N, 1), "B": {z: BPPM[z] for z in zones}, "N": N, "contra": {k: sorted(v) for k, v in contra.items()},
            "fam": [famn.index(r.fam) for r in recs], "famn": famn, "id": [r.id for r in recs],
            "len": lens, "prior": [r.prior for r in recs], "shards": shards, "status": [r.status for r in recs],
            "zones": zones}
    files["cols.json"] = canon(cols)
    files["thes.json"] = canon(thesaurus)
    for s in range((N + 255) // 256):
        part = recs[s * 256:(s + 1) * 256]
        dl, gl = [], []
        for k, r in enumerate(part):
            i = s * 256 + k
            row = {"aq": r.aq, "fam": cols["fam"][i], "i": i, "id": r.id, "oa": r.oa, "st": r.status}
            if r.an is not None:
                row["an"] = r.an
            dl.append(dumps(row))
            gl.append(dumps(dict(r.dg, i=i, id=r.id)))
        files["docs/%d.jsonl" % s] = ("\n".join(dl) + "\n").encode("utf-8")
        files["digest/%d.jsonl" % s] = ("\n".join(gl) + "\n").encode("utf-8")
    h = "".join("%s\t%s\n" % (k, sha(files[k])) for k in sorted(files))
    idx = sha(h.encode())
    man = {"schema": "ctx.index-manifest.v2", "corpus": corpus, "files": {k: sha(files[k]) for k in sorted(files)},
           "sources": {k: sources[k] for k in sorted(sources)}, "index_sha256": idx, "nodes": N, "zones": zones,
           "edges_typed": edges_typed_raw, "phrases": len(phr), "max_file_bytes": max(len(v) for v in files.values()),
           "builder": BUILDER}
    files["manifest.json"] = canon(man)
    files["ID"] = (idx + "\n").encode()
    return files, idx


def load_thesaurus(path):
    if not path or not os.path.isfile(path):
        return {"v": 0, "e": []}, sha(b"")
    raw = readb(path)
    t = json.loads(raw)
    ents = []
    for e in t["entries"]:
        wh = [toks(w) for w in e["when"]]
        wh = [w for w in wh if w]
        to = []
        for w in e["to"]:
            for x in toks(w):
                if x not in to:
                    to.append(x)
        ents.append([e["id"], wh, to, 1 if e.get("single_ok") else 0])
    return {"v": t.get("version", 1), "e": ents}, sha(raw)


def build(root, out, thes_path, cap=CAP, nodes=None, corpus="math"):
    sources = {}
    th, thsha = load_thesaurus(thes_path)
    sources["standards/thesaurus.json"] = thsha
    sources["tools/build_index_v2.py"] = sha(readb(os.path.abspath(__file__)))
    if nodes:
        recs = read_nodes(nodes, sources)
        raw_edges = 0
    else:
        recs = read_papers(root, sources)
        raw_edges = sum(len(r.edges) for r in recs)
    files, idx = build_files(recs, sources, th, corpus, cap, raw_edges)
    tmp = out.rstrip("/") + ".tmp"
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    for k in sorted(files):
        p = os.path.join(tmp, k)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(files[k])
    if os.path.isdir(out):
        old = out.rstrip("/") + ".old"
        if os.path.isdir(old):
            shutil.rmtree(old)
        os.replace(out, old)
        os.replace(tmp, out)
        shutil.rmtree(old)
    else:
        os.replace(tmp, out)
    return idx


def tree(d):
    return {os.path.relpath(os.path.join(a, f), d): readb(os.path.join(a, f)) for a, _, fs in os.walk(d) for f in fs}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--thesaurus", default="standards/thesaurus.json")
    ap.add_argument("--nodes")
    ap.add_argument("--corpus", default="math")
    ap.add_argument("--cap", type=int, default=CAP)
    ap.add_argument("--check", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not a.nodes and not (a.root and os.path.isfile(os.path.join(a.root, "catalogue", "routes.tsv"))):
        print("input missing", file=sys.stderr)
        return 3
    try:
        if a.check:
            chk = a.out.rstrip("/") + ".chk"
            build(a.root, chk, a.thesaurus, a.cap, a.nodes, a.corpus)
            try:
                ok = os.path.isdir(a.out) and tree(chk) == tree(a.out)
            finally:
                shutil.rmtree(chk, ignore_errors=True)
            print("byte-identical" if ok else "MISMATCH")
            return 0 if ok else 5
        print(build(a.root, a.out, a.thesaurus, a.cap, a.nodes, a.corpus))
    except BuildError as e:
        print(e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
