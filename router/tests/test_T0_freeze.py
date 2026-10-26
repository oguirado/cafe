import hashlib, json, os, subprocess, unittest
from common import *

V1 = os.environ.get("ROUTER_PILOT_V1") or None

PILOT_ABSENT = "NOT RUN: pilot corpus absent"


class T0Portable(unittest.TestCase):
    def test_sha256sums(self):
        r = subprocess.run(["sha256sum", "-c", "v1_frozen/SHA256SUMS"], cwd=W, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(r.stdout.strip().split("\n")), 5)


class T0Pilot(unittest.TestCase):
    def test_copies_equal_originals(self):
        if V1 is None:
            self.skipTest(PILOT_ABSENT + " (ROUTER_PILOT_V1 unset)")
        if not os.path.isdir(V1):
            self.fail("pilot input configured but invalid: ROUTER_PILOT_V1=%r" % (V1,))
        for n in ("build_index.py", "route.py", "eval_route.py", "make_pool.py"):
            with open(os.path.join(W, "v1_frozen", n), "rb") as f:
                frozen = f.read()
            pilot = os.path.join(V1, "tools", n)
            if not os.path.isfile(pilot):
                self.fail("pilot input missing file: %r" % (pilot,))
            with open(pilot, "rb") as f:
                self.assertEqual(frozen, f.read(), n)
        with open(os.path.join(W, "v1_frozen", "vocabulary.json"), "rb") as f:
            frozen_vocab = f.read()
        pilot_vocab = os.path.join(V1, "standards", "vocabulary.json")
        if not os.path.isfile(pilot_vocab):
            self.fail("pilot input missing file: %r" % (pilot_vocab,))
        with open(pilot_vocab, "rb") as f:
            self.assertEqual(frozen_vocab, f.read(), "vocabulary.json")

    def test_v1_index_rebuild_identical(self):
        if REPO is None:
            self.skipTest(PILOT_ABSENT + " (ROUTER_PILOT_REPO unset)")
        if not os.path.isdir(REPO):
            self.fail("pilot input configured but invalid: ROUTER_PILOT_REPO=%r" % (REPO,))
        p = os.path.join(W, "out", "v1_index")
        if not os.path.isdir(p):
            self.skipTest("NOT RUN: v1 index absent (historical rebuild input missing)")
        r = subprocess.run(["python3", os.path.join(W, "v1_frozen", "build_index.py"), "--root", REPO, "--out", p, "--vocab",
                            os.path.join(W, "v1_frozen", "vocabulary.json"), "--check"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "byte-identical"))


if __name__ == "__main__":
    unittest.main()
