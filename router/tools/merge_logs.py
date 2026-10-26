#!/usr/bin/env python3
"""Single-writer merge of the per-agent query-log spool into one journal (V2-12)."""
import argparse, glob, json, os, re, sys


def merge(spool, out):
    lines = []
    for p in sorted(glob.glob(os.path.join(spool, "w_*.jsonl"))):
        agent = re.match(r"w_(.*)\.jsonl$", os.path.basename(p)).group(1)
        with open(p, encoding="utf-8") as f:
            for n, l in enumerate(f):
                if l.strip():
                    json.loads(l)   # fail closed on a corrupt line
                    lines.append((agent, n, l.rstrip("\n")))
    lines.sort(key=lambda x: (x[0], x[1]))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for _, _, l in lines:
            f.write(l + "\n")
    os.replace(tmp, out)
    return len(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", required=True)
    ap.add_argument("--out", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not os.path.isdir(a.spool):
        return 3
    try:
        n = merge(a.spool, a.out)
    except ValueError:
        print("corrupt spool line", file=sys.stderr)
        return 5
    print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
