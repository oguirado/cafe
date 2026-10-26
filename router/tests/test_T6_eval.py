import json, math, os, unittest
from common import *
import eval_v2 as E

BENCH = [
    {"qid": "Q1", "text": "q1", "answerable": True, "class": "ALG", "split": "DEV", "gold": [{"id": "A", "grade": 3}, {"id": "B", "grade": 2}, {"id": "C", "grade": 1}],
     "adjudication": {"a1": {"A": 3, "B": 2, "C": 1, "D0": 0}}},
    {"qid": "Q2", "text": "q2", "answerable": True, "class": "DS", "split": "DEV", "gold": [{"id": "D", "grade": 3}], "adjudication": {"a1": {"D": 3}}},
    {"qid": "Q3", "text": "q3", "answerable": False, "class": "ALG", "split": "DEV", "gold": [], "adjudication": {"a1": {}}},
]
OUT = {"q1": (["X", "A", "Y", "Q", "R"], False), "q2": (["D", "E"], False), "q3": (["Z"], True)}


class Stub:
    def run(self, name, q):
        ids, ab = OUT[q]
        return ids, ids, ab, 1000


class T6(unittest.TestCase):
    def test_metrics_known_answers(self):
        res, pqs = E.evaluate(BENCH, Stub(), ["s"])
        m = res["s"]["metrics"]
        self.assertEqual(m["quality"]["n"], 2)
        self.assertAlmostEqual(m["quality"]["recall_at_1"], 0.5)           # Q1 0, Q2 1
        self.assertAlmostEqual(m["quality"]["recall_at_5"], 0.75)          # Q1 1/2, Q2 1
        self.assertAlmostEqual(m["quality"]["mrr"], 0.75)                  # Q1 1/2, Q2 1
        dcg1 = 7 / math.log2(3)                                              # A (grade 3) at rank 2
        idcg1 = 7 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
        self.assertAlmostEqual(m["quality"]["ndcg_at_5"], round((dcg1 / idcg1 + 1.0) / 2, 4), places=4)
        self.assertEqual(m["abstain"], {"precision": 1.0, "recall": 1.0, "f1": 1.0, "pass_through": 1.0, "n_unanswerable": 1,
                                        "n_answerable": 2, "n_abstained": 1})
        self.assertEqual(m["answer_bytes"]["share_le_2000"], 1.0)

    def test_brackets_raw_and_all_relevant(self):
        res, _ = E.evaluate(BENCH, Stub(), ["s"])
        b = res["s"]["metrics"]["bracket"]
        self.assertEqual(b["recall_at_5"], [0.75, round((5 / 6 + 1) / 2, 4)])
        self.assertEqual(b["mrr"], [0.75, 1.0])
        self.assertEqual(res["s"]["metrics"]["unjudged_top5"], {"docs": 4 + 1, "queries": 2})   # X,Y,Q,R and E

    def test_permutation(self):
        self.assertEqual(E.sign_flip_p([0.0, 0.0, 0.0]), 1.0)
        self.assertEqual(E.sign_flip_p([1, 1, 1]), 0.25)
        self.assertEqual(E.sign_flip_p([1, -1]), 1.0)
        _, pqs = E.evaluate(BENCH, Stub(), ["s", "t"])
        self.assertEqual(E.perm_p(pqs, BENCH, "s", "t", "r5"), 1.0)
        self.assertEqual(E.sign_flip_p([0.1] * 25), E.sign_flip_p([0.1] * 25))   # seeded, repeatable

    def _bench(self, d, split):
        p = os.path.join(d, "b.jsonl")
        with open(p, "w") as f:
            r = dict(BENCH[0], split=split, text="cache eviction paging")
            f.write(json.dumps(r) + "\n")
        return p

    def test_dev_refuses_bench_path(self):
        d = tmp()
        os.makedirs(os.path.join(d, "bench"))
        p = os.path.join(d, "bench", "x.jsonl")
        open(p, "w").write("{}\n")
        self.assertEqual(E.main(["--bench", p, "--split", "DEV", "--out", os.path.join(d, "r.json")]), 4)

    def test_test_refusals_and_lock(self):
        d = tmp()
        p = self._bench(d, "TEST")
        out = os.path.join(d, "run_TEST.json")
        self.assertEqual(E.main(["--bench", p, "--split", "TEST", "--out", out]), 4)           # no freeze
        E.write_freeze(os.path.join(d, "freeze.json"))
        open(os.path.join(d, "run_TEST.lock"), "w").write("x")
        self.assertEqual(E.main(["--bench", p, "--split", "TEST", "--out", out]), 4)           # lock present
        os.remove(os.path.join(d, "run_TEST.lock"))
        self.assertEqual(E.main(["--bench", p, "--split", "TEST", "--out", out, "--systems", "v2-default,b1"]), 0)
        self.assertTrue(os.path.exists(os.path.join(d, "run_TEST.lock")))
        self.assertEqual(E.main(["--bench", p, "--split", "TEST", "--out", out]), 4)           # second run refused
        run = json.load(open(out))
        self.assertEqual(run["split"], "TEST")

    def test_frozen_change_detected(self):
        d = tmp()
        fz = os.path.join(d, "freeze.json")
        E.write_freeze(fz)
        j = json.load(open(fz))
        k = sorted(j["files"])[0]
        j["files"][k] = "0" * 64
        json.dump(j, open(fz, "w"))
        self.assertTrue(E.check_freeze(fz).startswith("E_FROZEN_CHANGED"))

    def test_run_schema(self):
        import jsonschema_lite as JS
        d = tmp()
        p = self._bench(d, "DEV")
        out = os.path.join(d, "r.json")
        self.assertEqual(E.main(["--bench", p, "--split", "DEV", "--params", "none", "--out", out, "--systems", "v2,v2-default,b1,v1"]), 0)
        run = json.load(open(out))
        sc = json.load(open(os.path.join(W, "standards", "schemas", "ctx.bench-run.v2.schema.json")))
        self.assertEqual(JS.validate(run, sc), [])
        self.assertEqual(run["systems"]["v2"]["metrics"]["quality"], run["systems"]["v2-default"]["metrics"]["quality"])


if __name__ == "__main__":
    unittest.main()
