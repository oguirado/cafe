"""Qualification honesty, ownership boundaries and non-vacuous selection checks."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cafe_qualification", ROOT / "tools/qualify_cafe_router.py")
q = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = q
SPEC.loader.exec_module(q)


def owner(path):
    return {"path": path, "owner": "fixture", "language": "Python",
            "role": "verification fixture", "extractor": "not indexed"}


def passing_legs():
    return tuple(q.Leg(name, True, (q.Check(name, True, 0, 1),)) for name in q.LEG_NAMES[:4]) + tuple(
        q.Leg(name, False, reason=unit + " pending") for name, unit in q.PENDING)


class TestRouterQualification(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "catalogue").mkdir()
        (self.root / "core").mkdir()
        (self.root / "core/item.rs").write_text("fixture\n")
        self.inventory = ["catalogue/ownership.json", "core/item.rs"]
        self.write_policy([owner("catalogue/"), owner("core/")])

    def write_policy(self, rows):
        (self.root / "catalogue/ownership.json").write_text(json.dumps(
            {"schema": "cafe.ownership.v1", "roots": rows}) + "\n")

    def test_complete_disjoint_ownership(self):
        self.assertTrue(q.check_ownership(self.root, self.inventory).passed)

    def test_duplicate_and_nested_roots_rejected(self):
        for extra in [owner("core/"), owner("core/item.rs")]:
            with self.subTest(root=extra["path"]):
                self.write_policy([owner("catalogue/"), owner("core/"), extra])
                check = q.check_ownership(self.root, self.inventory)
                self.assertFalse(check.passed)
                self.assertIn("overlapping owner roots", check.problems)

    def test_unsafe_roots_rejected(self):
        for unsafe in ["../escape/", "/absolute/", "core/../core/", "core//", r"C:\fixture", ".", ""]:
            with self.subTest(root=unsafe):
                self.write_policy([owner("catalogue/"), owner("core/"), owner(unsafe)])
                self.assertFalse(q.check_ownership(self.root, self.inventory).passed)

    def test_symlink_alias_and_escape_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / "alias").symlink_to(self.root / "core", target_is_directory=True)
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for alias in ["alias/", "escape/"]:
            with self.subTest(root=alias):
                self.write_policy([owner("catalogue/"), owner("core/"), owner(alias)])
                self.assertFalse(q.check_ownership(self.root, self.inventory).passed)
        self.write_policy([owner("catalogue/"), owner("core/")])
        (self.root / "core/link").symlink_to(outside, target_is_directory=True)
        (outside / "file").write_text("fixture\n")
        self.addCleanup((outside / "file").unlink)
        self.assertFalse(q.check_ownership(self.root, self.inventory + ["core/link/file"]).passed)

    def test_orphan_missing_duplicate_and_empty_inventory_rejected(self):
        (self.root / "orphan.txt").write_text("fixture\n")
        for inventory in [[], self.inventory + ["orphan.txt"], self.inventory + ["core/missing.rs"],
                          self.inventory + [self.inventory[0]], self.inventory + ["../escape"]]:
            with self.subTest(inventory=inventory):
                self.assertFalse(q.check_ownership(self.root, inventory).passed)

    def test_current_ownership_inventory_and_privacy(self):
        # Scratch judges provide the explicit snapshot; live runs use Git itself.
        snapshot = os_inventory_snapshot()
        inventory, source = q.load_inventory(ROOT, snapshot)
        self.assertTrue(inventory)
        self.assertTrue(source)
        check = q.check_ownership(ROOT, inventory)
        self.assertTrue(check.passed, check.problems)
        spec = importlib.util.spec_from_file_location("cafe_entry_volatility", ROOT / "tests/bootstrap/test_entry_contract.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        self.assertEqual(entry.scan_volatility((ROOT / "catalogue/ownership.json").read_text()), [])
        self.assertEqual(entry.scan_volatility(json.dumps(q.summarize(passing_legs(), "fixture"))), [])

    def test_unavailable_cannot_pass_or_qualify(self):
        legs = passing_legs()
        report = q.summarize(legs, "fixture")
        q.validate_report(report, legs)
        self.assertEqual(report["verdict"], "router unavailable")
        for name, _unit in q.PENDING:
            self.assertEqual(report["legs"][name]["status"], "unavailable")
            self.assertFalse(report["legs"][name]["implemented"])
        self.assertEqual(q.exit_code(legs), 0)
        self.assertNotEqual(q.exit_code(legs, True), 0)
        with self.assertRaises(ValueError):
            q.Leg("extractor_rust", False, (q.Check("fake", True),))

    def test_forced_unavailable_pass_summary_rejected(self):
        legs = passing_legs()
        for name, _unit in q.PENDING:
            report = q.summarize(legs, "fixture")
            report["legs"][name]["status"] = "pass"
            with self.subTest(leg=name), self.assertRaises(ValueError):
                q.validate_report(report, legs)
        report = q.summarize(legs, "fixture")
        report["verdict"] = "qualified"
        with self.assertRaises(ValueError):
            q.validate_report(report, legs)
        del report["legs"]["ownership"]
        with self.assertRaises(ValueError):
            q.validate_report(report, legs)

    def test_failed_implemented_leg_fails_default_rc(self):
        legs = list(passing_legs())
        legs[0] = q.Leg("entry_suite", True, (q.Check("fixture", False, 1, 1),))
        report = q.summarize(legs, "fixture")
        q.validate_report(report, tuple(legs))
        self.assertEqual(report["legs"]["entry_suite"]["status"], "fail")
        self.assertEqual(report["verdict"], "router unavailable")
        self.assertNotEqual(q.exit_code(legs), 0)
        empty = list(passing_legs())
        empty[0] = q.Leg("entry_suite", True)
        self.assertNotEqual(q.exit_code(empty), 0)

    def test_all_implemented_pass_is_only_uncalibrated_availability(self):
        legs = tuple(q.Leg(name, True, (q.Check(name, True, 0, 1),)) for name in q.LEG_NAMES)
        report = q.summarize(legs, "fixture")
        q.validate_report(report, legs)
        self.assertEqual(report["verdict"], "router available (uncalibrated)")
        self.assertEqual(report["state"], "available_uncalibrated")
        self.assertFalse(report["calibrated"])
        self.assertEqual(q.exit_code(legs, True), 1)
        with self.assertRaises(ValueError):
            q.validate_report(q.summarize((), "fixture"), ())

    def test_nonvacuous_selection_statuses(self):
        samples = [
            (0, "Ran 1 test in 0.001s\n\nOK\n", True, 1),
            (0, "Ran 0 tests in 0.001s\n\nOK\n", False, 0),
            (0, "Ran 1 test in 0.001s\n\nOK (skipped=1)\n", False, 1),
            (0, "OK\n", False, None),
            (1, "Ran 1 test in 0.001s\n\nFAILED (failures=1)\n", False, 1),
            (0, "Ran 1 test in 0.001s\nRan 1 test in 0.002s\nOK\n", False, None)]
        for rc, text, passed, count in samples:
            with self.subTest(rc=rc, text=text), mock.patch.object(q.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], rc, "", text)):
                result = q.run_selection(self.root, "tests", "fixture.py")
                self.assertIs(result.passed, passed)
                self.assertEqual(result.tests, count)
        with mock.patch.object(q.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)):
            self.assertFalse(q.run_selection(self.root, "tests", "fixture.py").passed)

    def test_actual_empty_selection_rejected(self):
        (self.root / "tests").mkdir()
        result = q.run_selection(self.root, "tests", "test_missing.py")
        self.assertIsInstance(result.rc, int)
        self.assertEqual(result.tests, 0)
        self.assertFalse(result.passed)

    def test_snapshot_never_silently_falls_back_or_ignores_git(self):
        with mock.patch.object(q, "git_inventory", side_effect=ValueError("absent")):
            with self.assertRaises(ValueError):
                q.load_inventory(self.root)
        snapshot = self.root / "inventory.json"
        snapshot.write_text(json.dumps(self.inventory))
        (self.root / ".git").mkdir()
        with mock.patch.object(q, "git_inventory", return_value=self.inventory + ["orphan.txt"]):
            with self.assertRaises(ValueError):
                q.load_inventory(self.root, snapshot)

    def test_cli_json_and_require_qualified(self):
        legs = passing_legs()
        for args, expected_rc in [([], 0), (["--require-qualified"], 1)]:
            stream = io.StringIO()
            with mock.patch.object(q, "collect", return_value=(legs, "fixture")), contextlib.redirect_stdout(stream):
                rc = q.main(args)
            self.assertEqual(rc, expected_rc)
            self.assertEqual(json.loads(stream.getvalue())["verdict"], "router unavailable")
        bad = q.summarize(legs, "fixture")
        bad["legs"]["extractor_rust"]["status"] = "pass"
        stream = io.StringIO()
        with mock.patch.object(q, "collect", return_value=(legs, "fixture")), \
                mock.patch.object(q, "summarize", return_value=bad), contextlib.redirect_stdout(stream):
            rc = q.main([])
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(stream.getvalue())["verdict"], "router unavailable")


def os_inventory_snapshot():
    import os
    return os.environ.get("CAFE_ROUTER_INVENTORY")


if __name__ == "__main__":
    unittest.main()
