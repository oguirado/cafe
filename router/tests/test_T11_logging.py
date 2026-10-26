import glob, hashlib, json, os, subprocess, sys, unittest
from common import *
import merge_logs

WORKER = r"""
import io, random, sys, contextlib
sys.path.insert(0, %(tools)r)
import route_v2 as R
agent, ix, logd, n, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
rnd = random.Random(seed)
words = %(words)r
for _ in range(n):
    q = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 5)))
    with contextlib.redirect_stdout(io.StringIO()):
        assert R.main(["--q", q, "--index", ix, "--log", logd, "--agent", agent]) == 0
"""


class T11(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d, cls.root, cls.out = build_fixture(n_synth=20)

    def spawn(self, jobs, logd):
        code = WORKER % {"tools": os.path.join(W, "tools"), "words": repo_gen.WORDS + ["paging", "graph", "cache", "the"]}
        ps = [subprocess.Popen([sys.executable, "-c", code, a, self.out, logd, str(n), str(s)]) for a, n, s in jobs]
        for p in ps:
            self.assertEqual(p.wait(), 0)

    def test_8_agents_x_200_queries(self):
        logd = os.path.join(self.d, "spool")
        self.spawn([("agent%d" % i, 200, i) for i in range(8)], logd)
        self.assertEqual(len(glob.glob(os.path.join(logd, "w_*.jsonl"))), 8)
        j1, j2 = os.path.join(self.d, "logs", "journal.jsonl"), os.path.join(self.d, "logs", "journal2.jsonl")
        self.assertEqual(merge_logs.main(["--spool", logd, "--out", j1]), 0)
        self.assertEqual(merge_logs.main(["--spool", logd, "--out", j2]), 0)
        lines = open(j1, encoding="utf-8").read().split("\n")
        self.assertEqual(lines[-1], "")
        lines = lines[:-1]
        self.assertEqual(len(lines), 1600)
        for l in lines:
            o = no_float(l)
            self.assertEqual(sorted(o), ["abstain", "intent", "ix", "pr", "q_sha", "r", "terms"])
            self.assertLessEqual(len(l.encode()) + 1, 4096)
        self.assertEqual(open(j1, "rb").read(), open(j2, "rb").read())
        # ordered by (agent, line)
        per = [json.loads(l)["q_sha"] for l in lines[:200]]
        own = [json.loads(l)["q_sha"] for l in open(os.path.join(logd, "w_agent0.jsonl"), encoding="utf-8")]
        self.assertEqual(per, own)

    def test_shared_file_appends_do_not_interleave(self):
        logd = os.path.join(self.d, "spool_shared")
        self.spawn([("same", 150, 1), ("same", 150, 2), ("same", 150, 3)], logd)
        ls = open(os.path.join(logd, "w_same.jsonl"), encoding="utf-8").read().split("\n")[:-1]
        self.assertEqual(len(ls), 450)
        for l in ls:
            json.loads(l)

    def test_log_line_content(self):
        logd = os.path.join(self.d, "spool_c")
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as f:
            R.main(["--q", "cache eviction paging", "--index", self.out, "--log", logd, "--agent", "x/y z"])
        rec = json.loads(open(os.path.join(logd, "w_x_y_z.jsonl")).read())
        ans = json.loads(f.getvalue())
        self.assertEqual(rec["q_sha"], ans["q"])
        self.assertEqual(rec["r"][0][0], ans["r"][0][0])
        self.assertEqual(rec["abstain"], ans["abstain"]["flag"])

    def test_merge_fails_closed_on_corrupt_line(self):
        d = tmp()
        open(os.path.join(d, "w_a.jsonl"), "w").write('{"a":1}\n{broken\n')
        self.assertEqual(merge_logs.main(["--spool", d, "--out", os.path.join(d, "j.jsonl")]), 5)


if __name__ == "__main__":
    unittest.main()
