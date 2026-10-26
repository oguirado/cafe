import glob, json, os, random, subprocess, sys, unittest
from decimal import Decimal
from common import *
import gates_v2
import build_index_v2 as B2
import build_index as V1B


def tree(d):
    return {os.path.relpath(os.path.join(a, f), d): open(os.path.join(a, f), "rb").read() for a, _, fs in os.walk(d) for f in fs}


class T2(unittest.TestCase):
    def test_toks_equal_v1(self):
        for s in ["Online \\alpha paging, edit-distance of K-server 3D", "The quick brown fox: ünïcode ß", "x y zz PM-001.1"]:
            self.assertEqual(B2.toks(s), V1B.toks(s))
        self.assertEqual(B2.STOP, V1B.STOP)

    def test_byte_identical_under_perturbation(self):
        d, root, out = build_fixture(n_synth=12)
        a = tree(out)
        real = os.listdir
        rnd = random.Random(1)

        def shuf(p="."):
            l = list(real(p))
            rnd.shuffle(l)
            return l
        os.listdir = shuf
        old_tz, old_cwd = os.environ.get("TZ"), os.getcwd()
        try:
            os.environ["TZ"] = "Asia/Tokyo"
            os.chdir("/")
            out2 = os.path.join(d, "ix2")
            B.build(root, out2, os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"))
        finally:
            os.listdir = real
            os.chdir(old_cwd)
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
        self.assertEqual(a, tree(out2))
        rc = subprocess.run([sys.executable, os.path.join(W, "tools", "build_index_v2.py"), "--root", root, "--out", out,
                             "--thesaurus", os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"), "--check"], capture_output=True, text=True)
        self.assertEqual((rc.returncode, rc.stdout.strip()), (0, "byte-identical"))

    def test_forced_tiny_cap_split_and_same_answers(self):
        d, root, out = build_fixture(n_synth=40)
        small = os.path.join(d, "small")
        cap = 2500
        B.build(root, small, os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"), cap=cap)
        cols = json.load(open(os.path.join(small, "cols.json")))
        self.assertTrue(cols["shards"], "split path not exercised")
        for name, fs in cols["shards"].items():
            for first, fn in fs:
                self.assertLessEqual(os.path.getsize(os.path.join(small, fn)), cap, fn)
                self.assertTrue(json.load(open(os.path.join(small, fn))))
        for f in glob.glob(os.path.join(small, "post", "*.json")) + glob.glob(os.path.join(small, "phr", "*.json")):
            self.assertLessEqual(os.path.getsize(f), cap)
        ia, ib = R.Index(out), R.Index(small)
        for q in ["matrix lattice", "graph paging", "tensor sampling ordering", "online cache eviction", "graphs"]:
            for params in ({}, {"lcp5": 1, "w_phr": 500}):
                a = R.route(ia, q, params)
                b = R.route(ib, q, params)
                a.pop("ix"), b.pop("ix")
                self.assertEqual(a, b, q)

    def test_idf_hand_computed(self):
        d, root, out = build_fixture()
        N = 4
        post = json.load(open(os.path.join(out, "post", "pa.json")))
        e = post["paging"]
        # 'paging' occurs in PM-001.1 only (title/trig? 'online-paging' trig, kw, syn, purpose)
        df = 1
        want = int(((1 + (Decimal(N - df) + Decimal("0.5")) / (Decimal(df) + Decimal("0.5"))).ln() * 10 ** 6).to_integral_value(rounding="ROUND_FLOOR"))
        self.assertEqual((e["d"], e["i"]), (df, want))

    def test_no_float_anywhere(self):
        d, root, out = build_fixture(n_synth=10)
        for p in glob.glob(os.path.join(out, "**", "*"), recursive=True):
            if os.path.isfile(p) and p.endswith((".json", ".jsonl")):
                for line in open(p, encoding="utf-8").read().split("\n"):
                    if line.strip():
                        no_float(line)

    def test_schema_unknown(self):
        root = repo_gen.make_repo(os.path.join(tmp(), "repo"), extra_block="pm.search.v9")
        with self.assertRaises(B.BuildError) as c:
            B.build(root, os.path.join(tmp(), "ix"), None)
        self.assertEqual(c.exception.code, "E_SCHEMA_UNKNOWN")
        rc = subprocess.run([sys.executable, os.path.join(W, "tools", "build_index_v2.py"), "--root", root, "--out", os.path.join(tmp(), "ix")],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 3)

    def test_stale_after_edit(self):
        d, root, out = build_fixture()
        self.assertTrue(gates_v2.g_fresh(root, out, os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"))["pass"])
        p = glob.glob(os.path.join(root, "papers", "*PM-001.1.md"))[0]
        t = open(p, encoding="utf-8").read().replace("cache-eviction", "cache-flushing")
        open(p, "w", encoding="utf-8").write(t)
        g = gates_v2.g_fresh(root, out, os.path.join(W, "tests", "fixtures", "thesaurus_empty.json"))
        self.assertFalse(g["pass"])
        self.assertEqual(g["code"], "E_STALE")
        self.assertTrue(any(k.endswith("#pm.blocks") for k in g["stale"]))
        self.assertEqual(R.main(["--q", "x", "--index", out]), 0)


if __name__ == "__main__":
    unittest.main()
