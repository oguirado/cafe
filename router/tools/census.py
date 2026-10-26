#!/usr/bin/env python3
"""Census C-1..C-14 over a PM repository (T1)."""
import argparse, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blocks as BL  # noqa: E402

SOFT = {"software", "algorithms", "tools", "data_structures"}
STR = {"strong", "moderate"}
NEED = ["pm.search.v1", "pm.edges.v2", "pm.reading.v2", "pm.domains.v2", "pm.node.v2"]


def census(papers):
    c = {"papers": 0, "papers_with_required_blocks": 0, "triggers_papers": 0, "triggers": 0, "trigger_words": {"1": 0, "2": 0, "3": 0, "4": 0, "5+": 0},
         "keywords_papers": 0, "keywords": 0, "questions_papers": 0, "questions": 0, "qa_anchor_resolved": 0,
         "tasks_papers": 0, "tasks": 0, "task_anchor_resolved": 0, "task_maturity": {},
         "syn_papers": 0, "syn_terms": 0, "syn_aliases": 0, "prereq_papers": 0, "prereq_items": 0, "prereq_with_pm_ids": 0, "prereq_pm_ids": 0,
         "exclusions_papers": 0, "exclusions": 0, "edge_papers": 0, "edges": 0, "edge_targets_distinct": 0, "edge_targets_in_corpus": 0,
         "edge_relations": {}, "domain_entries": 0, "software_ish_papers": 0, "reading_slices": 0}
    slices, sfield, targets, ids = [], [], set(), set()
    edge_t = []
    for path in papers:
        b = BL.read_blocks(path)
        a = BL.anchors(path)
        c["papers"] += 1
        c["papers_with_required_blocks"] += all(n in b for n in NEED)
        s = b.get("pm.search.v1", {})
        ids.add(s.get("pm_id") or b.get("pm.node.v2", {}).get("pm_id"))
        trig = s.get("retrieval_triggers", [])
        c["triggers_papers"] += bool(trig)
        c["triggers"] += len(trig)
        for t in trig:
            n = len(t.replace("-", " ").split())
            c["trigger_words"]["5+" if n >= 5 else str(max(n, 1))] += 1
        kw = s.get("keywords", [])
        c["keywords_papers"] += bool(kw)
        c["keywords"] += len(kw)
        qs = s.get("questions_answered", [])
        c["questions_papers"] += bool(qs)
        c["questions"] += len(qs)
        c["qa_anchor_resolved"] += sum(q.get("answer_anchor") in a for q in qs)
        ts = s.get("engineering_tasks", [])
        c["tasks_papers"] += bool(ts)
        c["tasks"] += len(ts)
        c["task_anchor_resolved"] += sum(t.get("contract_anchor") in a for t in ts)
        for t in ts:
            m = t.get("maturity", "?")
            c["task_maturity"][m] = c["task_maturity"].get(m, 0) + 1
        sy = s.get("synonyms", [])
        c["syn_papers"] += bool(sy)
        c["syn_terms"] += len(sy)
        c["syn_aliases"] += sum(len(x.get("aliases", [])) for x in sy)
        pc = s.get("prerequisite_chains", [])
        c["prereq_papers"] += bool(pc)
        c["prereq_items"] += len(pc)
        c["prereq_with_pm_ids"] += sum(bool(x.get("supporting_pm_ids")) for x in pc)
        c["prereq_pm_ids"] += sum(len(x.get("supporting_pm_ids", [])) for x in pc)
        ex = s.get("exclusions", [])
        c["exclusions_papers"] += bool(ex)
        c["exclusions"] += len(ex)
        es = b.get("pm.edges.v2", {}).get("edges", [])
        c["edge_papers"] += bool(es)
        c["edges"] += len(es)
        for e in es:
            targets.add(e["target_pm_id"])
            edge_t.append(e["target_pm_id"])
            r = e["relation"]
            c["edge_relations"][r] = c["edge_relations"].get(r, 0) + 1
        ds = b.get("pm.domains.v2", {}).get("domains", [])
        c["domain_entries"] += len(ds)
        c["software_ish_papers"] += any(d.get("applies") and d.get("domain") in SOFT and d.get("strength") in STR for d in ds)
        sl = b.get("pm.reading.v2", {}).get("slices", [])
        c["reading_slices"] += len(sl)
        slices.append(len(sl))
        sub = {k: s.get(k) for k in ("engineering_tasks", "synonyms", "keywords", "retrieval_triggers", "questions_answered")}
        sfield.append(len(json.dumps(sub, separators=(",", ":"), sort_keys=True).encode()))
    c["edge_targets_distinct"] = len(targets)
    c["edge_targets_in_corpus"] = sum(t in ids for t in edge_t)
    slices.sort()
    sfield.sort()
    c["reading_slices_p50"] = slices[len(slices) // 2] if slices else 0
    c["reading_slices_max"] = max(slices) if slices else 0
    c["search_field_json_bytes"] = {"p50": sfield[len(sfield) // 2] if sfield else 0, "max": max(sfield) if sfield else 0, "total": sum(sfield)}
    return c


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    papers = sorted(glob.glob(os.path.join(a.root, "papers", "*.md")))
    try:
        c = census(papers)
    except BL.BlockError as e:
        print(e, file=sys.stderr)
        return 3
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(c, sort_keys=True, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
