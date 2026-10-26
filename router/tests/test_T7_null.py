import json, os, random, unittest
from collections import Counter
from common import *
import null_edges as N


def degs(per):
    out = {}
    for rc, pairs in per.items():
        if rc in N.SYM:
            c = Counter(x for p in pairs for x in p)
            out[rc] = (dict(c),)
        else:
            out[rc] = (dict(Counter(a for a, _ in pairs)), dict(Counter(b for _, b in pairs)))
    return out


class T7(unittest.TestCase):
    def graph(self):
        rnd = random.Random(3)
        per = {1: set(), 3: set(), 0: set()}
        while len(per[1]) < 40:
            a, b = rnd.randrange(30), rnd.randrange(30)
            if a != b:
                per[1].add((a, b))
        while len(per[3]) < 25:
            a, b = rnd.randrange(30), rnd.randrange(30)
            if a != b:
                per[3].add((a, b))
        while len(per[0]) < 30:
            a, b = rnd.randrange(30), rnd.randrange(30)
            if a != b:
                per[0].add((min(a, b), max(a, b)))
        return {k: sorted(v) for k, v in per.items()}

    def test_rewire_preserves_degrees(self):
        per = self.graph()
        for seed in (0, 1, 199):
            rp, acc = N.rewire(per, seed)
            self.assertGreater(acc, 0)
            self.assertEqual(degs(per), degs(rp))
            for rc, pairs in rp.items():
                self.assertEqual(len(pairs), len(set(pairs)))             # no duplicates
                self.assertTrue(all(a != b for a, b in pairs))            # no self-loops
                self.assertEqual(len(pairs), len(per[rc]))
                if rc in N.SYM:
                    self.assertTrue(all(a < b for a, b in pairs))
            self.assertNotEqual(rp, per)

    def test_deterministic_and_expand_roundtrip(self):
        per = self.graph()
        self.assertEqual(N.rewire(per, 5), N.rewire(per, 5))
        self.assertNotEqual(N.rewire(per, 5)[0], N.rewire(per, 6)[0])
        self.assertEqual(N.split_relations(N.expand(per)), per)

    def test_p_formula(self):
        self.assertEqual(N.p_value(0), (1, 201))
        self.assertEqual(N.p_value(200), (201, 201))
        self.assertEqual(N.p_value(9), (10, 201))

    def test_refuses_blind_inputs(self):
        d = tmp()
        os.makedirs(os.path.join(d, "bench"))
        p = os.path.join(d, "bench", "x.jsonl")
        open(p, "w").write("{}\n")
        self.assertEqual(N.main(["--index", os.path.join(W, "out", "index"), "--bench", p, "--out", os.path.join(d, "o.json")]), 4)


if __name__ == "__main__":
    unittest.main()
