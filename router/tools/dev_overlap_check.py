#!/usr/bin/env python3
"""Count normalised-text overlap between DEV and TEST query files. Prints only the count and the DEV qids (never TEST text)."""
import argparse, json, sys, unicodedata


def norm(t):
    return " ".join(unicodedata.normalize("NFKC", t).casefold().split())


def read_texts(path):
    """Return [(id, text)] from a ctx.bench.v1 jsonl or a TSV (id <TAB> ... <TAB> text)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, l in enumerate(f):
            l = l.rstrip("\n")
            if not l.strip():
                continue
            if l.lstrip().startswith("{"):
                o = json.loads(l)
                out.append((o.get("qid", str(n)), o["text"]))
            else:
                p = l.split("\t")
                out.append((p[0], p[-1]))
    return out


def check(dev, test):
    t = {norm(x) for _, x in read_texts(test)}
    qids = sorted(q for q, x in read_texts(dev) if norm(x) in t)
    return {"shared": len(qids), "qids": qids}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True)
    ap.add_argument("--test", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    print(json.dumps(check(a.dev, a.test), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
