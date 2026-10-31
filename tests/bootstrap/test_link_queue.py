"""Independent queue safety checks; no operator repository or process is changed."""
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('cafe_link_queue', Path(__file__).resolve().parents[2] / 'ctl' / 'link_queue.py')
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class LinkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-queue-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(strict=True)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.camp = self.root / 'campaign'; self.camp.mkdir()
        (self.camp / 'PARADA.txt').write_text('LIBRE\n')
        self.init_fixture_repo(self.repo)
        self.file = self.repo / 'sample.txt'; self.file.write_bytes(b'before\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'sample.txt'], check=True)
        self.unrelated = self.repo / 'operator.txt'; self.unrelated.write_bytes(b'operator work\n')
        self.patch = self.root / 'unit.patch'
        self.patch.write_bytes(b'diff --git a/sample.txt b/sample.txt\n--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-before\n+after\n')
        self.proof = self.root / 'proof.json'
        self.proof.write_text(json.dumps({'verdict': 'PASS', 'patch_sha256': hashlib.sha256(self.patch.read_bytes()).hexdigest(), 'preimages': {'sample.txt': {'sha256': hashlib.sha256(b'before\n').hexdigest(), 'mode': stat.S_IMODE(self.file.stat().st_mode)}}}))

    @staticmethod
    def init_fixture_repo(repo):
        # Byte oracles are LF; pin end-of-line handling in the fixture repo's
        # own config so a runner-wide core.autocrlf=true (Git for Windows
        # default) cannot rewrite `git apply` output as CRLF.
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', '--local', 'core.autocrlf', 'false'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', '--local', 'core.eol', 'lf'], check=True)

    def enqueue(self, unit='alpha'):
        return queue.append(self.repo, self.camp, unit, 'recorder', self.patch, self.proof, 'Implement bounded recorder')

    def test_green_keeps_patch_and_preserves_index(self):
        self.enqueue()
        index = queue.git(self.repo, 'diff', '--cached')
        row = queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.assertEqual(row['status'], 'linked')
        self.assertEqual(self.file.read_bytes(), b'after\n')
        self.assertEqual(queue.git(self.repo, 'diff', '--cached'), index)
        self.assertEqual(self.unrelated.read_bytes(), b'operator work\n')

    def test_crlf_true_runner_config_still_yields_lf_bytes(self):
        # Reproduce a runner whose global config sets core.autocrlf=true; the
        # fixture repo must still produce the exact LF bytes the oracles expect.
        from unittest import mock
        home = self.root / 'hostile-home'; home.mkdir()
        config = home / '.gitconfig'; config.write_bytes(b'[core]\n\tautocrlf = true\n')
        with mock.patch.dict(os.environ, {'HOME': str(home), 'GIT_CONFIG_GLOBAL': str(config)}):
            # Non-vacuity: the hostile setting is visible to git in this process tree.
            visible = subprocess.run(['git', 'config', '--global', '--get', 'core.autocrlf'], check=True, capture_output=True).stdout
            self.assertEqual(visible.strip(), b'true')
            repo = self.root / 'hostile-repo'; repo.mkdir()
            self.init_fixture_repo(repo)
            self.assertEqual(queue.git(repo, 'config', '--get', 'core.autocrlf').strip(), b'false')
            target = repo / 'sample.txt'; target.write_bytes(b'before\n')
            queue.git(repo, 'add', 'sample.txt')
            camp = self.root / 'hostile-campaign'; camp.mkdir(); (camp / 'PARADA.txt').write_text('LIBRE\n')
            proof = self.root / 'hostile-proof.json'
            proof.write_text(json.dumps({'verdict': 'PASS', 'patch_sha256': hashlib.sha256(self.patch.read_bytes()).hexdigest(), 'preimages': {'sample.txt': queue.snapshot(target)}}))
            queue.append(repo, camp, 'alpha', 'recorder', self.patch, proof, 'Implement bounded recorder')
            row = queue.apply_next(repo, camp, lambda *_: 0)
        self.assertEqual(row['status'], 'linked')
        self.assertEqual(target.read_bytes(), b'after\n')

    def test_patch_preimage_rejects_crlf_drift(self):
        # The reviewed LF preimage digest must not accept CRLF working-tree bytes.
        self.file.write_bytes(b'before\r\n')
        with self.assertRaisesRegex(ValueError, 'preimages'): self.enqueue()
        self.assertFalse((self.camp / 'LINK_QUEUE.json').exists())
        self.assertEqual(self.file.read_bytes(), b'before\r\n')

    def test_red_restores_only_owned_preimages(self):
        self.enqueue()
        row = queue.apply_next(self.repo, self.camp, lambda *_: 1)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(self.file.read_bytes(), b'before\n')
        self.assertEqual(self.unrelated.read_bytes(), b'operator work\n')

    def test_gate_exception_restores_patch_and_records_red(self):
        self.enqueue()
        def unavailable(*_):
            raise OSError('simulated gate unavailable')
        with self.assertRaises(OSError): queue.apply_next(self.repo, self.camp, unavailable)
        self.assertEqual(self.file.read_bytes(), b'before\n')
        with queue.locked(self.camp) as state:
            self.assertEqual(state['reds']['recorder'], 1)
            self.assertEqual(state['rollbacks_since_green'], 1)
            self.assertEqual(state['units'][0]['gate'], 'unavailable')

    def test_two_reds_park_surface(self):
        self.enqueue('alpha'); queue.apply_next(self.repo, self.camp, lambda *_: 1)
        self.enqueue('beta'); queue.apply_next(self.repo, self.camp, lambda *_: 1)
        with self.assertRaisesRegex(ValueError, 'parked'): self.enqueue('gamma')

    def test_local_green_never_resets_sprint_rollback_count(self):
        self.enqueue('first'); queue.apply_next(self.repo, self.camp, lambda *_: 1)
        self.enqueue('second'); queue.apply_next(self.repo, self.camp, lambda *_: 0)
        with queue.locked(self.camp) as state:
            self.assertEqual(state['rollbacks_since_green'], 1)

    def test_rollback_count_is_sprint_local_not_inherited_ci_failure(self):
        with queue.locked(self.camp) as state:
            self.assertEqual(state['rollbacks_since_green'], 0)
        self.enqueue('first'); queue.apply_next(self.repo, self.camp, lambda *_: 1)
        self.enqueue('second'); queue.apply_next(self.repo, self.camp, lambda *_: 1)
        with queue.locked(self.camp) as state:
            self.assertEqual(state['rollbacks_since_green'], 2)

    def test_corrupt_rollback_count_fails_closed(self):
        for count in (-1, True, 'zero'):
            (self.camp / 'LINK_QUEUE.json').write_text(json.dumps({'schema': 'cafe.link-queue.v1', 'units': [], 'reds': {}, 'rollbacks_since_green': count}))
            with self.subTest(count=count), self.assertRaises(ValueError), queue.locked(self.camp): pass

    def test_stop_file_prevents_any_patch_effect(self):
        self.enqueue()
        (self.camp / 'PARADA.txt').write_text('STOP\n')
        with self.assertRaisesRegex(ValueError, 'stop'): queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.assertEqual(self.file.read_bytes(), b'before\n')

    def test_empty_gate_stdout_cannot_be_success(self):
        from unittest import mock
        python = self.camp / 'out' / 'P15' / 'deps' / 'venv' / 'bin' / 'python'
        python.parent.mkdir(parents=True); python.write_text('fixture')
        node = self.root / 'node-bin'; node.mkdir(); (node / 'node').write_text('fixture')
        with mock.patch.dict(os.environ, {'CAFE_NODE_BIN': str(node)}), mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='', stderr='')):
            self.assertEqual(queue.full_gate(self.repo, self.camp, 'sample'), 1)

    def test_window_due_by_four_units_or_ninety_minutes(self):
        row = {'status': 'linked', 'linked_at': 100, 'unit': 'alpha'}
        self.assertFalse(queue.window_due({'units': [row]}, now=5499))
        self.assertTrue(queue.window_due({'units': [row]}, now=5500))
        self.assertTrue(queue.window_due({'units': [dict(row) for _ in range(4)]}, now=100))
        self.assertFalse(queue.window_due({'units': [dict(row, window=1)]}, now=9999))

    def test_explicit_node_directory_precedes_system_path(self):
        from unittest import mock
        node = self.root / 'node-bin'; node.mkdir(); (node / 'node').write_text('fixture')
        python = self.camp / 'out' / 'P15' / 'deps' / 'venv' / 'bin' / 'python'
        python.parent.mkdir(parents=True); python.write_text('fixture')
        report = {'status': 'pass', 'stages': [{'stage': name, 'status': 'pass'} for name in ('rust','python','node','checkjs','integration','router','publication','claims')]}
        with mock.patch.dict(os.environ, {'CAFE_NODE_BIN': str(node)}), mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(report), stderr='')) as run:
            self.assertEqual(queue.full_gate(self.repo, self.camp, 'sample'), 0)
            self.assertEqual(run.call_args.kwargs['env']['PATH'].split(os.pathsep)[0], str(node))

    def test_implicit_system_node_is_never_used(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'explicit_node'): queue.full_gate(self.repo, self.camp, 'sample')

    def test_nonoverlapping_missing_review_preimages_rejected(self):
        proof = json.loads(self.proof.read_text()); proof.pop('preimages')
        self.proof.write_text(json.dumps(proof))
        with self.assertRaisesRegex(ValueError, 'preimages'): self.enqueue()
        self.assertFalse((self.camp / 'LINK_QUEUE.json').exists())

    def test_nonoverlapping_mismatched_review_preimages_rejected(self):
        proof = json.loads(self.proof.read_text()); proof['preimages']['sample.txt']['sha256'] = '0' * 64
        self.proof.write_text(json.dumps(proof))
        with self.assertRaisesRegex(ValueError, 'preimages'): self.enqueue()
        self.assertFalse((self.camp / 'LINK_QUEUE.json').exists())

    def test_unproven_patch_cannot_enter_queue(self):
        self.proof.write_text(json.dumps({'verdict': 'PASS', 'patch_sha256': '0' * 64}))
        with self.assertRaisesRegex(ValueError, 'evidence'): self.enqueue()

    def test_stale_preimage_refuses_without_changing_operator_work(self):
        self.enqueue(); self.file.write_bytes(b'new operator work\n')
        with self.assertRaisesRegex(ValueError, 'preimage'): queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.assertEqual(self.file.read_bytes(), b'new operator work\n')

    def test_changed_queued_patch_refused(self):
        row = self.enqueue(); Path(row['patch']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'changed'): queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.assertEqual(self.file.read_bytes(), b'before\n')

    def test_overlapping_units_refused_before_commit_window(self):
        self.enqueue()
        with self.assertRaisesRegex(ValueError, 'overlapping'): self.enqueue('beta')

    def shared_patch(self, unit, code_name, shared_before, shared_after, bind=True):
        code = self.repo / code_name
        if not code.exists():
            code.write_bytes(b'before\n')
            subprocess.run(['git', '-C', str(self.repo), 'add', code_name], check=True)
        shared = self.repo / 'catalogue' / 'router_sources.json'
        if not shared.exists():
            shared.parent.mkdir(); shared.write_bytes(shared_before + b'\n')
            subprocess.run(['git', '-C', str(self.repo), 'add', 'catalogue/router_sources.json'], check=True)
        patch = self.root / (unit + '.patch')
        patch.write_bytes(('diff --git a/'+code_name+' b/'+code_name+'\n--- a/'+code_name+'\n+++ b/'+code_name+'\n@@ -1 +1 @@\n-before\n+after\n').encode() + b'diff --git a/catalogue/router_sources.json b/catalogue/router_sources.json\n--- a/catalogue/router_sources.json\n+++ b/catalogue/router_sources.json\n@@ -1 +1 @@\n-' + shared_before + b'\n+' + shared_after + b'\n')
        proof = self.root / (unit + '.json')
        evidence = {'verdict': 'PASS', 'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest()}
        if bind:
            evidence['preimages'] = {name: queue.snapshot(queue.safe_path(self.repo, name)) for name in [code_name, 'catalogue/router_sources.json']}
        proof.write_text(json.dumps(evidence))
        return queue.append(self.repo, self.camp, unit, unit, patch, proof, 'Implement '+unit)

    def test_serial_units_share_only_final_generated_metadata_commit(self):
        self.shared_patch('alpha', 'first.txt', b'zero', b'one')
        queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.shared_patch('beta', 'second.txt', b'one', b'two')
        queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.assertEqual((self.repo/'catalogue/router_sources.json').read_bytes(), b'two\n')
        self.assertEqual((self.repo/'first.txt').read_bytes(), b'after\n')
        self.assertEqual((self.repo/'second.txt').read_bytes(), b'after\n')
        record = queue.window(self.camp, self.repo)
        text = Path(record['path']).read_text()
        commits = [line for line in text.splitlines() if line.startswith('git commit')]
        self.assertEqual(len(commits), 3)
        self.assertIn(' -- first.txt', commits[0]); self.assertNotIn('catalogue/', commits[0])
        self.assertIn(' -- second.txt', commits[1]); self.assertNotIn('catalogue/', commits[1])
        self.assertIn(' -- catalogue/router_sources.json', commits[2])
        self.assertIn('only after all commits', text)
        self.assertEqual(self.unrelated.read_bytes(), b'operator work\n')

    def test_queued_metadata_overlap_is_still_forbidden(self):
        self.shared_patch('alpha', 'first.txt', b'zero', b'one')
        with self.assertRaisesRegex(ValueError, 'overlapping_queued'):
            self.shared_patch('beta', 'second.txt', b'zero', b'two')
        self.assertEqual((self.repo/'catalogue/router_sources.json').read_bytes(), b'zero\n')

    def test_linked_metadata_overlap_requires_exact_reviewed_preimages(self):
        self.shared_patch('alpha', 'first.txt', b'zero', b'one')
        queue.apply_next(self.repo, self.camp, lambda *_: 0)
        with self.assertRaisesRegex(ValueError, 'current_preimages'):
            self.shared_patch('beta', 'second.txt', b'one', b'two', bind=False)
        self.assertEqual((self.repo/'catalogue/router_sources.json').read_bytes(), b'one\n')

    def test_shared_metadata_red_restores_prior_successful_unit(self):
        self.shared_patch('alpha', 'first.txt', b'zero', b'one')
        queue.apply_next(self.repo, self.camp, lambda *_: 0)
        self.shared_patch('beta', 'second.txt', b'one', b'two')
        row = queue.apply_next(self.repo, self.camp, lambda *_: 1)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual((self.repo/'first.txt').read_bytes(), b'after\n')
        self.assertEqual((self.repo/'second.txt').read_bytes(), b'before\n')
        self.assertEqual((self.repo/'catalogue/router_sources.json').read_bytes(), b'one\n')
        self.assertEqual(self.unrelated.read_bytes(), b'operator work\n')

    def test_parent_paths_and_internal_state_refused(self):
        for name in ('../escape', '/escape', '.git/config', 'local/config', 'a/../b', 'a\\b', '.env', 'nested/operator.token', 'nested/private.key', 'C:escape'):
            with self.subTest(name=name), self.assertRaises(ValueError): queue.safe_path(self.repo, name)

    def test_symlink_refused(self):
        if os.name == 'nt':
            # Simulate the filesystem property without requiring symlink privileges.
            from unittest import mock
            with mock.patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError): queue.safe_path(self.repo, 'sample.txt')
        else:
            (self.repo / 'link.txt').symlink_to(self.file)
            with self.assertRaises(ValueError): queue.safe_path(self.repo, 'link.txt')

    def test_symlink_creation_patch_refused_before_apply(self):
        self.patch.write_bytes(b'diff --git a/link.txt b/link.txt\nnew file mode 120000\n--- /dev/null\n+++ b/link.txt\n@@ -0,0 +1 @@\n+sample.txt\n')
        self.proof.write_text(json.dumps({'verdict': 'PASS', 'patch_sha256': hashlib.sha256(self.patch.read_bytes()).hexdigest()}))
        with self.assertRaisesRegex(ValueError, 'symlink'): self.enqueue()
        self.assertFalse((self.repo / 'link.txt').exists())

    def test_corrupt_ledger_never_becomes_empty_queue(self):
        (self.camp / 'LINK_QUEUE.json').write_text('{')
        with self.assertRaises(ValueError), queue.locked(self.camp): pass

    def test_operator_window_uses_exact_paths_and_no_agent_commit(self):
        self.enqueue(); queue.apply_next(self.repo, self.camp, lambda *_: 0)
        record = queue.window(self.camp, self.repo)
        text = Path(record['path']).read_text()
        self.assertIn('git add -- sample.txt', text)
        self.assertIn(' -- sample.txt', text)
        self.assertIn('CARGO_HOME="$CAMP/out/P09/cargo-home" python3 -B tools/verify_clean_clone.py', text)
        self.assertIn('git push origin main', text)
        self.assertEqual(queue.git(self.repo, 'rev-list', '--all'), b'')


if __name__ == '__main__': unittest.main()
