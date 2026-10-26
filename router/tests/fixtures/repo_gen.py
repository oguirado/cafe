"""Fixture repository generator (stdlib). make_repo(root, ...) writes a mini pm repository."""
import json, os, random

HAND = [
    dict(id="PM-001.1", fam="PM-001", sid=1, status="ACTIVE", title="Online paging with cache eviction",
         purpose="Competitive ratio of online paging algorithms for a bounded cache.",
         trig=["cache-eviction", "online-paging", "competitive-ratio"], kw=["paging", "k-server"],
         tasks=["Design an eviction policy for a bounded cache"], qa=["What is the competitive ratio of LRU paging?"],
         syn=[("paging problem", ["page replacement"])], pre=[("amortized analysis", ["potential function"])],
         exc=["offline optimal Belady"], dom=("software", "strong"), edges=[("PM-001.2", "uses", "directed"), ("PM-002.1", "shared_technique", "symmetric")],
         out="LRU is k-competitive for paging."),
    dict(id="PM-001.2", fam="PM-001", sid=1, status="ACTIVE", title="Graph edit distance alignment",
         purpose="Alignment of labelled graphs under edit operations.",
         trig=["edit-distance", "graph-alignment"], kw=["graphs", "edits"],
         tasks=["Compute an alignment between two graphs"], qa=["How are edit scripts between graphs computed?"],
         syn=[("edit script", [])], pre=[], exc=["string matching only"], dom=("algorithms", "moderate"),
         edges=[("PM-001.1", "contradicts", "symmetric")], out="Alignment cost is bounded by the edit distance."),
    dict(id="PM-002.1", fam="PM-002", sid=2, status="ACTIVE", title="Spectral graph partitioning",
         purpose="Cheeger inequality for graph partitioning with eigenvalues.",
         trig=["spectral-partitioning", "cheeger-inequality"], kw=["graph", "eigenvalue"],
         tasks=["Partition a graph with a spectral cut"], qa=["Which cut does the Fiedler vector give?"],
         syn=[], pre=[("linear algebra", [])], exc=[], dom=("tools", "weak"),
         edges=[("PM-001.1", "supersedes", "directed")], out="The Fiedler cut has conductance at most sqrt(2 lambda)."),
    dict(id="PM-003.1", fam="PM-003", sid=2, status="RETIRED", title="Zymurgy retired note",
         purpose="Zymurgy fermentation retired.", trig=["zymurgy-note"], kw=["zymurgy"], tasks=[], qa=[], syn=[], pre=[],
         exc=[], dom=("software", "none"), edges=[], out="Zymurgy is retired."),
]
WORDS = ("matrix lattice sampling tensor ordering hashing quantum polynomial convex tree flow sparse kernel metric regret bandit "
         "entropy coding sketch streaming parity circuit group ring module sheaf wavelet gradient markov martingale").split()


def synth(n, seed=0):
    r = random.Random(seed)
    out = []
    for k in range(n):
        w = [r.choice(WORDS) for _ in range(8)]
        out.append(dict(id="PM-010.%d" % (k + 1), fam="PM-010", sid=3, status="ACTIVE", title=" ".join(w[:3]).title(),
                        purpose="Study of %s and %s." % (w[3], w[4]), trig=["%s-%s" % (w[0], w[1]), "%s-%s" % (w[2], w[5])],
                        kw=[w[6], w[7]], tasks=["Apply %s to %s" % (w[0], w[3])], qa=["How does %s relate to %s?" % (w[1], w[4])],
                        syn=[], pre=[], exc=[], dom=("algorithms", "weak"), edges=[], out="%s bounds %s." % (w[5], w[6])))
    return out


def block(name, obj):
    return "<!-- PM:BEGIN %s -->\n```json\n%s\n```\n<!-- PM:END %s -->\n" % (name, json.dumps(obj, sort_keys=True, indent=1), name)


