#!/usr/bin/env python3
"""Deterministic integer router v2 (V2-02..V2-08, V2-11, V2-12; precedence table of section 3)."""
import argparse, bisect, hashlib, io, json, ntpath, os, re, sys, time, unicodedata
from contextlib import contextmanager

VERSION = "route_v2.py/2.0.1"
K = 1200000
CAP_EXP = 4
BUDGET = 2000
LIM = dict(id=32, anchor=64, why=24, warn=32)   # UTF-8 byte limits, enforced in code (schema maxLength counts characters)
STOP = set("""a about above after again against all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where which while who whom why will
with would you your via using use used given let thus hence one two may must shall within without upon per""".split())
PROFILES = {
    "P0": dict(trig=3000, task=2500, qa=2500, title=2000, syn=2000, purpose=1500, kw=1500, out=1000, pre=1000, subj=800, fam=500, inp=500),
    "P1": dict(trig=4000, task=2000, qa=2000, title=2000, syn=2500, purpose=1500, kw=2500, out=800, pre=800, subj=500, fam=300, inp=300),
    "P2": dict(trig=2000, task=2500, qa=1500, title=3000, syn=1500, purpose=2500, kw=1000, out=1000, pre=500, subj=1000, fam=500, inp=500),
}
BPPM_NEG = 750000
RW = [500, 800, 700, 1000, 700, 300, 1000, 0]   # by relcode (index = build RELS)
DEFAULTS = dict(profile="P0", w_exp=0, w_phr=0, lcp5=0, w_dom=0, w_edge=0, intent_boost=0, m_min=1, tau_cov=None, tau_peak=None)
INTENT_RULES = [
    ("find_limit", {"fail", "failure", "counterexample", "limitation", "limit", "impossible", "hardness", "obstruction"}),
    ("find_bound", {"bound", "bounds", "complexity", "lower", "upper", "rate", "ratio", "competitive", "guarantee", "convergence", "approximation"}),
    ("find_prerequisite", {"prerequisite", "background", "definition", "define", "basics"}),
    ("find_structure", {"structure", "representation", "layout", "invariant", "encoding"}),
    ("check_claim", {"verify", "claim", "true", "prove", "whether"}),
    ("find_method", {"algorithm", "compute", "implement", "construct", "method", "solve", "procedure"}),
]
INTENTS = ["find_method", "find_structure", "find_bound", "find_prerequisite", "check_claim", "find_limit", "locate_id", "none"]
SECTION = {"find_method": "pm-s4", "find_bound": "pm-s2", "find_prerequisite": "pm-s0", "find_structure": "pm-s5",
           "find_limit": "pm-s6", "check_claim": "pm-s1"}
BOOST = {"find_bound": ("out", "qa"), "find_method": ("task",), "find_prerequisite": ("pre",), "find_structure": ("kw", "syn"),
         "check_claim": ("out",), "find_limit": ("qa",)}
ID_RE = re.compile(r"PM-\d{3}(?:\.\d+)?")


def dumps(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def toks(text):
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"\\([a-zA-Z]+)", r" \1 ", t)
    return [w for w in re.split(r"[^0-9a-z]+", t) if len(w) > 1 and w not in STOP]


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def sha16(b):
    return hashlib.sha256(b).hexdigest()[:16]


class IndexError_(Exception):
    pass


class BudgetError(Exception):
    """E_BUDGET: the answer cannot fit the byte budget even without rows (fail closed)."""


def clip(s, n):
    """Longest prefix of s whose UTF-8 encoding is at most n bytes (code-point boundary)."""
    b = s.encode("utf-8")
    return s if len(b) <= n else b[:n].decode("utf-8", "ignore")


