import json, os, unittest
from common import *
import blocks, census, repo_gen


class T1(unittest.TestCase):
    def test_two_blocks_and_fence(self):
        d, root, out = build_fixture()
        p = os.path.join(root, "papers", "EN-0000_PM-001.1.md")
        b = blocks.read_blocks(p)
        self.assertIn("pm.search.v1", b)
        self.assertIn("pm.edges.v2", b)
        self.assertEqual(b["pm.search.v1"]["note"], "a fence ``` inside a string")
        self.assertEqual(blocks.anchors(p) & {"pm-s0", "stmt-main"}, {"pm-s0", "stmt-main"})

    def test_malformed_json(self):
        root = repo_gen.make_repo(os.path.join(tmp(), "repo"), bad_json=True)
        p = os.path.join(root, "papers", "EN-0001_PM-001.2.md")
        with self.assertRaises(blocks.BlockError) as c:
            blocks.read_blocks(p)
        self.assertEqual(c.exception.code, "E_BLOCK_JSON")
        self.assertEqual(c.exception.path, p)
        with self.assertRaises(B.BuildError) as c2:  # builder reports the same code with the path
            B.build(root, os.path.join(tmp(), "ix"), None)
        self.assertEqual(c2.exception.code, "E_BLOCK_JSON")

    def test_census_fixture_exact(self):
        d, root, out = build_fixture()
        import glob
        c = census.census(sorted(glob.glob(os.path.join(root, "papers", "*.md"))))
        self.assertEqual(c["papers"], 4)
        self.assertEqual(c["triggers"], 3 + 2 + 2 + 1)
        self.assertEqual(c["keywords"], 2 + 2 + 2 + 1)
        self.assertEqual(c["questions"], 3)
        self.assertEqual(c["qa_anchor_resolved"], 3)
        self.assertEqual(c["tasks"], 3)
        self.assertEqual(c["edges"], 4)
        self.assertEqual(c["edge_relations"]["supersedes"], 1)
        self.assertEqual(c["software_ish_papers"], 2)  # PM-001.1 strong software, PM-001.2 moderate algorithms
        self.assertEqual(c["trigger_words"]["2"], 8)

    def test_real_census_receipt(self):
        p = os.path.join(W, "out", "census.json")
        if not os.path.isfile(p):
            self.skipTest("census receipt absent")
        c = json.load(open(p))
        for k, v in dict(papers=719, triggers=6043, keywords=9437, questions=3194, tasks=1276, edges=1308, software_ish_papers=344).items():
            self.assertEqual(c[k], v, k)


if __name__ == "__main__":
    unittest.main()
