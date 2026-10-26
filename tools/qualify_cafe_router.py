"""Report structural router availability honestly; calibration is not implemented."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys

SCHEMA = "cafe.router-qualification.v1"
ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ("test_T2_build.py", "test_T3_route.py", "test_T9_gates.py",
            "test_T10_multimount.py", "test_T11_logging.py")
PENDING = (("extractor_rust", "R5"), ("extractor_js", "R6"),
           ("extractor_python", "R7"), ("resolver_index", "R8"))
LEG_NAMES = ("entry_suite", "portable_router", "ownership", "integration_suite") + tuple(name for name, _ in PENDING)


@dataclass(frozen=True)
class Check:
    selection: str
    passed: bool
    rc: int | None = None
    tests: int | None = None
    problems: tuple = ()

    def public(self):
        return {"selection": self.selection, "passed": self.passed, "rc": self.rc,
                "tests": self.tests, "problems": list(self.problems)}


@dataclass(frozen=True)
class Leg:
    name: str
    implemented: bool
    checks: tuple = ()
    reason: str = ""

    def __post_init__(self):
        if not self.implemented and self.checks:
            raise ValueError("unimplemented leg cannot carry passing checks")

    @property
    def status(self):
        if not self.implemented:
            return "unavailable"
        return "pass" if self.checks and all(check.passed for check in self.checks) else "fail"


def safe_relative(path):
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    return (posixpath.normpath(path) == path and
            all(part not in ("", ".", "..") for part in path.split("/")))


def _contained(root, path):
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except ValueError:
        return False


def git_inventory(root):
    """Include prepared public files before the operator's first commit."""
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=30)
    if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
        raise ValueError("repository Git inventory unavailable")
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                             "--exclude-standard", "-z"], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("repository Git inventory failed")
    return sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})


def read_inline_inventory(root):
    """Explicit clean-export metadata; never discover or trust a fallback tree."""
    value = os.environ.get('CAFE_PREPARED_INVENTORY_JSON')
    if value is None:
        return None
    try:
        names = json.loads(value)
        if not isinstance(names, list) or not names or any(not isinstance(p, str) for p in names) or names != sorted(set(names)):
            raise ValueError('invalid_prepared_inventory')
        root = Path(root).resolve()
        for name in names:
            parts = name.split('/')
            if not name or name.startswith('/') or '\\' in name or ':' in name or '\0' in name or any(p in ['', '.', '..'] for p in parts) or parts[0] in ['.git', 'local', 'node_modules', 'target', '_scratch', '_target']:
                raise ValueError('invalid_prepared_inventory')
            path = root / name
            if path.resolve() != path or not path.is_file():
                raise ValueError('invalid_prepared_inventory')
        return names
    except (ValueError, TypeError, OSError):
        raise ValueError('invalid_prepared_inventory') from None


def load_inventory(root, snapshot=None):
    root = Path(root)
    if snapshot is None:
        if not (root / '.git').exists():
            inline = read_inline_inventory(root)
            if inline is not None:
                return inline, 'explicit prepared inventory; Git freshness not asserted'
        return git_inventory(root), "git cached and nonignored untracked"
    with Path(snapshot).open(encoding="utf-8") as stream:
        inventory = json.load(stream)
    # A caller-supplied snapshot is explicit evidence, never a silent fallback.
    # When Git exists it remains authoritative and detects stale/partial snapshots.
    if (root / ".git").exists():
        actual = git_inventory(root)
        if not isinstance(inventory, list) or set(inventory) != set(actual):
            raise ValueError("explicit inventory differs from current Git inventory")
    return inventory, "explicit inventory snapshot; Git freshness not asserted"


