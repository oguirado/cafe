#!/usr/bin/env python3
"""g_thes semantics (section 4.4): schema, no PM ids, single_ok only for df = 0, DEV provenance, df > 0 targets."""
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import route_v2 as R  # noqa: E402
import jsonschema_lite as JS  # noqa: E402

SCHEMA = os.path.join(os.path.dirname(HERE), "standards", "schemas", "ctx.thesaurus.v1.schema.json")
PMID = re.compile(r"^pm-?[0-9]")


def check(thes_path, index, dev_path=None):
    errs = []
    with open(thes_path, encoding="utf-8") as f:
        t = json.load(f)
    with open(SCHEMA, encoding="utf-8") as f:
        errs += ["schema " + e for e in JS.validate(t, json.load(f))]
    ix = R.Index(index)
    qids = set()
    if dev_path:
        with open(dev_path, encoding="utf-8") as f:
            qids = {json.loads(l)["qid"] for l in f if l.strip()}
    df = lambda term: (ix.post(term) or {"d": 0})["d"]
    seen = set()
    for e in t.get("entries", []):
        i = e.get("id")
        if i in seen:
            errs.append("%s duplicate id" % i)
        seen.add(i)
        for w in e.get("to", []):
            if PMID.match(w):
                errs.append("%s: PM id in to: %s" % (i, w))
            elif not any(df(x) > 0 for x in R.toks(w)):
                errs.append("%s: to term has df 0: %s" % (i, w))
        for w in e.get("when", []):
            if PMID.match(w):
                errs.append("%s: PM id in when: %s" % (i, w))
        if e.get("single_ok"):
            for w in e.get("when", []):
                tk = R.toks(w)
                if len(tk) != 1 or df(tk[0]) != 0:
                    errs.append("%s: single_ok needs a single when token with df 0: %s" % (i, w))
        pv = e.get("provenance", {})
        if pv.get("source") == "dev_split":
            q = pv.get("qids", [])
            if not q or not (set(q) & qids):
                errs.append("%s: dev_split provenance needs >=1 qid present in DEV" % i)
        if pv.get("source") == "card_field" and not pv.get("card"):
            errs.append("%s: card_field provenance needs a card coordinate" % i)
    return {"gate": "g_thes", "pass": not errs, "errors": errs[:20], "entries": len(t.get("entries", []))}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--thesaurus", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--dev")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    r = check(a.thesaurus, a.index, a.dev)
    print(json.dumps(r, sort_keys=True))
    return 0 if r["pass"] else 5


if __name__ == "__main__":
    sys.exit(main())
