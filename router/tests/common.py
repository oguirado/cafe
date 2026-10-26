import os, sys, tempfile, shutil, json, warnings
warnings.simplefilter("ignore", ResourceWarning)
W = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(W, "tools"))
sys.path.insert(0, os.path.join(W, "tests", "fixtures"))
sys.path.insert(0, os.path.join(W, "v1_frozen"))
import repo_gen  # noqa: E402
import build_index_v2 as B  # noqa: E402
import route_v2 as R  # noqa: E402

REPO = os.environ.get("ROUTER_PILOT_REPO") or None


def tmp():
    d = tempfile.mkdtemp(prefix="r2t")
    return d


def build_fixture(thes=None, **kw):
    d = tmp()
    root = repo_gen.make_repo(os.path.join(d, "repo"), **kw)
    out = os.path.join(d, "ix")
    if isinstance(thes, list):
        thes_path = os.path.join(d, "thes.json")
        with open(thes_path, "w") as f:
            json.dump({"schema": "ctx.thesaurus.v1", "version": 1, "entries": thes}, f)
    else:
        thes_path = thes or os.path.join(W, "tests", "fixtures", "thesaurus_empty.json")
    B.build(root, out, thes_path)
    return d, root, out


def no_float(s):
    def bad(x):
        raise ValueError("float " + x)
    return json.loads(s, parse_float=bad, parse_constant=bad)