class Index:
    def __init__(self, d):
        self.d = d
        try:
            self.cols = jload(os.path.join(d, "cols.json"))
            self.thes = jload(os.path.join(d, "thes.json"))
            with open(os.path.join(d, "ID"), encoding="utf-8") as f:
                self.ixid = f.read().strip()
        except (OSError, ValueError) as e:
            raise IndexError_(str(e))
        self.ix16 = self.ixid[:16]
        self.N = self.cols["N"]
        self.ids = self.cols["id"]
        self._maps, self._docs = {}, {}
        self.corpus = "math"
        self.edges_override = None   # in-memory rewired graph (null test only)

    def _file(self, fn):
        if fn not in self._maps:
            try:
                self._maps[fn] = jload(os.path.join(self.d, fn))
            except OSError:
                self._maps[fn] = {}
        return self._maps[fn]

    def mapget(self, name, key):
        sh = self.cols["shards"].get(name)
        if sh:
            keys = [x[0] for x in sh]
            i = bisect.bisect_right(keys, key) - 1
            if i < 0:
                return None
            return self._file(sh[i][1]).get(key)
        return self._file(name + ".json").get(key)

    def post(self, t):
        return self.mapget("post/" + t[:2], t)

    def doc(self, i):
        s = i >> 8
        if s not in self._docs:
            rows = {}
            with open(os.path.join(self.d, "docs", "%d.jsonl" % s), encoding="utf-8") as f:
                for l in f:
                    if l.strip():
                        r = json.loads(l)
                        rows[r["i"]] = r
            self._docs[s] = rows
        return self._docs[s][i]

    def all_edges(self):
        if self.edges_override is not None:
            return self.edges_override
        ed = {}
        names = [x[1] for x in self.cols["shards"].get("edges", [])] or ["edges.json"]
        for fn in names:
            ed.update(self._file(fn))
        return ed


class VP:
    __slots__ = ("idf", "df", "z")

    def __init__(self, idf, df, z):
        self.idf, self.df, self.z = idf, df, z

    def has(self, d, zones):
        return any(d in self.z.get(f, ()) for f in zones)


def vposting(ix, t, lcp5):
    if lcp5 and len(t) >= 5:
        c = ix.mapget("pfx", t[:5])
        if not c:
            return VP(ix.cols["idf1"], 0, {})
        z = {}
        for ct in c["t"]:
            p = ix.post(ct)
            for f, flat in p["z"].items():
                zz = z.setdefault(f, {})
                for j in range(0, len(flat), 2):
                    zz[flat[j]] = zz.get(flat[j], 0) + flat[j + 1]
        return VP(c["i"], c["d"], z)
    p = ix.post(t)
    if not p:
        return VP(ix.cols["idf1"], 0, {})
    z = {f: {flat[j]: flat[j + 1] for j in range(0, len(flat), 2)} for f, flat in p["z"].items()}
    return VP(p["i"] if p["d"] else ix.cols["idf1"], p["d"], z)


def weights(params, intent):
    w = dict(PROFILES[params["profile"]])
    if params["intent_boost"]:
        for f in BOOST.get(intent, ()):
            w[f] = (3 * w[f]) // 2
    return w


def zone_x(ix, f, d, tf, w_f):
    cols = ix.cols
    A, B = cols["A"][f], cols["B"][f]
    ln = cols["len"][f][d]
    ratio = (ln * 10 ** 9) // A if A else 10 ** 6
    norm = (10 ** 6 - B) + (B * ratio) // 10 ** 6
    return (w_f * tf * 10 ** 9) // norm


