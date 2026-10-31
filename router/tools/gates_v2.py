#!/usr/bin/env python3
"""Gates of design section 5. Every gate fails closed; rc 0 only if all pass."""
import argparse, ast, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build_index_v2 as B  # noqa: E402
import route_v2 as R  # noqa: E402
import jsonschema_lite as JS  # noqa: E402
import blocks as BL  # noqa: E402

CAP = 393216
SCH = os.path.join(W, "standards", "schemas")
ISO = re.compile(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d|\bpid[ =:]\d+|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,? \d{1,2} [A-Z][a-z]{2} 20\d\d", re.I)
PRIV = re.compile('/' + 'home' + '/[a-z0-9_]+|/' + 'Users' + '/[A-Za-z0-9_]+|C:\\\\Users|\\b[\\w.+-]+@[\\w-]+\\.[a-z]{2,}\\b|(?:api[_-]?key|secret|passwd|password|token)\\s*[=:]\\s*\\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY|\\bsk-[A-Za-z0-9]{20,}', re.I)


def jl(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def schema(name):
    return jl(os.path.join(SCH, name + ".schema.json"))


def files_of(d):
    return sorted(os.path.relpath(os.path.join(a, f), d) for a, _, fs in os.walk(d) for f in fs)


def tree(d):
    return {k: B.readb(os.path.join(d, k)) for k in files_of(d)}


def res(gate, errs, **kw):
    return dict({"gate": gate, "pass": not errs, "errors": errs[:10]}, **kw)


def read_dev(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---- g_fresh ----------------------------------------------------------------------------------------
def g_fresh(root, index, thesaurus=None, builder=None):
    man = jl(os.path.join(index, "manifest.json"))
    now = {}
    B.read_papers(root, now)
    th = thesaurus or os.path.join(W, "standards", "thesaurus.json")
    now["standards/thesaurus.json"] = B.sha(B.readb(th)) if os.path.isfile(th) else B.sha(b"")
    now["tools/build_index_v2.py"] = B.sha(B.readb(builder or os.path.join(HERE, "build_index_v2.py")))
    bad = sorted(k for k in set(now) | set(man["sources"]) if now.get(k) != man["sources"].get(k))
    return {"gate": "g_fresh", "pass": not bad, "errors": bad[:10], "code": None if not bad else "E_STALE", "stale": bad[:10], "checked": len(now)}


# ---- g_rebuild / g_volatile ------------------------------------------------------------------------
_REBUILD = r"""
import os, random, sys
sys.path.insert(0, %(tools)r)
real = os.listdir
rnd = random.Random(int(sys.argv[4]))
def shuf(p="."):
    l = list(real(p)); rnd.shuffle(l); return l
os.listdir = shuf
os.chdir(sys.argv[3])
import build_index_v2 as B
B.build(sys.argv[1], sys.argv[2], sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None)
"""


def rebuild_perturbed(root, out, thesaurus, tz, epoch, cwd, seed):
    env = dict(os.environ, TZ=tz, SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED=str(seed))
    return subprocess.run([sys.executable, "-c", _REBUILD % {"tools": HERE}, root, out, cwd, str(seed), thesaurus or ""], env=env,
                          capture_output=True, text=True)


def g_rebuild(root, index, thesaurus=None, name="g_rebuild"):
    errs = []
    d = tempfile.mkdtemp(prefix="g_rb")
    try:
        for k, (tz, ep, seed) in enumerate((("Pacific/Kiritimati", 0, 11), ("America/Los_Angeles", 1700000000, 12))):
            out = os.path.join(d, "ix%d" % k)
            r = rebuild_perturbed(root, out, thesaurus, tz, ep, d, seed)
            if r.returncode != 0:
                errs.append("build rc %d %s" % (r.returncode, r.stderr[-200:]))
                continue
            a, b = tree(index), tree(out)
            if a != b:
                errs.append("tree differs: %s" % sorted(k2 for k2 in set(a) | set(b) if a.get(k2) != b.get(k2))[:5])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return res(name, errs)


def g_volatile(root, index, entry_files, thesaurus=None):
    errs = list(g_rebuild(root, index, thesaurus, "g_volatile")["errors"])
    for p in entry_files:
        if os.path.isfile(p):
            txt = open(p, encoding="utf-8", errors="replace").read()
            m = ISO.search(txt)
            if m:
                errs.append("volatile token in %s: %r" % (os.path.basename(p), m.group(0)))
    return res("g_volatile", errs, scanned=len([p for p in entry_files if os.path.isfile(p)]))


# ---- g_size / g_nofloat ----------------------------------------------------------------------------
def g_size(index, cap=CAP):
    errs, sizes = [], {}
    for k in files_of(index):
        sizes[k] = os.path.getsize(os.path.join(index, k))
        if sizes[k] > cap:
            errs.append("%s %d > cap" % (k, sizes[k]))
    man = jl(os.path.join(index, "manifest.json"))
    listed = [sizes[k] for k in man["files"] if k in sizes]
    if man["max_file_bytes"] != max(listed):
        errs.append("manifest.max_file_bytes %d != measured %d" % (man["max_file_bytes"], max(listed)))
    return res("g_size", errs, files=len(sizes), max_file_bytes=max(sizes.values()))


def _bad(x):
    raise ValueError("float " + x)


def parse_nofloat(text):
    return json.loads(text, parse_float=_bad, parse_constant=_bad)


def g_nofloat(index, answers=()):
    errs = []
    for k in files_of(index):
        if k == "ID":
            continue
        txt = B.readb(os.path.join(index, k)).decode("utf-8")
        for n, line in enumerate(txt.split("\n")):
            if line.strip():
                try:
                    parse_nofloat(line)
                except ValueError as e:
                    errs.append("%s:%d %s" % (k, n + 1, e))
                    break
    for a in answers:
        try:
            parse_nofloat(json.dumps(a))
        except ValueError as e:
            errs.append("answer " + str(e))
    return res("g_nofloat", errs)


# ---- answers / g_compact ---------------------------------------------------------------------------
REASON_FLAG = {"exact_id": False, "no_query_term": True, "no_candidate": True, "uncalibrated": False, "low_term_match": True,
               "below_tau": True, "none": False}


def check_answer(ans, q=None, budget=2000):
    errs = []
    ser = R.dumps(ans).encode("utf-8")
    if len(ser) + 1 > budget:
        errs.append("answer %d B > %d" % (len(ser) + 1, budget))
    errs += JS.validate(ans, schema("ctx.shortlist.v2"))
    ab = ans.get("abstain", {})
    if REASON_FLAG.get(ab.get("reason")) is not ab.get("flag"):
        errs.append("reason/flag mismatch %s" % ab)
    if ab.get("reason") in ("no_query_term", "no_candidate") and ans.get("r"):
        errs.append("rows on empty-reason answer")
    for i, r in enumerate(ans.get("r", [])):
        if i >= 3 and len(r) != 3:
            errs.append("why beyond rank 3")
        if not isinstance(r, list) or len(r) < 3 or not isinstance(r[0], str) or not isinstance(r[1], str):
            continue
        if len(r[0].encode("utf-8")) > R.LIM["id"] or len(r[1].encode("utf-8")) > R.LIM["anchor"]:
            errs.append("row %d id/anchor exceeds the UTF-8 byte limit" % i)
        if len(r) > 3 and any(len(t.encode("utf-8")) > R.LIM["why"] for t in r[3] if isinstance(t, str)):
            errs.append("row %d why term exceeds the UTF-8 byte limit" % i)
    if any(isinstance(w, str) and len(w.encode("utf-8")) > R.LIM["warn"] for w in ans.get("warn", [])):
        errs.append("warn id exceeds the UTF-8 byte limit")
    if q is not None and ans.get("q") != hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]:
        errs.append("q hash")
    ids = R.ID_RE.findall(q or "")
    pos = 0
    if q is not None and ids:
        pos = 0
        while pos < len(ans.get("r", [])) and any(ans["r"][pos][0] == x or ans["r"][pos][0].startswith(x.split(".")[0] + ".") for x in ids) and pos < len(ids) + 20:
            pos += 1
    sc = [r[2] for r in ans.get("r", [])][pos:]
    if any(a < b for a, b in zip(sc, sc[1:])):
        errs.append("scores not non-increasing")
    return errs


def run_precedence_cases():
    sys.path.insert(0, os.path.join(W, "tests", "fixtures"))
    import repo_gen
    errs = []
    cases = jl(os.path.join(W, "tests", "fixtures", "precedence", "cases.json"))
    d = tempfile.mkdtemp(prefix="g_pc")
    try:
        for c in cases:
            root = repo_gen.make_repo(os.path.join(d, c["name"], "repo"), n_synth=c["repo"]["n_synth"])
            out = os.path.join(d, c["name"], "ix")
            thes = None
            if c.get("thes"):
                thes = os.path.join(d, c["name"], "thes.json")
                with open(thes, "w") as f:
                    json.dump({"schema": "ctx.thesaurus.v1", "version": 1, "entries": c["thes"]}, f)
            B.build(root, out, thes)
            x = c["expect"]
            try:
                ans = R.route(R.Index(out), c["q"], c["params"], budget=c["budget"])
            except R.BudgetError as ex:
                if str(ex) != x.get("error"):
                    errs.append("%s: unexpected %s" % (c["name"], ex))
                continue
            if x.get("error"):
                errs.append("%s: expected %s, got an answer" % (c["name"], x["error"]))
                continue
            e = ans["abstain"]
            if e["reason"] != x["reason"] or e["flag"] is not x["flag"]:
                errs.append("%s: got %s" % (c["name"], e))
            if len(ans["r"]) < x.get("rows_min", 0) or len(ans["r"]) > x.get("rows_max", 99):
                errs.append("%s: rows %d" % (c["name"], len(ans["r"])))
            if bool(ans.get("overflow")) != x.get("overflow", False):
                errs.append("%s: overflow" % c["name"])
            errs += ["%s: %s" % (c["name"], m) for m in check_answer(ans, c["q"], c["budget"])]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return errs


def dev_answers(index, dev, params_path):
    ix = R.Index(index)
    pr = R.load_params(params_path if params_path and os.path.isfile(params_path) else None)
    out = []
    for r in dev:
        for params, prh in ((pr[0], pr[1]), ({}, "0" * 16)):
            out.append((r["text"], R.route(ix, r["text"], params, prh)))
    return out


def g_compact(index, dev, params_path=None, cases=True):
    errs = []
    for q, a in dev_answers(index, dev, params_path):
        errs += ["%s: %s" % (q[:30], m) for m in check_answer(a, q)]
    if cases:
        errs += run_precedence_cases()
    return res("g_compact", errs)


# ---- g_schema ---------------------------------------------------------------------------------------
def g_schema(index, params_path=None, thesaurus=None, answers=(), protocol=None):
    errs = []
    errs += ["manifest " + e for e in JS.validate(jl(os.path.join(index, "manifest.json")), schema("ctx.index-manifest.v2"))]
    skipped = []
    if params_path and os.path.isfile(params_path):
        p = jl(params_path)
        errs += ["params " + e for e in JS.validate(p, schema("ctx.router-params.v2"))]
        if (p.get("tau_cov") is None) != (p.get("tau_peak") is None):
            errs.append("tau_cov/tau_peak must be both null or both integers")
        if p.get("w_edge") == 250 and not p.get("null_test", {}).get("enabled"):
            errs.append("w_edge 250 without null_test.enabled")
    else:
        skipped.append("params")
    if thesaurus and os.path.isfile(thesaurus):
        errs += ["thesaurus " + e for e in JS.validate(jl(thesaurus), schema("ctx.thesaurus.v1"))]
    if protocol and os.path.isfile(protocol):
        errs += ["protocol " + e for e in JS.validate(jl(protocol), schema("ctx.protocol.v2"))]
    for q, a in answers:
        errs += ["answer " + e for e in JS.validate(a, schema("ctx.shortlist.v2"))]
    return res("g_schema", errs, skipped=skipped)


# ---- g_anchor ---------------------------------------------------------------------------------------
def paper_anchors(root):
    rows = [l.split("\t") for l in B.readb(os.path.join(root, "catalogue", "routes.tsv")).decode("utf-8").split("\n")][1:]
    return {r[0]: r[2] for r in rows if len(r) >= 3 and r[0]}


def g_anchor(root, index, answers=()):
    errs, checked = [], 0
    ix = R.Index(index)
    papers = paper_anchors(root)
    cache = {}

    def anc(i):
        pid = ix.ids[i]
        if pid not in cache:
            cache[pid] = BL.anchors(os.path.join(root, papers[pid]))
        return cache[pid]
    for i in range(ix.N):
        rec = ix.doc(i)
        for a in [x[0] for x in rec["aq"]] + ([rec["oa"]] if rec.get("oa") else []):
            checked += 1
            if a not in anc(i):
                errs.append("%s: doc anchor %s unresolved" % (ix.ids[i], a))
    pos = {x: i for i, x in enumerate(ix.ids)}
    for q, a in answers:
        for r in a["r"]:
            checked += 1
            if r[1] not in anc(pos[r[0]]):
                errs.append("answer %s %s unresolved" % (r[0], r[1]))
    return res("g_anchor", errs[:10], checked=checked, unresolved=len(errs))


# ---- g_determinism ----------------------------------------------------------------------------------
def g_determinism(index, dev, params_path=None):
    errs = []
    base = [sys.executable, "-I", os.path.join(HERE, "route_v2.py"), "--index", index]
    if params_path and os.path.isfile(params_path):
        base += ["--params", params_path]
    for r in dev:
        outs = [subprocess.run(base + ["--q", r["text"]], capture_output=True).stdout for _ in range(2)]
        if outs[0] != outs[1] or not outs[0]:
            errs.append("process runs differ: " + r["qid"])
        inproc = (R.dumps(R.route(R.Index(index), r["text"], *R.load_params(params_path if params_path and os.path.isfile(params_path) else None))) + "\n").encode()
        if inproc != outs[0]:
            errs.append("in-process differs: " + r["qid"])
    mm = getattr(R, "route_multi", None)
    shuffled = None
    if mm is not None:
        m1 = [{"corpus": "math", "path": index, "priority": 0}, {"corpus": "math2", "path": index, "priority": 1}]
        for r in dev[:10]:
            a = mm(m1, r["text"], {}, "0" * 16)
            b = mm(list(reversed(m1)), r["text"], {}, "0" * 16)
            if R.dumps(a) != R.dumps(b):
                errs.append("mount-order dependent: " + r["qid"])
        shuffled = True
    return res("g_determinism", errs, queries=len(dev), mount_shuffle=shuffled)


# ---- g_stdlib ---------------------------------------------------------------------------------------
LOCAL = {"build_index_v2", "route_v2", "tune_v2", "eval_v2", "blocks", "census", "gates_v2", "null_edges", "jsonschema_lite", "merge_logs",
         "make_pool_v2", "thes_check", "dev_overlap_check", "route", "build_index", "eval_route", "make_pool", "repo_gen", "common"}


def g_stdlib(tools_dir, names=None):
    errs, std = [], set(sys.stdlib_module_names)
    for p in sorted(glob.glob(os.path.join(tools_dir, "*.py"))):
        base = os.path.basename(p)[:-3]
        if base == "route_local":
            continue
        tree_ = ast.parse(open(p, encoding="utf-8").read())
        for n in ast.walk(tree_):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) and n.level == 0 else []
            for m in mods:
                top = m.split(".")[0]
                if top and top not in std and top not in LOCAL:
                    errs.append("%s imports %s" % (base, top))
        if base == "route_v2" and "route_local" in open(p, encoding="utf-8").read().replace("route_local.py", ""):
            errs.append("route_v2 references route_local")
    return res("g_stdlib", errs)


# ---- g_thes / g_blind / g_calib / g_privacy ---------------------------------------------------------
def g_thes(thesaurus, index, dev_path):
    import thes_check
    if not thesaurus or not os.path.isfile(thesaurus):
        return res("g_thes", ["thesaurus file absent"])
    r = thes_check.check(thesaurus, index, dev_path)
    return res("g_thes", r["errors"], entries=r["entries"])


def g_blind(dev_path, params_path=None, tune=None):
    errs = []
    tune = tune or os.path.join(HERE, "tune_v2.py")
    d = tempfile.mkdtemp(prefix="g_bl")
    try:
        os.makedirs(os.path.join(d, "bench"))
        p = os.path.join(d, "bench", "x.jsonl")
        open(p, "w").write('{"qid":"X","split":"DEV","answerable":true,"text":"t","gold":[]}\n')
        r = subprocess.run([sys.executable, "-I", tune, "--index", os.path.join(W, "out", "index"), "--bench", p], capture_output=True)
        if r.returncode != 4:
            errs.append("tune did not refuse /bench/ path (rc %d)" % r.returncode)
        p2 = os.path.join(d, "t.jsonl")
        open(p2, "w").write('{"qid":"X","split":"TEST","answerable":true,"text":"t","gold":[]}\n')
        r = subprocess.run([sys.executable, "-I", tune, "--index", os.path.join(W, "out", "index"), "--bench", p2], capture_output=True)
        if r.returncode != 4:
            errs.append("tune did not refuse TEST rows (rc %d)" % r.returncode)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if any(x.get("split") == "TEST" for x in read_dev(dev_path)):
        errs.append("DEV file contains TEST rows")
    if R.is_bench_path(dev_path):
        errs.append("DEV file under /bench/")
    skipped = []
    if params_path and os.path.isfile(params_path):
        if jl(params_path).get("dev_sha256") != hashlib.sha256(B.readb(dev_path)).hexdigest():
            errs.append("params.dev_sha256 != sha256(DEV file)")
    else:
        skipped.append("params")
    return res("g_blind", errs, skipped=skipped)


def g_calib(dev_path):
    rows = read_dev(dev_path)
    a = sum(1 for r in rows if r["answerable"])
    errs = [] if a >= 1 and len(rows) - a >= 1 else ["E_CALIBRATION: answerable=%d unanswerable=%d" % (a, len(rows) - a)]
    return res("g_calib", errs, answerable=a, unanswerable=len(rows) - a, thin_dev=a < 20 or len(rows) - a < 20)


def private_policy():
    """Optional reviewed external hashes; absent policy never disables generic privacy."""
    configured = os.environ.get("ROUTER_PRIVATE_POLICY")
    path = configured or os.path.join(W, "..", "local", "private_denylist.json")
    if not os.path.exists(path):
        if configured: raise ValueError("configured_privacy_policy_absent")
        return []
    if any(part in {"operator.token", "launch.token", "auth.json"} or part.endswith(".key") for part in os.path.realpath(path).split(os.sep)):
        raise ValueError("sensitive_privacy_policy_not_opened")
    policy = jl(path)
    if set(policy) != {"schema", "reviewed", "deny"} or policy["schema"] != "pm.release-policy.v1" or policy["reviewed"] is not True or not isinstance(policy["deny"], list) or not policy["deny"]:
        raise ValueError("invalid_privacy_policy")
    for rule in policy["deny"]:
        if set(rule) - {"case_sensitive"} != {"category", "words", "first_sha256", "phrase_sha256"} or rule["category"] not in {"private_user", "private_host", "private_tool", "private_corpus", "private_project"} or type(rule["words"]) is not int or not 1 <= rule["words"] <= 12 or rule.get("case_sensitive", False) not in [True, False] or any(not re.fullmatch(r"[a-f0-9]{64}", rule[key]) for key in ["first_sha256", "phrase_sha256"]):
            raise ValueError("invalid_privacy_policy_rule")
    return policy["deny"]


def private_hit(text, rules):
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    words = re.findall(r"[^\W_]+", text.casefold())
    for i, word in enumerate(words):
        first = hashlib.sha256(word.encode()).hexdigest()
        for rule in rules:
            if first == rule["first_sha256"] and hashlib.sha256(" ".join(words[i:i+rule["words"]]).encode()).hexdigest() == rule["phrase_sha256"]:
                return True
    return False


def g_privacy(paths):
    errs, n = [], 0
    try:
        rules = private_policy()
    except (OSError, ValueError, TypeError, KeyError):
        return res("g_privacy", ["privacy_policy_unavailable_or_invalid"], scanned=0)
    for p in paths:
        if os.path.isdir(p):
            fs = [os.path.join(p, k) for k in files_of(p)]
        elif os.path.isfile(p):
            fs = [p]
        else:
            continue
        for f in fs:
            n += 1
            text = B.readb(f).decode("utf-8", errors="replace")
            if PRIV.search(text) or private_hit(text, rules):
                errs.append("private_data file_sha256=" + hashlib.sha256(os.fsencode(os.path.relpath(f, os.path.dirname(p) if os.path.isfile(p) else p))).hexdigest())
    return res("g_privacy", errs, scanned=n)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--params")
    ap.add_argument("--dev", required=True)
    ap.add_argument("--thesaurus", default=os.path.join(W, "standards", "thesaurus.json"))
    ap.add_argument("--out", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    dev = read_dev(a.dev)
    ans = dev_answers(a.index, dev, a.params)
    entry = [os.path.join(a.root, n) for n in ("llms.txt", "AGENTS.md", "catalogue/subjects.json")] + [os.path.join(W, "out", "protocol.json")]
    gates = [g_rebuild(a.root, a.index, a.thesaurus), g_size(a.index), g_nofloat(a.index, [x for _, x in ans]),
             g_schema(a.index, a.params, a.thesaurus, ans, os.path.join(W, "out", "protocol.json")), g_anchor(a.root, a.index, ans),
             g_fresh(a.root, a.index, a.thesaurus), g_compact(a.index, dev, a.params), g_determinism(a.index, dev, a.params),
             g_stdlib(HERE), g_thes(a.thesaurus, a.index, a.dev), g_blind(a.dev, a.params), g_calib(a.dev),
             g_volatile(a.root, a.index, entry, a.thesaurus), g_privacy([a.index] + entry)]
    out = {"schema": "ctx.gates.v1", "index_sha256": B.readb(os.path.join(a.index, "ID")).decode().strip(),
           "passed": sum(g["pass"] for g in gates), "total": len(gates), "gates": {g["gate"]: g for g in gates}}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out, sort_keys=True, indent=1) + "\n")
    return 0 if out["passed"] == out["total"] else 5


if __name__ == "__main__":
    sys.exit(main())
