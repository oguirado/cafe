import json, os, tempfile, unittest
from fractions import Fraction as F
from common import *
import tune_v2 as T

D = dict(profile="P0", w_exp=0, w_phr=0, lcp5=0, w_dom=0, intent_boost=0, w_edge=0)
ORDER = ["profile", "w_exp", "w_phr", "lcp5", "w_dom", "intent_boost", "w_edge"]


def cfg(**k):
    return dict(D, **k)


class T8(unittest.TestCase):
    def test_x2_single_threshold(self):
        # HB fixture: answerable {0.1, 0.9}, unanswerable {0.2, 0.8} on cov (ppm); peak/m uninformative
        rows = [(False, False, 2, 100000, 10 ** 9, True), (False, False, 2, 900000, 10 ** 9, True),
                (False, False, 2, 200000, 10 ** 9, False), (False, False, 2, 800000, 10 ** 9, False)]
        fit = T.fit_abstention(rows, floor=F(1, 2))
        self.assertEqual((fit["m_min"], fit["tau_cov"], fit["tau_peak"]), (1, 850000, 0))   # floor 1/2: cov<0.85 abstains on 0.1(ans), 0.2, 0.8 -> F1 4/5
        self.assertEqual((fit["tp"], fit["abstained_answerable"]), (2, 1))
        fit2 = T.fit_abstention(rows)                                                      # floor 0.8 forces no abstention on answerable
        self.assertEqual((fit2["tau_cov"], fit2["tau_peak"], fit2["abstained_answerable"]), (0, 0, 0))

    def test_x2_floor_unreachable_gives_none_and_calibration(self):
        rows = [(True, False, 0, 0, 0, True), (False, False, 2, 5, 5, False)]
        self.assertIsNone(T.fit_abstention(rows))
        with self.assertRaises(ValueError):
            T.fit_abstention([(False, False, 1, 1, 1, True)])

    def test_parsimony_all_equal_picks_default(self):
        cs = [cfg(), cfg(w_exp=250), cfg(profile="P1"), cfg(w_dom=100)]
        tab = [(c, {"a": F(1, 2), "b": F(1, 2)}) for c in cs]
        ch, rep = T.select(tab, {"a": "X", "b": "Y"}, D, ORDER, F(1, 4), F(-1, 20))
        self.assertEqual(ch, D)

    def test_promotion_rejects_class_regression(self):
        cs = [cfg(), cfg(w_exp=250), cfg(w_exp=500)]
        tab = [(cs[0], {"a": F(1, 2), "b": F(1, 2)}),
               (cs[1], {"a": F(1, 1), "b": F(1, 2) - F(6, 100)}),       # class Y at -0.06 -> rejected
               (cs[2], {"a": F(7, 10), "b": F(74, 100)})]          # same J, accepted next
        ch, rep = T.select(tab, {"a": "X", "b": "Y"}, D, ORDER, F(1, 100), F(-1, 20))
        self.assertEqual(ch, cs[2])
        self.assertEqual(len(rep["trail"]), 2)

    def test_guards(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bench")
            os.makedirs(p)
            f = os.path.join(p, "x.jsonl")
            open(f, "w").write("{}\n")
            self.assertEqual(T.main(["--index", d, "--bench", f]), 4)
            g = os.path.join(d, "t.jsonl")
            open(g, "w").write(json.dumps({"split": "TEST", "answerable": True}) + "\n")
            self.assertEqual(T.main(["--index", d, "--bench", g]), 4)

    def test_grid_size(self):
        g = json.load(open(os.path.join(W, "standards", "grid.json")))
        self.assertEqual(len(T.configs(g, False)), 216)
        self.assertEqual(len(T.configs(g, True)), 432)


if __name__ == "__main__":
    unittest.main()
