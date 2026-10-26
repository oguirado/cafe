import glob, json, os, shutil, subprocess, sys, unittest
from common import *
import gates_v2 as G

FX = os.path.join(W, "tests", "fixtures", "gates")
CAL9 = {"m_min": 1, "tau_cov": 0, "tau_peak": 0}
DEV = [{"qid": "F1", "text": "cache eviction paging", "answerable": True, "split": "DEV", "gold": []},
       {"qid": "F2", "text": "graph partitioning spectral", "answerable": False, "split": "DEV", "gold": []}]


def devfile(d, rows=DEV):
    p = os.path.join(d, "dev.jsonl")
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def cp(out):
    d = tmp()
    dst = os.path.join(d, "ix")
    shutil.copytree(out, dst)
    return dst


class T9(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d, cls.root, cls.out = build_fixture()
        cls.dev = devfile(cls.d)
        cls.thes = os.path.join(W, "tests", "fixtures", "thesaurus_empty.json")

    # ---- positives on the clean fixture ----
    def test_clean_fixture_passes(self):
        ans = G.dev_answers(self.out, DEV, None)
        self.assertTrue(G.g_size(self.out)["pass"])
        self.assertTrue(G.g_nofloat(self.out, [a for _, a in ans])["pass"])
        self.assertTrue(G.g_schema(self.out, None, self.thes, ans)["pass"])
        self.assertTrue(G.g_anchor(self.root, self.out, ans)["pass"])
        self.assertTrue(G.g_fresh(self.root, self.out, self.thes)["pass"])
        self.assertTrue(G.g_compact(self.out, DEV)["pass"])
        self.assertTrue(G.g_determinism(self.out, DEV)["pass"])
        self.assertTrue(G.g_stdlib(os.path.join(W, "tools"))["pass"])
        self.assertTrue(G.g_thes(self.thes, self.out, self.dev)["pass"])
        self.assertTrue(G.g_calib(self.dev)["pass"])
        self.assertTrue(G.g_rebuild(self.root, self.out, self.thes)["pass"])
        self.assertTrue(G.g_blind(self.dev)["pass"])
        self.assertTrue(G.g_privacy([self.out])["pass"])

    # ---- negative fixtures: every gate must be able to fail ----
    def test_neg_nofloat(self):
        ix = cp(self.out)
        shutil.copy(os.path.join(FX, "float.json"), os.path.join(ix, "float.json"))
        self.assertFalse(G.g_nofloat(ix)["pass"])

    def test_neg_size(self):
        ix = cp(self.out)
        with open(os.path.join(ix, "big.json"), "wb") as f:
            f.write(b" " * (G.CAP + 1))
        g = G.g_size(ix)
        self.assertFalse(g["pass"])
        self.assertTrue(any("cap" in e for e in g["errors"]))
        ix2 = cp(self.out)
        m = json.load(open(os.path.join(ix2, "manifest.json")))
        m["max_file_bytes"] += 1
        json.dump(m, open(os.path.join(ix2, "manifest.json"), "w"))
        self.assertFalse(G.g_size(ix2)["pass"])

    def test_neg_anchor(self):
        d, root, out = build_fixture()
        p = glob.glob(os.path.join(root, "papers", "*PM-001.1.md"))[0]
        t = open(p, encoding="utf-8").read().replace('<a id="stmt-main"></a>', "")
        open(p, "w", encoding="utf-8").write(t)
        g = G.g_anchor(root, out)
        self.assertFalse(g["pass"])
        self.assertGreater(g["unresolved"], 0)

    def test_neg_stale(self):
        d, root, out = build_fixture()
        p = glob.glob(os.path.join(root, "papers", "*PM-002.1.md"))[0]
        open(p, "w", encoding="utf-8").write(open(p, encoding="utf-8").read().replace("cheeger-inequality", "cheeger-bound"))
        g = G.g_fresh(root, out, os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"))
        self.assertEqual((g["pass"], g["code"]), (False, "E_STALE"))

    def test_neg_answer_2001(self):
        a = json.load(open(os.path.join(FX, "answer_2001.json")))
        self.assertEqual(len(R.dumps(a).encode()) + 1, 2001)
        self.assertTrue(any("2001 B" in e for e in G.check_answer(a)))
        a2 = json.load(open(os.path.join(FX, "answer_2001.json")))
        a2["r"][0][1] = "pm-s1"
        self.assertFalse(any("B >" in e for e in G.check_answer(a2)))

    def test_neg_thes_pm_id(self):
        g = G.g_thes(os.path.join(FX, "thesaurus_pm.json"), self.out, self.dev)
        self.assertFalse(g["pass"])
        self.assertTrue(any("PM id" in e for e in g["errors"]))

    def test_neg_thes_single_ok_and_provenance(self):
        d = tmp()
        t = {"schema": "ctx.thesaurus.v1", "version": 1, "entries": [
            {"id": "TH-001", "when": ["paging"], "to": ["cache"], "single_ok": True, "provenance": {"source": "authored"}},
            {"id": "TH-002", "when": ["flushing"], "to": ["paging"], "single_ok": True, "provenance": {"source": "dev_split", "qids": ["NOPE"]}},
            {"id": "TH-003", "when": ["flushing"], "to": ["nonexistentword"], "single_ok": True, "provenance": {"source": "authored"}}]}
        p = os.path.join(d, "t.json")
        json.dump(t, open(p, "w"))
        errs = G.g_thes(p, self.out, self.dev)["errors"]
        self.assertTrue(any("TH-001" in e and "single_ok" in e for e in errs))
        self.assertTrue(any("TH-002" in e and "dev_split" in e for e in errs))
        self.assertTrue(any("TH-003" in e and "df 0" in e for e in errs))

    def test_neg_blind(self):
        d = tmp()
        os.makedirs(os.path.join(d, "bench"))
        p = devfile(os.path.join(d, "bench"))
        self.assertFalse(G.g_blind(p)["pass"])
        p2 = devfile(d, DEV + [dict(DEV[0], qid="T", split="TEST")])
        self.assertFalse(G.g_blind(p2)["pass"])
        par = os.path.join(d, "params.json")
        json.dump({"dev_sha256": "0" * 64}, open(par, "w"))
        self.assertFalse(G.g_blind(self.dev, par)["pass"])

    def test_neg_calib(self):
        d = tmp()
        p = devfile(d, [DEV[0]])
        g = G.g_calib(p)
        self.assertFalse(g["pass"])
        self.assertIn("E_CALIBRATION", g["errors"][0])
        self.assertFalse(G.g_calib(os.path.join(FX, "dev_no_unanswerable.jsonl"))["pass"])

    def test_neg_volatile_timestamp(self):
        g = G.g_volatile(self.root, self.out, [os.path.join(FX, "entry_timestamp.txt")], self.thes)
        self.assertFalse(g["pass"])
        self.assertTrue(any("volatile token" in e for e in g["errors"]))

    def test_neg_rebuild(self):
        ix = cp(self.out)
        with open(os.path.join(ix, "thes.json"), "ab") as f:
            f.write(b" ")
        self.assertFalse(G.g_rebuild(self.root, ix, self.thes)["pass"])

    def test_neg_schema(self):
        ix = cp(self.out)
        m = json.load(open(os.path.join(ix, "manifest.json")))
        del m["builder"]
        json.dump(m, open(os.path.join(ix, "manifest.json"), "w"))
        self.assertFalse(G.g_schema(ix)["pass"])
        d = tmp()
        par = os.path.join(d, "p.json")
        json.dump({"schema": "ctx.router-params.v2", "profile": "P9"}, open(par, "w"))
        self.assertFalse(G.g_schema(self.out, par)["pass"])

    def test_neg_stdlib(self):
        d = tmp()
        open(os.path.join(d, "bad.py"), "w").write("import numpy\nimport os\n")
        g = G.g_stdlib(d)
        self.assertFalse(g["pass"])
        self.assertIn("bad imports numpy", g["errors"])

    def test_neg_privacy(self):
        g = G.g_privacy([os.path.join(FX, "privacy_path.txt")])
        self.assertFalse(g["pass"])

    def test_neg_determinism(self):
        orig = R.route
        n = [0]

        def flaky(*a, **k):
            r = orig(*a, **k)
            n[0] += 1
            r["warn"] = ["X%d" % n[0]]
            return r
        R.route = flaky
        try:
            self.assertFalse(G.g_determinism(self.out, DEV)["pass"])
        finally:
            R.route = orig

    def test_neg_compact_reason_flag(self):
        a = R.route(R.Index(self.out), "paging", {})
        a["abstain"]["flag"] = True
        self.assertTrue(any("reason/flag" in e for e in G.check_answer(a)))

    def test_neg_compact_empty_reason_with_rows(self):   # MF-A: the 2.0.0 decide emitted rows under no_query_term
        a = R.route(R.Index(self.out), "paging", {})
        a["abstain"].update(reason="no_query_term", flag=True)
        self.assertTrue(any("rows on empty-reason" in e for e in G.check_answer(a)))

    def test_neg_compact_multibyte_anchor(self):   # MF-B: 40 characters = 80 bytes pass schema maxLength 64
        a = json.load(open(os.path.join(FX, "answer_mb_anchor.json")))
        self.assertTrue(max(len(r[1]) for r in a["r"]) <= 64 < max(len(r[1].encode()) for r in a["r"]))
        self.assertTrue(any("UTF-8 byte limit" in e for e in G.check_answer(a)))
        a["r"][0][1] = "\u00e9" * 32   # 64 bytes
        self.assertFalse(any("UTF-8 byte limit" in e for e in G.check_answer(a)))

    def test_precedence_cases_pass(self):
        self.assertEqual(G.run_precedence_cases(), [])

    def test_neg_precedence_old_decide(self):   # discriminating: the 2.0.0 empty-T rule fails the new fixture
        orig = R.decide

        def old(ix, Rr, no_abstain=False):
            if not Rr["X"] and not Rr["T"]:
                return True, "no_query_term", 0, 0, 0
            return orig(ix, Rr, no_abstain)
        R.decide = old
        try:
            errs = G.run_precedence_cases()
        finally:
            R.decide = orig
        self.assertTrue(any(e.startswith("vii_expansion_overrides_empty_t") for e in errs), errs)

    def test_neg_compact_why_qhash_order(self):
        base = R.route(R.Index(self.out), "paging cache", CAL9)
        self.assertEqual(G.check_answer(base, "paging cache"), [])
        a = json.loads(R.dumps(base))
        while len(a["r"]) < 4:
            a["r"].append(["PM-900.1", "pm-s1", 1])
        a["r"][3] = a["r"][3][:3] + [["x"]]
        self.assertTrue(any("why beyond rank 3" in e for e in G.check_answer(a, "paging cache")))
        self.assertTrue(any("q hash" in e for e in G.check_answer(base, "other query")))
        b = json.loads(R.dumps(base))
        b["r"] = [["PM-900.1", "pm-s1", 1], ["PM-900.2", "pm-s1", 5]]
        self.assertTrue(any("scores not non-increasing" in e for e in G.check_answer(b, "paging cache")))

    def test_cli_all_on_fixture(self):
        d = tmp()
        o = os.path.join(d, "g.json")
        rc = G.main(["--all", "--root", self.root, "--index", self.out, "--dev", self.dev, "--thesaurus", self.thes, "--out", o])
        j = json.load(open(o))
        self.assertEqual((rc, j["passed"], j["total"]), (0, 14, 14), {k: v["errors"] for k, v in j["gates"].items() if not v["pass"]})


if __name__ == "__main__":
    unittest.main()