def check_ownership(root, inventory):
    problems = []
    root = root.resolve()
    try:
        data = json.loads((root / "catalogue/ownership.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check("ownership", False, problems=("ownership policy unreadable",))
    if (not isinstance(data, dict) or set(data) != {"schema", "roots"} or
            data.get("schema") != "cafe.ownership.v1" or
            not isinstance(data.get("roots"), list) or not data["roots"]):
        return Check("ownership", False, problems=("invalid ownership schema",))
    roots = []
    for item in data["roots"]:
        if (not isinstance(item, dict) or set(item) != {"path", "owner", "language", "role", "extractor"} or
                any(not isinstance(value, str) or not value.strip() for value in item.values())):
            problems.append("invalid owner row")
            continue
        raw = item["path"]
        directory = raw.endswith("/")
        rel = raw[:-1] if directory else raw
        if not safe_relative(rel) or item["extractor"] not in {"R5", "R6", "R7", "not indexed"}:
            problems.append("unsafe root or invalid extractor declaration")
            continue
        lexical = root / rel
        canonical = lexical.resolve()
        if not _contained(root, canonical) or canonical != lexical:
            problems.append("root escape or symlink alias")
            continue
        if lexical.exists() and ((directory and not lexical.is_dir()) or
                                 (not directory and not lexical.is_file())):
            problems.append("root kind differs from declaration")
            continue
        for previous, previous_dir in roots:
            if (canonical == previous or (directory and _contained(canonical, previous)) or
                    (previous_dir and _contained(previous, canonical))):
                problems.append("overlapping owner roots")
        roots.append((canonical, directory))
    if (not isinstance(inventory, list) or not inventory or
            any(not safe_relative(rel) for rel in inventory)):
        return Check("ownership", False, problems=tuple(problems + ["empty or unsafe file inventory"]))
    if len(set(inventory)) != len(inventory):
        problems.append("duplicate inventory paths")
    for rel in inventory:
        lexical = root / rel
        canonical = lexical.resolve()
        if not _contained(root, canonical) or canonical != lexical or not lexical.is_file():
            problems.append("file missing, escaping or aliased")
            continue
        owners = [owner for owner, directory in roots
                  if canonical == owner or (directory and _contained(owner, canonical))]
        if len(owners) != 1:
            problems.append("file does not have exactly one owner")
    return Check("ownership", not problems, problems=tuple(sorted(set(problems))))


def run_selection(root, directory, pattern):
    """Zero exit alone is insufficient: a selection must run real passing tests."""
    selection = directory + "/" + pattern
    index_check = directory == "catalogue/router" and pattern == "--check"
    if index_check:
        command = [sys.executable, "-B", "router/tools/build_index_v2.py", "--nodes",
                   "catalogue/nodes.jsonl", "--out", "catalogue/router", "--corpus", "cafe", "--check"]
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=900)
            passed = result.returncode == 0 and result.stdout.strip() == "byte-identical"
            return Check(selection, passed, result.returncode, problems=() if passed else ("index not byte-identical",))
        except (OSError, subprocess.TimeoutExpired):
            return Check(selection, False, problems=("index check unavailable",))
    integration = directory == "tests/integration" and pattern == "test_authority_channel.py"
    command = ([sys.executable, "-B", "tools/run_integration_gate.py"] if integration else
               [sys.executable, "-B", "-m", "unittest", "discover", "-s", directory, "-p", pattern])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name in ("ROUTER_PILOT_REPO", "ROUTER_PILOT_V1"):
        env.pop(name, None)
    try:
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return Check(selection, False, problems=("test process unavailable or timed out",))
    text = result.stdout + result.stderr
    counts = re.findall(r"(?m)^Ran (\d+) tests? in [^\n]+$", text)
    count = int(counts[0]) if len(counts) == 1 else None
    plain_ok = bool(re.search(r"(?m)^OK\s*$", text))
    rejected = bool(re.search(r"(?m)^(?:FAILED|ERROR:|FAIL:|OK \()", text))
    passed = result.returncode == 0 and count is not None and count > 0 and plain_ok and not rejected
    if integration:
        try:
            gate = json.loads(result.stdout)
            passed = (passed and gate.get("schema") == "cafe.integration-gate.v1" and
                      gate.get("status") == "pass" and gate.get("cleanup") is True and
                      gate.get("build_rc") == 0 and gate.get("test_rc") == 0 and
                      gate.get("tests") == count)
        except (ValueError, AttributeError):
            passed = False
    problems = () if passed else ("nonpassing, empty or unverifiable test selection",)
    # Never expose raw tracebacks, execution paths, durations or process identifiers.
    return Check(selection, passed, result.returncode, count, problems)


def collect(root, snapshot=None):
    entry = Leg("entry_suite", True, (run_selection(root, "tests/bootstrap", "test_entry_contract.py"),))
    portable = Leg("portable_router", True,
                   tuple(run_selection(root / "router", "tests", name) for name in PORTABLE))
    try:
        inventory, inventory_source = load_inventory(root, snapshot)
        ownership = check_ownership(root, inventory)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        inventory_source = "unavailable"
        ownership = Check("ownership", False, problems=("file inventory unavailable or invalid",))
    integration = Leg("integration_suite", True,
                      (run_selection(root, "tests/integration", "test_authority_channel.py"),))
    legs = (entry, portable, Leg("ownership", True, (ownership,)), integration) + tuple(
        Leg(name, True, (run_selection(root, "tests/bootstrap",
             {"extractor_rust": "test_extract_rust.py", "extractor_js": "test_extract_js.py",
              "extractor_python": "test_extract_py.py"}[name]),))
        if name in {"extractor_rust", "extractor_js", "extractor_python"} else
        Leg(name, True, (run_selection(root, "tests/bootstrap", "test_router_index.py"),
                         run_selection(root, "catalogue/router", "--check")))
        for name, unit in PENDING)
    return legs, inventory_source


def summarize(legs, inventory_source):
    rows = {}
    for leg in legs:
        status = leg.status
        rows[leg.name] = {"status": status, "implemented": leg.implemented,
                          "checks": [check.public() for check in leg.checks], "reason": leg.reason}
    available = all(row["status"] == "pass" for row in rows.values()) and bool(rows)
    if "resolver_index" in rows:
        rows["resolver_index"]["availability"] = "available_uncalibrated" if rows["resolver_index"]["status"] == "pass" else "unavailable"
    return {"schema": SCHEMA, "verdict": "router available (uncalibrated)" if available else "router unavailable",
            "state": "available_uncalibrated" if available else "unavailable", "calibrated": False,
            "inventory_source": inventory_source, "legs": rows}


def validate_report(report, legs):
    """Independently reject statuses promoted without implementation evidence."""
    if set(report.get("legs", {})) != set(LEG_NAMES) or tuple(leg.name for leg in legs) != LEG_NAMES:
        raise ValueError("incomplete or duplicate qualification legs")
    statuses = []
    for leg in legs:
        row = report["legs"][leg.name]
        expected = ("unavailable" if not leg.implemented else
                    "pass" if leg.checks and all(check.passed for check in leg.checks) else "fail")
        if (row.get("status") != expected or row.get("implemented") is not leg.implemented or
                row.get("checks") != [check.public() for check in leg.checks] or
                row.get("reason") != leg.reason):
            raise ValueError("qualification summary contradicts executed evidence")
        statuses.append(expected)
    available = bool(statuses) and all(status == "pass" for status in statuses)
    verdict = "router available (uncalibrated)" if available else "router unavailable"
    expected_state = "available_uncalibrated" if available else "unavailable"
    resolver = report["legs"]["resolver_index"]
    expected_resolver = "available_uncalibrated" if resolver["status"] == "pass" else "unavailable"
    if (report.get("schema") != SCHEMA or report.get("verdict") != verdict or
            report.get("state") != expected_state or report.get("calibrated") is not False or
            resolver.get("availability") != expected_resolver):
        raise ValueError("incorrect global availability or calibration verdict")


def exit_code(legs, require_qualified=False):
    if tuple(leg.name for leg in legs) != LEG_NAMES:
        return 1
    if any(leg.implemented and leg.status != "pass" for leg in legs):
        return 1
    # Structural success is not calibration evidence. No calibration gate exists yet.
    if require_qualified:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-qualified", action="store_true")
    parser.add_argument("--inventory", help="explicit JSON inventory for no-Git verification copies")
    args = parser.parse_args(argv)
    try:
        legs, source = collect(ROOT, args.inventory)
        report = summarize(legs, source)
        validate_report(report, legs)
        rc = exit_code(legs, args.require_qualified)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        report = {"schema": SCHEMA, "verdict": "router unavailable",
                  "error": "qualification evidence or summary rejected"}
        rc = 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
