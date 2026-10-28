"""Check the worktree launcher contract without a network fetch or history changes."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'ctl' / 'unit.sh'


class UnitWorktreeTests(unittest.TestCase):
    def test_bash_syntax_and_invalid_slugs(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)
        for slug in ('../escape', 'Uppercase', 'a/b', '-option', ''):
            with self.subTest(slug=slug):
                result = subprocess.run(['bash', str(SCRIPT), slug], capture_output=True)
                self.assertEqual(result.returncode, 2)

    def test_requires_external_campaign_before_git(self):
        env = dict(os.environ); env.pop('CAMP', None)
        result = subprocess.run(['bash', str(SCRIPT), 'safe-unit'], env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'CAMP', result.stderr)

    def test_exact_fetched_base_is_used_and_history_operations_absent(self):
        # The embedded program is checked structurally; all git calls are declared argv.
        import ast
        source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == 'git']
        constants = [[value.value for value in node.elts if isinstance(value, ast.Constant)] for node in calls]
        self.assertEqual(len(calls), 3)
        self.assertIn(['git', '-C', 'fetch', 'origin', 'main'], constants)
        self.assertIn(['git', '-C', 'rev-parse', '--verify', 'origin/main^{commit}'], constants)
        self.assertIn(['git', '-C', 'worktree', 'add', '--detach'], constants)
        self.assertNotIn('stash', source); self.assertNotIn("'push'", source)
        self.assertIn("state/'target-writer'", source)

    def test_mocked_creation_uses_fetched_revision_and_own_target(self):
        import contextlib
        import io
        import json
        from unittest import mock
        source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        with tempfile.TemporaryDirectory(prefix='cafe-unit-test-') as temp:
            root = Path(temp); repo = root / 'repo'; repo.mkdir()
            camp = root / 'campaign'; camp.mkdir()
            parent = root / 'worktrees'
            revision = 'a' * 40
            calls = []
            def fake_run(argv, **kwargs):
                calls.append(argv)
                if 'worktree' in argv:
                    Path(argv[-2]).mkdir()
                return subprocess.CompletedProcess(argv, 0, stdout=revision+'\n')
            env = dict(os.environ, CAMP=str(camp), CAFE_UNIT_REPO=str(repo), CAFE_UNIT_SLUG='sample', CAFE_UNIT_WT=str(parent))
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(subprocess, 'run', side_effect=fake_run), contextlib.redirect_stdout(output):
                exec(compile(source, str(SCRIPT), 'exec'), {})
            record = json.loads(output.getvalue())
            self.assertEqual(record['base'], revision)
            self.assertEqual(calls[0][-3:], ['fetch', 'origin', 'main'])
            self.assertEqual(calls[2][-1], revision)
            self.assertEqual(Path(record['CARGO_TARGET_DIR']), camp / 'out' / 'worktrees' / 'sample' / 'target-writer')
            self.assertTrue((parent / 'wt-sample').is_dir())

    def test_existing_destination_is_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory(prefix='cafe-unit-test-') as temp:
            root = Path(temp); camp = root / 'campaign'; camp.mkdir()
            worktrees = root / 'worktrees'; worktrees.mkdir()
            (worktrees / 'wt-safe-unit').mkdir()
            env = dict(os.environ, CAMP=str(camp), CAFE_WORKTREE_ROOT=str(worktrees))
            result = subprocess.run(['bash', str(SCRIPT), 'safe-unit'], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'worktree_already_exists', result.stderr)


if __name__ == '__main__': unittest.main()