def paper_text(d, extra=None, bad_json=False, fence=False):
    ex = [{"query": q, "reason": "r"} for q in d["exc"]]
    s = {"schema": "pm.search.v1", "pm_id": d["id"], "retrieval_triggers": d["trig"], "keywords": d["kw"],
         "engineering_tasks": [{"task": t, "contract_anchor": "stmt-main", "maturity": "adaptable"} for t in d["tasks"]],
         "questions_answered": [{"question": q, "answer_anchor": "stmt-main" if i == 0 else "stmt-missing"} for i, q in enumerate(d["qa"])],
         "synonyms": [{"term": t, "aliases": a} for t, a in d["syn"]],
         "prerequisite_chains": [{"concept": c, "requires_concepts": r} for c, r in d["pre"]], "exclusions": ex}
    if fence:
        s["note"] = "a fence ``` inside a string"
    e = {"schema": "pm.edges.v2", "pm_id": d["id"], "edges": [{"relation": r, "direction": di, "source_pm_id": d["id"], "target_pm_id": t,
                                                                "verification": "PENDING"} for t, r, di in d["edges"]]}
    dm = {"schema": "pm.domains.v2", "pm_id": d["id"], "domains": [{"domain": d["dom"][0], "strength": d["dom"][1], "applies": d["dom"][1] != "none"}]}
    t = "# %s\n\n" % d["title"] + "".join('<a id="pm-s%d"></a>\n' % i for i in range(8)) + '<a id="stmt-main"></a>\n\nBody.\n\n'
    t += block("pm.search.v1", s)
    if bad_json:
        t = t.replace('"pm_id"', '"pm_id"', 1).replace("```\n<!-- PM:END pm.search.v1", "{oops```\n<!-- PM:END pm.search.v1", 1)
    t += block("pm.edges.v2", e) + block("pm.domains.v2", dm) + block("pm.reading.v2", {"schema": "pm.reading.v2", "slices": []})
    if extra:
        t += block(extra, {"x": 1})
    return t


def make_repo(root, n_synth=0, extra_block=None, bad_json=False, fence=True, docs=None):
    docs = list(docs if docs is not None else HAND) + synth(n_synth)
    cat = os.path.join(root, "catalogue")
    os.makedirs(os.path.join(cat, "families"), exist_ok=True)
    os.makedirs(os.path.join(root, "papers"), exist_ok=True)
    rows = ["id\tsubject_id\tpaper\tpage"]
    pages = {}
    subj = {1: ("Online algorithms", ["online", "competitive"]), 2: ("Graph theory", ["graph", "spectral"]), 3: ("Synthetic", ["synthetic"])}
    fams = {}
    for k, d in enumerate(docs):
        paper = "papers/%s_%s.md" % ("EN-%04d" % k, d["id"])
        with open(os.path.join(root, paper), "w", encoding="utf-8", newline="\n") as f:
            f.write(paper_text(d, extra_block if k == 0 else None, bad_json and k == 1, fence and k == 0))
        pg = "catalogue/subjects/%d/pages/p001.jsonl" % d["sid"]
        card = {"id": d["id"], "family": d["fam"], "title": d["title"], "purpose": d["purpose"], "status": d["status"],
                "inputs": {"hypotheses": [{"anchor": "stmt-main", "statement": "Given a finite instance."}]},
                "outputs": [{"claim_id": "c1", "exact_statement": d["out"], "kind": "theorem", "statement_anchor": "stmt-main"}],
                "exclusions": [{"query": q} for q in d["exc"]], "domains": []}
        pages.setdefault(pg, []).append(card)
        rows.append("\t".join([d["id"], str(d["sid"]), paper, pg]))
        fams[d["fam"]] = {"title": "Family " + d["fam"]}
    for pg, cs in pages.items():
        os.makedirs(os.path.dirname(os.path.join(root, pg)), exist_ok=True)
        with open(os.path.join(root, pg), "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(c, sort_keys=True) + "\n" for c in cs))
    for fam, v in fams.items():
        with open(os.path.join(cat, "families", fam + ".json"), "w", encoding="utf-8") as f:
            json.dump(v, f)
    with open(os.path.join(cat, "measurements.json"), "w", encoding="utf-8") as f:
        json.dump({"pages": {pg: {"tokens": 100} for pg in pages}, "details": {}}, f)
    with open(os.path.join(cat, "routes.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")
    with open(os.path.join(cat, "subjects.json"), "w", encoding="utf-8") as f:
        json.dump({"subjects": [{"subject_id": k, "subject": v[0], "keywords": v[1]} for k, v in sorted(subj.items())]}, f)
    return root
