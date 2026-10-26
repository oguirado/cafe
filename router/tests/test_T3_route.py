import json, os, random, unittest
from decimal import Decimal
from common import *
import jsonschema_lite as JS
import build_index as V1B

SCHEMA = json.load(open(os.path.join(W, "standards", "schemas", "ctx.shortlist.v2.schema.json")))
CAL = {"m_min": 1, "tau_cov": 0, "tau_peak": 0}


def route(ix, q, params=None, **kw):
    a = R.route(ix, q, params, **kw)
    assert JS.validate(a, SCHEMA) == [], (q, JS.validate(a, SCHEMA))
    return a


class T3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d, cls.root, cls.out = build_fixture()
        cls.ix = R.Index(cls.out)

    # ---- section 3 precedence fixtures ----
    def test_i_exact_id(self):
        a = route(self.ix, "PM-001.1", CAL)
        self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"]), (False, "exact_id"))
        self.assertGreaterEqual(len(a["r"]), 1)
        self.assertEqual(a["r"][0][0], "PM-001.1")
        self.assertEqual(a["intent"], "locate_id")

    def test_ii_retired_only(self):
        a = route(self.ix, "zymurgy", CAL)
        self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"], a["r"]), (True, "no_candidate", []))

    def test_iii_nine_ids_overflow(self):
        d, root, out = build_fixture(n_synth=9)
        ix = R.Index(out)
        full = route(ix, "PM-010", CAL)
        self.assertEqual([r[0] for r in full["r"][:9]], ["PM-010.%d" % i for i in range(1, 10)])
        self.assertNotIn("overflow", full)
        small = route(ix, "PM-010", CAL, budget=420)
        self.assertTrue(small.get("overflow"))
        self.assertLess(len(small["r"]), 9)
        self.assertEqual(R.rank(ix, "PM-010", CAL)["n_elig"] >= 9, True)
        self.assertLessEqual(len(R.dumps(small).encode()) + 1, 420)
        self.assertEqual([r[0] for r in small["r"]], [r[0] for r in full["r"]][:len(small["r"])])  # match order

    def test_iv_broad_term(self):
        a = route(self.ix, "graph", {"m_min": 2, "tau_cov": 0, "tau_peak": 0})
        self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"]), (True, "low_term_match"))
        self.assertGreaterEqual(len(a["r"]), 1)
        b = route(self.ix, "graph", {"m_min": 1, "tau_cov": 0, "tau_peak": 5 * 10 ** 6})
        self.assertEqual((b["abstain"]["flag"], b["abstain"]["reason"]), (True, "below_tau"))
        self.assertGreaterEqual(len(b["r"]), 1)
        c = route(self.ix, "graph", CAL)
        self.assertEqual((c["abstain"]["flag"], c["abstain"]["reason"]), (False, "none"))

    def test_v_stopwords(self):
        a = route(self.ix, "the of and to", CAL)
        self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"], a["r"]), (True, "no_query_term", []))

    def test_vi_uncalibrated(self):
        for params in (None, {"tau_cov": None, "tau_peak": None}):
            a = route(self.ix, "paging cache", params)
            self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"]), (False, "uncalibrated"))
            self.assertGreaterEqual(len(a["r"]), 1)

    # ---- budget ----
    def test_budget_200_random(self):
        d, root, out = build_fixture(n_synth=40)
        ix = R.Index(out)
        rnd = random.Random(7)
        vocab = repo_gen.WORDS + ["paging", "graph", "cache", "eviction", "edit", "distance", "online", "the"]
        for _ in range(200):
            q = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 6)))
            Rk = R.rank(ix, q, {})
            a = route(ix, q, CAL)
            n_prot = max(min(len(Rk["X"]), 20), min(5, Rk["n_elig"])) if Rk["order"] else 0
            self.assertLessEqual(len(R.dumps(a).encode()) + 1, 2000, q)
            self.assertGreaterEqual(len(a["r"]), min(n_prot, len(Rk["order"][:20])), q)

    # ---- arithmetic ----
    def test_bm25f_hand_computed(self):
        docs = sorted(repo_gen.HAND, key=lambda d: B.idkey(d["id"]))
        subj = {1: ("Online algorithms", ["online", "competitive"]), 2: ("Graph theory", ["graph", "spectral"]), 3: ("Synthetic", ["synthetic"])}
        tk = V1B.toks
        zt = []
        for d in docs:
            z = {"title": tk(d["title"]), "purpose": tk(d["purpose"]), "trig": [w for t in d["trig"] for w in tk(t.replace("-", " "))],
                 "task": [w for t in d["tasks"] for w in tk(t)], "qa": [w for t in d["qa"] for w in tk(t)],
                 "syn": [w for t, a in d["syn"] for s in [t] + a for w in tk(s)], "kw": [w for t in d["kw"] for w in tk(t)],
                 "out": tk(d["out"]), "pre": [w for c, r in d["pre"] for s in [c] + r for w in tk(s)],
                 "subj": tk(subj[d["sid"]][0] + " " + " ".join(subj[d["sid"]][1])), "fam": tk("Family " + d["fam"]),
                 "inp": tk(" ".join(V1B.strs({"hypotheses": [{"anchor": "stmt-main", "statement": "Given a finite instance."}]})))}
            zt.append(z)
        N = len(docs)
        W0 = R.PROFILES["P0"]
        BP = B.BPPM
        q = "paging"
        df = sum(any("paging" in z[f] for f in W0) for z in zt)
        idf = int((((1 + (Decimal(N - df) + Decimal("0.5")) / (Decimal(df) + Decimal("0.5"))).ln()) * 10 ** 6).to_integral_value(rounding="ROUND_FLOOR"))
        d0 = [i for i, d in enumerate(docs) if d["id"] == "PM-001.1"][0]
        acc = 0
        for f, w in W0.items():
            tf = zt[d0][f].count("paging")
            if not tf:
                continue
            A = (1000 * sum(len(z[f]) for z in zt)) // N
            ratio = (len(zt[d0][f]) * 10 ** 9) // A
            norm = (10 ** 6 - BP[f]) + (BP[f] * ratio) // 10 ** 6
            acc += (w * tf * 10 ** 9) // norm
        want = (idf * acc) // (1200000 + acc)
        Rk = R.rank(self.ix, q, {})
        self.assertEqual(Rk["Sf"][d0], want)
        self.assertEqual(route(self.ix, q)["r"][0][2], min(9999999, want))

    def test_phrase_contiguous_only(self):
        d0 = 1  # PM-001.2 (trigger edit-distance)
        base = R.rank(self.ix, "edit distance", {})["Sf"][d0]
        ph = R.rank(self.ix, "edit distance", {"w_phr": 500})["Sf"][d0]
        self.assertGreater(ph, base)
        base2 = R.rank(self.ix, "distance edit", {})["Sf"][d0]
        ph2 = R.rank(self.ix, "distance edit", {"w_phr": 500})["Sf"][d0]
        self.assertEqual(ph2, base2)
        self.assertEqual(base, base2)

    def test_lcp5_semantics(self):
        ix = self.ix
        g = R.vposting(ix, "graphs", 1)
        e = R.vposting(ix, "graphs", 0)
        self.assertGreater(len(g.z["kw"]), len(e.z["kw"]))   # joins graph (PM-002.1) with graphs (PM-001.2)
        self.assertEqual(set(g.z["kw"]), {1, 2})
        self.assertEqual(set(e.z["kw"]), {1})
        # |t| = 4 never joins: 'edit' (trig/qa) does not see 'edits' (kw) and vice versa
        a, b = R.vposting(ix, "edit", 1), R.vposting(ix, "edits", 1)
        self.assertNotIn("kw", a.z)
        self.assertNotIn("trig", b.z)
        self.assertEqual(R.vposting(ix, "edit", 1).z, R.vposting(ix, "edit", 0).z)

    def test_thesaurus_single_ok_and_features_unexpanded(self):
        th = [{"id": "TH-001", "when": ["cache eviction"], "to": ["paging"], "provenance": {"source": "authored"}},
              {"id": "TH-002", "when": ["flushing"], "to": ["spectral"], "single_ok": True, "provenance": {"source": "authored"}},
              {"id": "TH-003", "when": ["jargon"], "to": ["paging"], "provenance": {"source": "authored"}},
              {"id": "TH-004", "when": ["alpha beta"], "to": ["aa", "bb", "cc", "dd", "ee", "ff"], "provenance": {"source": "authored"}}]
        d, root, out = build_fixture(thes=th)
        ix = R.Index(out)
        self.assertEqual(R.expansion(ix, ["cache", "eviction"]), ["paging"])
        self.assertEqual(R.expansion(ix, ["flushing"]), ["spectral"])
        self.assertEqual(R.expansion(ix, ["jargon"]), [])
        self.assertEqual(R.expansion(ix, ["cache"]), [])
        self.assertEqual(R.expansion(ix, ["alpha", "beta"]), ["aa", "bb", "cc", "dd"])   # CAP_EXP = 4
        q = "cache eviction"
        r0 = R.rank(ix, q, {"w_exp": 0})
        r5 = R.rank(ix, q, {"w_exp": 500})
        self.assertEqual(r5["exp"], ["paging"])
        self.assertNotEqual(r0["Sf"], r5["Sf"])
        for p in ({"w_exp": 0}, {"w_exp": 500}):
            a = route(ix, q, dict(p, **CAL))
            if p["w_exp"] == 0:
                f0 = a["abstain"]
            else:
                self.assertEqual({k: a["abstain"][k] for k in ("m", "cov", "peak")}, {k: f0[k] for k in ("m", "cov", "peak")})

    # ---- v2.0.1: MF-A (expansion overrides empty T) ----
    def test_expansion_overrides_empty_t(self):
        th = [{"id": "TH-002", "when": ["flushing"], "to": ["spectral"], "single_ok": True, "provenance": {"source": "authored"}}]
        d, root, out = build_fixture(thes=th)
        ix = R.Index(out)
        on = route(ix, "flushing", dict(CAL, w_exp=500))
        self.assertGreaterEqual(len(on["r"]), 1)
        self.assertEqual((on["abstain"]["flag"], on["abstain"]["reason"]), (True, "low_term_match"))
        off = route(ix, "flushing", CAL)   # same query, expansion off: no query term occurs, no rows
        self.assertEqual((off["abstain"]["reason"], off["r"]), ("no_query_term", []))
        unc = route(ix, "flushing", {"w_exp": 500})   # uncalibrated: rows and the uncalibrated reason
        self.assertEqual((unc["abstain"]["flag"], unc["abstain"]["reason"]), (False, "uncalibrated"))
        self.assertEqual(R.rank(ix, "flushing", dict(CAL, w_exp=500))["T"], [])
        self.assertEqual(route(ix, "the of and to", dict(CAL, w_exp=500))["abstain"]["reason"], "no_query_term")

    # ---- v2.0.1: MF-B (priced overflow flag, UTF-8 limits, fail closed) ----
    def test_build_rows_prices_overflow_flag(self):
        def old(items, n_prot, mk, budget):
            rows, of = [], False
            for idx, (b, f) in enumerate(items):
                done = False
                for c in (f, b):
                    if len(R.dumps(mk(rows + [c], False)).encode()) + 1 <= budget:
                        rows.append(c)
                        done = True
                        break
                if not done:
                    of = idx < n_prot
                    break
            return rows, of
        hit_old = 0
        for pad in range(0, 60):
            mk = lambda rs, of, pad=pad: dict({"p": "x" * (1900 + pad), "r": rs}, **({"overflow": True} if of else {}))
            items = [(["PM-%03d.1" % i, "pm-s1", 1],) * 2 for i in range(5)]
            for n_prot in (1, 5):
                try:
                    rows, of = R.build_rows(items, n_prot, mk, 2000)
                except R.BudgetError:
                    self.assertGreater(len(R.dumps(mk([], True)).encode()) + 1, 2000)
                    continue
                self.assertLessEqual(len(R.dumps(mk(rows, of)).encode()) + 1, 2000)
                o_rows, o_of = old(items, n_prot, mk, 2000)
                hit_old += len(R.dumps(mk(o_rows, o_of)).encode()) + 1 > 2000
        self.assertGreater(hit_old, 0)   # the sweep contains 2,001-byte-class inputs for the 2.0.0 algorithm

    def test_pack_sweep_never_exceeds_budget(self):
        d, root, out = build_fixture(n_synth=9)
        ix = R.Index(out)
        rr = R.rank(ix, "PM-010", CAL)
        ab = R.decide(ix, rr)
        n_ok = n_err = 0
        for b in range(1, 900):
            try:
                a = R.pack(ix, rr, "0" * 16, ab, 20, b)
            except R.BudgetError:
                n_err += 1
                continue
            n_ok += 1
            self.assertLessEqual(len(R.dumps(a).encode()) + 1, b, b)
        self.assertTrue(n_ok and n_err)
        self.assertEqual(R.main(["--q", "PM-010", "--index", out, "--budget", "100"]), 6)

    def test_utf8_byte_limits(self):
        for s in ("é" * 40, "漢" * 30, "a" * 80, "x" + "😀" * 20):
            for n in (24, 32, 64):
                c = R.clip(s, n)
                self.assertLessEqual(len(c.encode("utf-8")), n)
                self.assertTrue(s.startswith(c))
        self.assertEqual(R.clip("abc", 24), "abc")

    def test_why_ranks_1_3_only(self):
        d, root, out = build_fixture(n_synth=20)
        ix = R.Index(out)
        a = route(ix, "matrix tensor lattice sampling", CAL)
        self.assertGreaterEqual(len(a["r"]), 5)
        for i, r in enumerate(a["r"]):
            if i >= 3:
                self.assertEqual(len(r), 3)
        self.assertEqual(len(a["r"][0]), 4)

    def test_cli_codes(self):
        self.assertEqual(R.main(["--q", "x", "--index", "/nonexistent"]), 3)
        self.assertEqual(R.main(["--q", "x", "--index", self.out, "--k", "99"]), 2)

    def test_no_abstain_flag(self):
        a = route(self.ix, "graph", {"m_min": 2, "tau_cov": 0, "tau_peak": 0}, no_abstain=True)
        self.assertEqual((a["abstain"]["flag"], a["abstain"]["reason"]), (False, "none"))

    def test_warn_contradicts(self):
        a = route(self.ix, "paging", CAL)
        self.assertIn("PM-001.2", a.get("warn", []))


if __name__ == "__main__":
    unittest.main()