def term_scores(ix, vp, w, with_neg=False):
    acc = {}
    for f, wf in w.items():
        for d, tf in vp.z.get(f, {}).items():
            acc[d] = acc.get(d, 0) + zone_x(ix, f, d, tf, wf)
    out = {d: (vp.idf * a) // (K + a) for d, a in acc.items()}
    neg = {}
    if with_neg:
        for d, tf in vp.z.get("neg", {}).items():
            a = zone_x(ix, "neg", d, tf, 1000)
            neg[d] = (vp.idf * a) // (K + a)
    return out, neg


def detect_intent(q, terms, X):
    if ID_RE.search(q) or X:
        return "locate_id"
    ts = set(terms)
    for name, words in INTENT_RULES:
        if ts & words:
            return name
    return "none"


def expansion(ix, terms):
    ts, out = set(terms), []
    for _id, whens, to, single in ix.thes["e"]:
        fire = any(set(ph) <= ts and (len(ph) >= 2 or single) for ph in whens)
        if fire:
            for e in to:
                if e not in ts and e not in out:
                    out.append(e)
    return out[:CAP_EXP]


def exact_ids(ix, q):
    X = []
    for m in ID_RE.finditer(q):
        for i, (id_, f) in enumerate(zip(ix.ids, ix.cols["fam"])):
            if (id_ == m.group(0) or ix.cols["famn"][f] == m.group(0)) and i not in X:
                X.append(i)
    return X


def lexical(ix, R, exp):
    """Scores for the parsed query R, with or without thesaurus expansion. Returns (S, Sf, order, n_elig)."""
    p, terms, vps, X = R["p"], R["terms"], R["vps"], R["X"]
    w, lcp, qt = R["w"], p["lcp5"], R["qt"]
    S, negs = {}, {}
    for t in terms:
        sc, ng = term_scores(ix, vps[t], w, True)
        for d, v in sc.items():
            S[d] = S.get(d, 0) + v
        for d, v in ng.items():
            negs[d] = negs.get(d, 0) + v
    for e in exp:
        sc, _ = term_scores(ix, vposting(ix, e, lcp), w)
        for d, v in sc.items():
            S[d] = S.get(d, 0) + (p["w_exp"] * v) // 1000
    for d, v in negs.items():
        S[d] = S.get(d, 0) - (500 * v) // 1000
    S = {d: v for d, v in S.items() if v > 0}
    if p["w_phr"]:
        fired = set()
        for i in range(len(qt)):
            for n in (2, 3, 4):
                if i + n <= len(qt):
                    key = " ".join(qt[i:i + n])
                    if key in fired:
                        continue
                    docs = ix.mapget("phr/" + key[:2], key)
                    if docs:
                        fired.add(key)
                        bonus = (p["w_phr"] * sum(vposting(ix, t, lcp).idf for t in qt[i:i + n])) // 1000
                        for d in docs:
                            S[d] = S.get(d, 0) + bonus
    pre = ix.cols["prior"]
    Sd = {d: (v * (1000 + (p["w_dom"] * pre[d]) // 1000)) // 1000 for d, v in S.items()}
    Sf = dict(Sd)
    top = sorted(Sd, key=lambda d: (-Sd[d], -S[d], d))
    if p["w_edge"] and top:
        seeds = [d for d in top[:3] if 2 * Sd[d] >= Sd[top[0]]]
        ed = ix.all_edges()
        for s in seeds:
            for t, rc in ed.get(str(s), []):
                v = (p["w_edge"] * RW[rc] * Sd[s]) // 10 ** 6
                if v:
                    Sf[t] = Sf.get(t, 0) + v
    status = ix.cols["status"]
    Xset = set(X)
    elig = {d for d, v in Sf.items() if status[d] & 3 and (v > 0 or d in Xset)}
    elig |= {d for d in X if status[d] & 3}
    order = X + sorted((d for d in elig if d not in Xset), key=lambda d: (-Sf[d], -S.get(d, 0), d))
    return S, Sf, order, len(elig)


def rank(ix, q, params, intent=None):
    p = dict(DEFAULTS, **params)
    qt = toks(q)
    terms = sorted(set(qt))
    X = exact_ids(ix, q)
    intent = intent or detect_intent(q, terms, X)
    R = dict(p=p, q=q, qt=qt, terms=terms, X=X, intent=intent, w=weights(p, intent))
    R["vps"] = {t: vposting(ix, t, p["lcp5"]) for t in terms}
    R["T"] = [t for t in terms if R["vps"][t].df > 0]
    R["exp"] = expansion(ix, terms) if p["w_exp"] else []
    R["S"], R["Sf"], R["order"], R["n_elig"] = lexical(ix, R, R["exp"])
    if R["exp"]:   # abstention features use the unexpanded ranking only (F-R-02-8)
        R["fS"], R["fSf"], R["forder"], _ = lexical(ix, R, [])
    else:
        R["fS"], R["fSf"], R["forder"] = R["S"], R["Sf"], R["order"]
    return R


def features(ix, R):
    order, vps, terms = R["forder"], R["vps"], R["terms"]
    if not order:
        return 0, 0, 10 ** 9, []
    d1 = order[0]
    zs = [z for z in ix.cols["zones"] if z != "neg"]
    matched = [t for t in terms if vps[t].df > 0 and vps[t].has(d1, zs)]
    m = min(64, len(matched))
    den = sum(vps[t].idf for t in terms)
    cov = (10 ** 6 * sum(vps[t].idf for t in matched)) // max(1, den)
    f1 = ix.cols["fam"][d1]
    S1 = R["fSf"].get(d1, 0)
    peak = 10 ** 9
    for d in order[1:]:
        if ix.cols["fam"][d] != f1:
            peak = min(10 ** 9, (10 ** 6 * S1) // max(1, R["fSf"].get(d, 0)))
            break
    return m, min(cov, 10 ** 6), peak, matched


def decide(ix, R, no_abstain=False):
    p = R["p"]
    if R["X"]:
        m, cov, peak, matched = features(ix, R)
        return False, "exact_id", m, cov, peak
    if not R["order"]:   # a successful expansion (non-empty order) overrides an empty T
        return True, ("no_candidate" if R["T"] else "no_query_term"), 0, 0, 0
    m, cov, peak, matched = features(ix, R)
    if p["tau_cov"] is None or p["tau_peak"] is None:
        return False, "uncalibrated", m, cov, peak
    if m < p["m_min"]:
        flag, reason = True, "low_term_match"
    elif cov < p["tau_cov"] or peak < p["tau_peak"]:
        flag, reason = True, "below_tau"
    else:
        flag, reason = False, "none"
    if no_abstain and flag:
        return False, "none", m, cov, peak
    return flag, reason, m, cov, peak


def choose_anchor(ix, d, R):
    rec = ix.doc(d)
    if rec.get("an"):
        for a in rec["an"]:
            if len(a) > 2 and a[2] == R["intent"]:
                return clip(a[0], LIM["anchor"])
        return clip(rec["an"][0][0], LIM["anchor"])
    qs = set(R["terms"])
    best, bs = None, 1
    for kind in ("q", "t"):
        for a, k, ts in rec["aq"]:
            if k != kind:
                continue
            o = len(qs & set(ts))
            if o > bs:
                best, bs = a, o
    if best:
        return clip(best, LIM["anchor"])
    sec = SECTION.get(R["intent"])
    if sec:
        return sec
    return clip(rec.get("oa") or "pm-s1", LIM["anchor"])


def why_terms(ix, R, d):
    zs = [z for z in ix.cols["zones"] if z != "neg"]
    ts = [t for t in R["terms"] if R["vps"][t].df > 0 and R["vps"][t].has(d, zs)]
    ts.sort(key=lambda t: (-R["vps"][t].idf, t))
    return [clip(t, LIM["why"]) for t in ts[:3]]


def envelope(ix, R, pr, abstain, rows, overflow, warn):
    e = {"schema": "ctx.shortlist.v2", "router": VERSION, "ix": ix.ix16, "pr": pr,
         "q": sha16(R["q"].encode("utf-8")), "intent": R["intent"],
         "abstain": {"flag": abstain[0], "reason": abstain[1], "m": abstain[2], "cov": abstain[3], "peak": abstain[4]}, "r": rows}
    if overflow:
        e["overflow"] = True
    if warn:
        e["warn"] = warn
    return e


def build_rows(items, n_prot, mk_env, budget):
    """items: [(base_row, full_row)]. Algorithm 3'' with protected prefix; returns (rows, overflow).
    mk_env(rows, overflow) builds the exact envelope. Pass 1 prices without the overflow flag; if a protected row fails,
    pass 2 re-packs with the flag's bytes reserved up-front, so the emitted answer (flag included) is priced exactly.
    Raises BudgetError when even the row-less envelope (with the flag if one is set) exceeds the budget."""
    def size(rows, of):
        return len(dumps(mk_env(rows, of)).encode("utf-8")) + 1

    def pack_pass(of):
        rows, overflow = [], False
        for idx, (base, full) in enumerate(items):
            done = False
            for cand in (full, base):
                if size(rows + [cand], of) <= budget:
                    rows.append(cand)
                    done = True
                    break
            if not done:
                overflow = idx < n_prot
                break
        return rows, overflow

    if size([], False) > budget:
        raise BudgetError("E_BUDGET")
    rows, overflow = pack_pass(False)
    if overflow:
        rows, overflow = pack_pass(True)
        if size([], True) > budget:
            raise BudgetError("E_BUDGET")
        overflow = True   # pass 2 packs a subset of pass 1, so the protected row still fails
    assert size(rows, overflow) <= budget
    return rows, overflow


def pack(ix, R, pr, abstain, k, budget):
    order = R["order"][:k]
    n_prot = max(min(len(R["X"]), k), min(5, R["n_elig"])) if order else 0
    items = []
    for rk, d in enumerate(order):
        base = [clip(ix.ids[d], LIM["id"]), choose_anchor(ix, d, R), min(9999999, R["Sf"].get(d, 0))]
        w = why_terms(ix, R, d) if rk < 3 else []
        items.append((base, base + [w] if w else base))
    warn = []
    contra = ix.cols["contra"]
    for d in order[:3]:
        for t in contra.get(str(d), []):
            if ix.ids[t] not in warn and len(warn) < 3:
                warn.append(clip(ix.ids[t], LIM["id"]))
    rows, overflow = build_rows(items, n_prot, lambda rs, of: envelope(ix, R, pr, abstain, rs, of, warn), budget)
    return envelope(ix, R, pr, abstain, rows, overflow, warn)


ROLE_RESOLVER = {"asset": "asset", "scene": "scene", "symbol": "symbol", "section": "section", "statement": "section"}


def resolve_anchor(ix, d, anchor, resolvers):
    """Dispatch an anchor to the resolver of its role (asset -> UID sidecar/header, scene -> scene path, ...)."""
    role = "section"
    for a in ix.doc(d).get("an") or []:
        if a[0] == anchor:
            role = a[1]
    return resolvers[ROLE_RESOLVER[role]](anchor)


def route_multi(mounts, q, params=None, pr="0" * 16, intent=None, k=20, budget=BUDGET, no_abstain=False, _cache={}):
    """V2-11: per-mount ranking, S_norm = S_final*1e6 // S_max, merge by (-S_norm, priority, corpus, natural id)."""
    ms = []
    for m in sorted(mounts, key=lambda m: (m["priority"], m["corpus"], m["path"])):
        key = os.path.abspath(m["path"])
        if key not in _cache:
            _cache[key] = Index(m["path"])
        ix = _cache[key]
        Rm = rank(ix, q, params or {}, intent)
        smax = sum(Rm["vps"][t].idf for t in Rm["terms"])
        ms.append((m, ix, Rm, smax))
    norm = lambda Rm, smax, d: (Rm["Sf"].get(d, 0) * 10 ** 6) // max(1, smax)
    pre = lambda m, ix, i: "%s:%s" % (m["corpus"], ix.ids[i])
    X = [(m, ix, Rm, d, norm(Rm, smax, d)) for m, ix, Rm, smax in ms for d in Rm["X"]]
    cand = []
    for m, ix, Rm, smax in ms:
        xs = set(Rm["X"])
        for d in Rm["order"]:
            if d not in xs:
                cand.append((-norm(Rm, smax, d), m["priority"], m["corpus"], idkey(ix.ids[d]), m, ix, Rm, d))
    cand.sort(key=lambda c: c[:4])
    top = ms[0][2]
    merged = X + [(c[4], c[5], c[6], c[7], -c[0]) for c in cand]
    n_elig = sum(Rm["n_elig"] for _, _, Rm, _ in ms)
    if X:
        m0, ix0, R0, _, _ = merged[0]
        f = features(ix0, R0)
        ab = (False, "exact_id", f[0], f[1], f[2])
    elif not merged:
        ab = (True, "no_candidate" if any(Rm["T"] for _, _, Rm, _ in ms) else "no_query_term", 0, 0, 0)
    else:
        m0, ix0, R0, _, _ = merged[0]
        ab = decide(ix0, R0, no_abstain)
    n_prot = max(min(len(X), k), min(5, n_elig)) if merged else 0
    items = []
    for rk, (m, ix, Rm, d, sn) in enumerate(merged[:k]):
        base = [clip(pre(m, ix, d), LIM["id"]), choose_anchor(ix, d, Rm), min(9999999, sn)]
        w = why_terms(ix, Rm, d) if rk < 3 else []
        items.append((base, base + [w] if w else base))
    ixh = sha16("\n".join(sorted(ix.ixid for _, ix, _, _ in ms)).encode())
    env = lambda rows, of: {"schema": "ctx.shortlist.v2", "router": VERSION, "ix": ixh, "pr": pr, "q": sha16(q.encode("utf-8")),
                                  "intent": top["intent"], "abstain": {"flag": ab[0], "reason": ab[1], "m": ab[2], "cov": ab[3], "peak": ab[4]},
                                  "r": rows, **({"overflow": True} if of else {})}
    rows, overflow = build_rows(items, n_prot, env, budget)
    return env(rows, overflow)


def idkey(i):
    m = re.match(r"PM-(\d+)(?:\.(\d+))?$", i)
    return (0, int(m.group(1)), int(m.group(2) or 0), "") if m else (1, 0, 0, i)


def route(ix, q, params=None, pr="0" * 16, intent=None, k=20, budget=BUDGET, no_abstain=False):
    R = rank(ix, q, params or {}, intent)
    ab = decide(ix, R, no_abstain)
    return pack(ix, R, pr, ab, k, budget)


def load_params(path):
    if not path or not os.path.isfile(path):
        return {}, "0" * 16
    raw = open(path, "rb").read()
    j = json.loads(raw)
    keep = {k: j[k] for k in DEFAULTS if k in j}
    if keep.get("profile", "P0") not in PROFILES:
        raise ValueError("profile")
    return keep, sha16(raw)


def is_bench_path(path):
    """Refuse bench components in supplied and resolved native/Windows forms."""
    forms = (os.fspath(path), os.path.realpath(path))
    return any("bench" in form.replace("\\", "/").casefold().split("/")
               for form in forms)


def parse_mount(spec, default_priority):
    name, rest = spec.split("=", 1)
    drive, tail = ntpath.splitdrive(rest)
    path, sep, priority = tail.partition(":")
    if not name or not path:
        raise ValueError("empty mount")
    return {"corpus": name, "path": drive + path,
            "priority": int(priority) if sep else default_priority}


@contextmanager
def windows_log_lock(path, timeout=10.0):
    """Lock a stable, separate inode; never unlink it while writers may exist."""
    import errno
    import msvcrt
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
    locked = False
    deadline = time.monotonic() + timeout
    try:
        while not locked:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("router log lock timeout") from exc
                time.sleep(0.01)
        yield
    finally:
        try:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def append_log_record(path, record):
    def append():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     getattr(os, "O_BINARY", 0), 0o644)
        try:
            # Keep the Unix single O_APPEND write; a short write is never success.
            if os.write(fd, record) != len(record):
                raise OSError("incomplete router log record")
        finally:
            os.close(fd)
    if os.name == "nt":
        with windows_log_lock(path + ".lock"):
            append()
    else:
        append()


def write_log(logdir, agent, q, ans, R):
    os.makedirs(logdir, exist_ok=True)
    line = {"q_sha": sha16(q.encode("utf-8")), "terms": R["terms"][:40], "intent": ans["intent"], "ix": ans["ix"], "pr": ans["pr"],
            "r": [[r[0], r[2]] for r in ans["r"]][:20], "abstain": ans["abstain"]["flag"]}
    b = (dumps(line) + "\n").encode("utf-8")
    if len(b) > 4096:
        line["terms"] = []
        b = (dumps(line) + "\n").encode("utf-8")
    path = os.path.join(logdir, "w_%s.jsonl" % re.sub(r"[^A-Za-z0-9_-]", "_", agent))
    append_log_record(path, b)


def write_stdout_json(value):
    # External text pipes are canonical UTF-8/LF; in-memory StringIO capture
    # remains supported for the existing in-process logging oracle.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdout.write(dumps(value) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--params")
    ap.add_argument("--intent", choices=INTENTS)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--corpus", action="append", metavar="NAME=PATH[:PRIORITY]")
    ap.add_argument("--log")
    ap.add_argument("--agent", default="anon")
    ap.add_argument("--no-abstain", action="store_true")
    ap.add_argument("--verify", metavar="ROOT")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not 1 <= a.k <= 20 or a.budget < 1:
        return 2
    try:
        params, pr = load_params(a.params)
        ix = Index(a.index)
    except (IndexError_, ValueError, KeyError):
        print("index or params missing/invalid", file=sys.stderr)
        return 3
    if a.verify:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gates_v2
        if gates_v2.g_fresh(a.verify, a.index)["pass"] is not True:
            print("E_STALE", file=sys.stderr)
            return 5
    if a.corpus:
        mounts = [{"corpus": "math", "path": a.index, "priority": 0}]
        try:
            for c in a.corpus:
                mounts.append(parse_mount(c, len(mounts)))
            for m in mounts[1:]:
                Index(m["path"])
        except (ValueError, IndexError_):
            print("bad --corpus mount", file=sys.stderr)
            return 3
        try:
            write_stdout_json(route_multi(mounts, a.q, params, pr, a.intent, a.k, a.budget, a.no_abstain))
        except BudgetError:
            print("E_BUDGET", file=sys.stderr)
            return 6
        return 0
    R = rank(ix, a.q, params, a.intent)
    ab = decide(ix, R, a.no_abstain)
    try:
        ans = pack(ix, R, pr, ab, a.k, a.budget)
    except BudgetError:
        print("E_BUDGET", file=sys.stderr)
        return 6
    if a.log:
        write_log(a.log, a.agent, a.q, ans, R)
    write_stdout_json(ans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
