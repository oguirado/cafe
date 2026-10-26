import json, os, random, subprocess, sys, unittest
from common import *
import jsonschema_lite as JS

MN = os.path.join(W, "tests", "fixtures", "mounts")
NODE_SCHEMA = json.load(open(os.path.join(W, "standards", "schemas", "ctx.node.v2.schema.json")))
SHORT = json.load(open(os.path.join(W, "standards", "schemas", "ctx.shortlist.v2.schema.json")))


def build_nodes(d, corpus):
    out = os.path.join(d, corpus)
    B.build(None, out, None, nodes=os.path.join(MN, corpus, "nodes.jsonl"), corpus=corpus)
    return out


class T10(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tmp()
        cls.sw = build_nodes(cls.d, "sw")
        cls.vg = build_nodes(cls.d, "vg")
        _, _, cls.math = build_fixture(n_synth=40)
        cls.mounts = [{"corpus": "math", "path": cls.math, "priority": 0}, {"corpus": "sw", "path": cls.sw, "priority": 1},
                      {"corpus": "vg", "path": cls.vg, "priority": 2}]

    def test_fixture_nodes_valid_and_build_cli(self):
        for c in ("sw", "vg"):
            rows = [json.loads(l) for l in open(os.path.join(MN, c, "nodes.jsonl"))]
            self.assertEqual(len(rows), 20)
            for r in rows:
                self.assertEqual(JS.validate(r, NODE_SCHEMA), [])
            self.assertEqual(sum(a["role"] == "asset" and a["a"] == "uid://example0001" for r in rows for a in r["anchors"]), 1)
            self.assertEqual(sum(a["role"] == "scene" for r in rows for a in r["anchors"]), 1)
        o = os.path.join(self.d, "cli")
        for _ in range(2):
            rc = subprocess.run([sys.executable, os.path.join(W, "tools", "build_index_v2.py"), "--nodes", os.path.join(MN, "sw", "nodes.jsonl"),
                                 "--corpus", "sw", "--out", o], capture_output=True)
            self.assertEqual(rc.returncode, 0)
        chk = subprocess.run([sys.executable, os.path.join(W, "tools", "build_index_v2.py"), "--nodes", os.path.join(MN, "sw", "nodes.jsonl"),
                              "--corpus", "sw", "--out", o, "--check"], capture_output=True, text=True)
        self.assertEqual(chk.stdout.strip(), "byte-identical")
        man = json.load(open(os.path.join(self.sw, "manifest.json")))
        self.assertEqual((man["corpus"], man["nodes"]), ("sw", 20))
        self.assertEqual(JS.validate(man, json.load(open(os.path.join(W, "standards", "schemas", "ctx.index-manifest.v2.schema.json")))), [])

    def _mount(self, corpus, n, special):
        import hashlib
        rows = []
        for k in range(n):
            t = special.get(k, "filler%d" % k)
            rows.append({"schema": "ctx.node.v2", "id": "%s-%03d" % (corpus.upper(), k), "corpus": corpus, "kind": "symbol", "title": t.title(),
                         "purpose": "Synthetic node %d." % k, "anchors": [{"a": "a%d" % k, "role": "section"}],
                         "search": {"triggers": [t.replace(" ", "-")], "keywords": [w for w in t.split()]}, "status": "active",
                         "source_sha256": hashlib.sha256(str(k).encode()).hexdigest()})
        p = os.path.join(self.d, corpus + ".jsonl")
        with open(p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = os.path.join(self.d, "ix_" + corpus)
        B.build(None, out, None, nodes=p, corpus=corpus)
        return out

    def test_small_mount_max_doc_outranks_half_score_big_doc(self):
        # small mount: node 0 matches both query terms (S close to S_max). big mount: best doc matches one term with a much rarer
        # (higher idf) term, so its RAW score is larger but its S_norm is about half.
        small = self._mount("small", 20, {0: "alpha beta", 1: "alpha", 2: "alpha", 3: "beta", 4: "beta"})
        big = self._mount("big", 200, {0: "alpha", 1: "beta", 2: "beta", 3: "beta", 4: "beta", 5: "beta", 6: "beta", 7: "beta", 8: "beta"})
        ms = [{"corpus": "big", "path": big, "priority": 0}, {"corpus": "small", "path": small, "priority": 1}]
        q = "alpha beta"
        info = {}
        for m in ms:
            ix = R.Index(m["path"])
            Rm = R.rank(ix, q, {})
            smax = sum(Rm["vps"][t].idf for t in Rm["terms"])
            d = Rm["order"][0]
            info[m["corpus"]] = (ix.ids[d], Rm["Sf"][d], smax, Rm["Sf"][d] * 10 ** 6 // smax)
        self.assertGreater(info["big"][1], info["small"][1])            # raw order favours the big mount
        self.assertGreater(info["small"][3], info["big"][3])            # normalised order favours the small one
        self.assertLess(info["big"][3], 600000)                          # about S_max/2
        a = R.route_multi(ms, q, {}, "0" * 16)
        self.assertEqual(a["r"][0][0], "small:SMALL-000")
        self.assertEqual(a["r"][1][0], "big:BIG-000")

    def test_shuffle_invariance(self):
        rnd = random.Random(5)
        for q in ["scheduler socket", "sprite tilemap collider", "cache eviction paging", "parser", "graph partitioning", "PM-010.3"]:
            base = R.dumps(R.route_multi(self.mounts, q, {}, "0" * 16))
            for _ in range(4):
                m = list(self.mounts)
                rnd.shuffle(m)
                self.assertEqual(R.dumps(R.route_multi(m, q, {}, "0" * 16)), base, q)
            self.assertEqual(JS.validate(json.loads(base), SHORT), [])

    def test_abstention_uses_d1_mount_stats(self):
        q = "scheduler socket handler"
        a = R.route_multi(self.mounts, q, {"m_min": 1, "tau_cov": 0, "tau_peak": 0}, "0" * 16)
        top = a["r"][0][0].split(":")[0]
        mp = {m["corpus"]: m for m in self.mounts}[top]
        ix = R.Index(mp["path"])
        Rm = R.rank(ix, q, {"m_min": 1, "tau_cov": 0, "tau_peak": 0})
        m, cov, peak, _ = R.features(ix, Rm)
        self.assertEqual((a["abstain"]["m"], a["abstain"]["cov"], a["abstain"]["peak"]), (m, cov, peak))
        other = [x for x in self.mounts if x["corpus"] != top][0]
        ix2 = R.Index(other["path"])
        f2 = R.features(ix2, R.rank(ix2, q, {}))[:3]
        self.assertNotEqual((m, cov, peak), tuple(f2))

    def test_asset_anchor_dispatched_to_asset_resolver(self):
        calls = []
        stubs = {k: (lambda anchor, k=k: calls.append((k, anchor)) or k) for k in ("asset", "scene", "symbol", "section")}
        q = "sprite loader"
        ix = R.Index(self.vg)
        # choose a query that returns VG-001 whose first anchor is the asset
        rows = R.route(ix, "collider physics module", {})["r"]
        hit = [r for r in R.route(ix, "sprite tilemap module", {})["r"] if r[0] == "VG-001"]
        self.assertTrue(hit)
        self.assertEqual(hit[0][1], "uid://example0001")
        d = ix.ids.index("VG-001")
        self.assertEqual(R.resolve_anchor(ix, d, "uid://example0001", stubs), "asset")
        self.assertEqual(R.resolve_anchor(ix, d, "scene/Main/Node1", stubs), "scene")
        self.assertEqual(R.resolve_anchor(ix, d, "sec-1", stubs), "section")
        self.assertEqual(calls[0], ("asset", "uid://example0001"))
        px = R.Index(self.math)
        self.assertEqual(R.resolve_anchor(px, 0, "stmt-main", stubs), "section")


if __name__ == "__main__":
    unittest.main()


class T10cli(unittest.TestCase):
    def test_cli_corpus_flag(self):
        d = tmp()
        sw = build_nodes(d, "sw")
        r = subprocess.run([sys.executable, os.path.join(W, "tools", "route_v2.py"), "--index", sw, "--corpus", "vg=" + build_nodes(d, "vg") + ":5",
                            "--q", "scheduler sprite"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        a = json.loads(r.stdout)
        self.assertEqual(JS.validate(a, SHORT), [])
        self.assertTrue(all(x[0].split(":")[0] in ("math", "vg") for x in a["r"]))
        bad = subprocess.run([sys.executable, os.path.join(W, "tools", "route_v2.py"), "--index", sw, "--corpus", "oops", "--q", "x"], capture_output=True)
        self.assertEqual(bad.returncode, 3)
