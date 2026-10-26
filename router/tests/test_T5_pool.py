import json, os, re, subprocess, sys, unittest
from common import *
import make_pool_v2 as P
import dev_overlap_check as O
import build_index as V1B


class T5(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d, cls.root, cls.out = build_fixture(n_synth=12)
        cls.v1 = os.path.join(cls.d, "v1ix")
        os.makedirs(cls.v1)
        V1B.build(cls.root, cls.v1, os.path.join(W, "v1_frozen", "vocabulary.json"))
        cls.src = os.path.join(cls.d, "src")
        os.makedirs(cls.src)
        row = {"qid": "Q-X-01", "text": "cache eviction paging policy", "answerable": True, "class": "ALG", "source": "authored_blind", "split": "TEST",
               "gold": [{"id": "PM-001.1", "grade": 3}], "adjudication": {"a1": {"PM-001.1": 3, "PM-001.2": 0}}}
        cls.jsonl = os.path.join(cls.src, "b.jsonl")
        open(cls.jsonl, "w").write(json.dumps(row) + "\n")
        cls.tsv = os.path.join(cls.src, "q.tsv")
        open(cls.tsv, "w").write("R01\trouter\tgraph spectral partitioning\nH01\thyperbooks\tmatrix tensor lattice\n")
        cls.tsv2 = os.path.join(cls.src, "m.tsv")
        open(cls.tsv2, "w").write("M01\tmemory\tonline paging cache\n")
        cls.pool = os.path.join(cls.d, "pool")
        assert P.main(["--dev-src", cls.jsonl, "--dev-src", cls.tsv, "--dev-src", cls.tsv2, "--index", cls.out, "--v1-index", cls.v1,
                       "--out", cls.pool]) == 0
        cls.meta = json.load(open(os.path.join(cls.pool, "pool.json")))

    def test_qids_and_sources(self):
        q = self.meta["queries"]
        self.assertEqual(sorted(q), ["Q-X-01", "W-H01", "W-M01", "W-R01"])
        self.assertEqual(q["W-R01"]["source"], "w11_brainstorm")
        self.assertEqual(q["W-R01"]["source_ref"], "q.tsv:R01")
        self.assertFalse(q["W-R01"]["labelled"])
        self.assertTrue(q["Q-X-01"]["labelled"])

    def test_graded_pairs_removed(self):
        c = self.meta["queries"]["Q-X-01"]
        self.assertNotIn("PM-001.1", c["candidates"])
        self.assertNotIn("PM-001.2", c["candidates"])
        self.assertGreaterEqual(c["n_already_graded"], 1)

    def test_sheets_blind_sorted_digest(self):
        for qid, c in self.meta["queries"].items():
            p = os.path.join(self.pool, "sheet_%s.md" % qid)
            if not c["candidates"]:
                self.assertFalse(os.path.exists(p))
                continue
            t = open(p, encoding="utf-8").read()
            ids = re.findall(r"^## (PM-\S+)$", t, re.M)
            self.assertEqual(ids, c["candidates"])
            self.assertEqual(ids, sorted(ids, key=B.idkey))
            for forbidden in ("v1", "b1", "v2", "router", "rank", "score"):
                self.assertNotIn(forbidden, t.split("Task:")[0].lower())
            self.assertEqual(t.count("- title:"), len(ids))
            self.assertLessEqual(max([t.count("- statement:")] + [0]), 2 * len(ids))
            for blk in t.split("## ")[1:]:
                self.assertLessEqual(len(re.findall(r"^- statement:", blk, re.M)), 2)
                tr = re.search(r"^- triggers: (.*)$", blk, re.M).group(1)
                self.assertLessEqual(len([x for x in tr.split("; ") if x and x != "-"]), 6)
        t = open(os.path.join(self.pool, "sheet_W-R01.md"), encoding="utf-8").read()
        self.assertTrue(t.startswith("# Candidate sheet W-R01"))

    def test_batches_and_instructions(self):
        bs = json.load(open(os.path.join(self.pool, "batches.json")))
        self.assertTrue(bs)
        for b in bs:
            self.assertLessEqual(b["bytes"], 90 * 1024)
            body = open(os.path.join(self.pool, "batches", b["batch"] + ".md"), encoding="utf-8").read()
            self.assertIn("Annotator instructions", body)
            for q in b["qids"]:
                self.assertIn("# Candidate sheet %s" % q, body)
        ins = open(os.path.join(self.pool, "ANNOTATOR_INSTRUCTIONS.md"), encoding="utf-8").read()
        for c in P.CLASSES:
            self.assertIn(c, ins)
        self.assertNotIn("TEST", ins)

    def test_pool_systems_keep_attribution_out_of_sheets(self):
        sy = json.load(open(os.path.join(self.pool, "pool_systems.json")))
        self.assertEqual(sorted(sy["W-R01"]), ["b1", "v1", "v2_p0_off", "v2_p1_exp_phr_lcp5", "v2_p2_dom_intent"])
        union = set(i for v in sy["W-R01"].values() for i in v)
        self.assertEqual(union, set(self.meta["queries"]["W-R01"]["candidates"]))

    def test_deterministic(self):
        d2 = os.path.join(self.d, "pool2")
        P.main(["--dev-src", self.jsonl, "--dev-src", self.tsv, "--dev-src", self.tsv2, "--index", self.out, "--v1-index", self.v1, "--out", d2])
        for f in ("pool.json", "pool_systems.json", "batches.json", "sheet_W-R01.md"):
            self.assertEqual(open(os.path.join(self.pool, f), "rb").read(), open(os.path.join(d2, f), "rb").read())

    def test_refuses_bench_path(self):
        os.makedirs(os.path.join(self.d, "bench"), exist_ok=True)
        p = os.path.join(self.d, "bench", "x.jsonl")
        open(p, "w").write("{}\n")
        self.assertEqual(P.main(["--dev-src", p, "--index", self.out, "--v1-index", self.v1, "--out", os.path.join(self.d, "o")]), 4)

    def test_overlap_check_prints_no_test_text(self):
        dev = os.path.join(self.d, "dev.jsonl")
        open(dev, "w").write(json.dumps({"qid": "D1", "text": "Cache  Eviction policy"}) + "\n" + json.dumps({"qid": "D2", "text": "other"}) + "\n")
        test = os.path.join(self.d, "test.jsonl")
        open(test, "w").write(json.dumps({"qid": "T1", "text": "cache eviction POLICY"}) + "\n" + json.dumps({"qid": "T2", "text": "SECRETTESTTEXT"}) + "\n")
        r = subprocess.run([sys.executable, os.path.join(W, "tools", "dev_overlap_check.py"), "--dev", dev, "--test", test], capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout), {"shared": 1, "qids": ["D1"]})
        self.assertNotIn("SECRETTESTTEXT", r.stdout + r.stderr)
        self.assertNotIn("T1", r.stdout)
        tsv = os.path.join(self.d, "t.tsv")
        open(tsv, "w").write("T9\tx\tother\n")
        self.assertEqual(O.check(dev, tsv), {"shared": 1, "qids": ["D2"]})


class T5Merge(unittest.TestCase):
    def test_kappa_known(self):
        self.assertEqual(P.kappa([(1, 1), (0, 0), (1, 0), (0, 1)]), 0.0)
        self.assertEqual(P.kappa([(1, 1), (0, 0), (1, 1), (0, 0)]), 1.0)
        self.assertIsNone(P.kappa([(0, 0), (0, 0)]))

    def test_final_grade_rule(self):
        self.assertEqual(P.final_grade(3, 2, None), 2)
        self.assertEqual(P.final_grade(0, 1, None), 0)
        self.assertIsNone(P.final_grade(1, 2, None))
        self.assertEqual(P.final_grade(1, 2, 3), 3)

    def test_qid_aliases(self):
        pool = {"W-H09": 1, "W-M09": 1, "W-R13": 1, "Q-ALG-01": 1}
        self.assertEqual([P.norm_qid(x, pool) for x in ("Q-W-H09", "Q-M09", "R13", "Q-ALG-01", "H99")],
                         ["W-H09", "W-M09", "W-R13", "Q-ALG-01", None])


if __name__ == "__main__":
    unittest.main()
