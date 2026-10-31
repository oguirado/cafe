"""Independent portability regressions; run on Linux and native Windows."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'router' / 'tools'
sys.path.insert(0, str(TOOLS))
import route_v2 as R
import gates_v2 as G


class VendorWindows(unittest.TestCase):
    def child(self, code, *args):
        return subprocess.run([sys.executable, '-c',
            'import sys;sys.path.insert(0, ' + repr(str(TOOLS)) + ');' + code,
            *map(str, args)], capture_output=True, text=True, timeout=30)

    def test_path_forms(self):
        for path in ['/a/bench/dev.jsonl', r'C:\a\bench\dev.jsonl',
                     chr(92) * 2 + r'server\share\bench\dev.jsonl', r'a/bench\dev.jsonl',
                     r'C:\BENCH\dev.jsonl', 'bench/dev.jsonl']:
            with self.subTest(path=path):
                self.assertTrue(R.is_bench_path(path))
                p = subprocess.run([sys.executable, str(TOOLS / 'tune_v2.py'),
                    '--index', 'absent-index', '--bench', path], capture_output=True, timeout=30)
                self.assertEqual(p.returncode, 4, p.stderr)
                self.assertIn(b'E_BLIND', p.stderr)
        for path in ['/a/benchmark/dev.jsonl', r'C:\a\benchmarks\dev.jsonl',
                     r'C:\a\mybench\dev.jsonl', '/a/bench.jsonl']:
            self.assertFalse(R.is_bench_path(path), path)

    def test_blind_gate_and_near_match(self):
        with tempfile.TemporaryDirectory() as temp:
            # On POSIX this is a literal backslash name; on Windows real components.
            for rel, refused in [(r'a\bench\dev.jsonl', True),
                                 ('bench/dev.jsonl', True), ('benchmark/dev.jsonl', False)]:
                p = Path(temp) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('{"qid":"X","split":"DEV","answerable":true,"text":"x","gold":[]}\n', encoding='utf-8')
                gate = G.g_blind(str(p))
                self.assertEqual(gate['pass'], not refused, gate)
                run = subprocess.run([sys.executable, str(TOOLS / 'tune_v2.py'),
                    '--index', 'absent-index', '--bench', str(p)], capture_output=True, timeout=30)
                self.assertEqual(run.returncode, 4 if refused else 5, run.stderr)

    def test_mount_expected_values_in_child(self):
        cases = [
            ('sw=C:\\data\\ix', 'sw', 'C:\\data\\ix', 7),
            ('sw=C:\\data\\ix:2', 'sw', 'C:\\data\\ix', 2),
            ('sw=C:/data/ix:-3', 'sw', 'C:/data/ix', -3),
            ('sw=\\\\server\\share\\ix:0', 'sw', '\\\\server\\share\\ix', 0),
            ('sw=/tmp/ix', 'sw', '/tmp/ix', 7),
            ('sw=relative/ix:4', 'sw', 'relative/ix', 4),
            ('sw=C:relative', 'sw', 'C:relative', 7),
        ]
        for spec, name, path, priority in cases:
            p = self.child('import route_v2 as R,json;print(json.dumps(R.parse_mount(sys.argv[1],7)))', spec)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout), {'corpus': name, 'path': path, 'priority': priority})
        for spec in ['sw=C:\\ix:bad', 'sw=C:\\ix:', 'sw=/tmp/ix:no',
                     'sw=/tmp/ix:1:2', 'sw=', '=C:\\ix', 'missing']:
            p = self.child('import route_v2 as R;R.parse_mount(sys.argv[1],7)', spec)
            self.assertNotEqual(p.returncode, 0, spec)

    def test_actual_concurrent_records(self):
        code = '''
import route_v2 as R
for i in range(150):
    q = sys.argv[2] + ':' + str(i)
    R.write_log(sys.argv[1], 'same', q,
        {'intent':'none','ix':'a'*16,'pr':'b'*16,'r':[['DOC','anchor',7]],'abstain':{'flag':False}},
        {'terms':['café','cache']})
'''
        with tempfile.TemporaryDirectory() as temp:
            children = [subprocess.Popen([sys.executable, '-c',
                'import sys;sys.path.insert(0,' + repr(str(TOOLS)) + ');' + code,
                temp, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
            for child in children:
                out, err = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, err)
            raw = (Path(temp) / 'w_same.jsonl').read_bytes()
            self.assertTrue(raw.endswith(b'\n'))
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(records), 450)
            expected = {hashlib.sha256(f'{a}:{i}'.encode()).hexdigest()[:16]
                        for a in range(3) for i in range(150)}
            self.assertEqual({r['q_sha'] for r in records}, expected)
            for r in records:
                self.assertEqual(r, {'q_sha': r['q_sha'], 'terms':['café','cache'],
                    'intent':'none','ix':'a'*16,'pr':'b'*16,'r':[['DOC',7]],'abstain':False})
            if os.name == 'nt':
                self.assertTrue((Path(temp) / 'w_same.jsonl.lock').is_file())

    def test_cli_utf8_lf_bytes_ignore_host_text_wrapper(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('vendor_fixture_repo', ROOT / 'router/tests/fixtures/repo_gen.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as temp:
            source = fixture.make_repo(str(Path(temp) / 'repo'), n_synth=2)
            index = str(Path(temp) / 'ix')
            G.B.build(source, index, None)
            code = (
                "import io,sys,runpy;"
                "sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='ascii',newline='\\r\\n');"
                "sys.argv=[sys.argv[1],'--index',sys.argv[2],'--q','cache'];"
                "runpy.run_path(sys.argv[0],run_name='__main__')"
            )
            result = subprocess.run([sys.executable, '-I', '-c', code,
                str(TOOLS / 'route_v2.py'), index], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = (R.dumps(R.route(R.Index(index), 'cache', *R.load_params(None))) + '\n').encode('utf-8')
            self.assertEqual(result.stdout, expected)
            self.assertNotIn(b'\r', result.stdout)

    def test_partial_write_is_explicit_failure(self):
        code = '''
import route_v2 as R
from unittest.mock import patch
with patch.object(R.os, 'write', return_value=1):
    R.append_log_record(sys.argv[1], b'whole record\\n')
'''
        with tempfile.TemporaryDirectory() as temp:
            p = self.child(code, Path(temp) / 'partial.jsonl')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('incomplete router log record', p.stderr)

    def test_windows_lock_timeout_and_release(self):
        # The native branch runs on Windows; Linux checks explicit I/O failure.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'stable.lock'
            if os.name == 'nt':
                code = '''
import route_v2 as R
with R.windows_log_lock(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
'''
                child = subprocess.Popen([sys.executable, '-c',
                    'import sys;sys.path.insert(0,' + repr(str(TOOLS)) + ');' + code, str(path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'locked')
                    p = self.child('import route_v2 as R;\nwith R.windows_log_lock(sys.argv[1],timeout=0.1): pass', path)
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn('router log lock timeout', p.stderr)
                    self.assertTrue(path.exists())
                finally:
                    child.communicate('\n', timeout=30)
                self.assertEqual(child.returncode, 0)
                with R.windows_log_lock(str(path), timeout=0.1):
                    self.assertTrue(path.exists())
            else:
                p = self.child('import route_v2 as R;R.append_log_record(sys.argv[1],b"record\\n")', Path(temp) / 'absent' / 'record')
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('FileNotFoundError', p.stderr)


if __name__ == '__main__':
    unittest.main()
