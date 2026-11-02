import json
import contextlib
import io
from pathlib import Path
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import route_cafe as tool

BLOCKED = re.compile('/' + 'home/|/' + 'Users/')


class RouteTest(unittest.TestCase):
    def tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'core').mkdir()
        (root / 'tests').mkdir()
        (root / 'route').mkdir()
        body = b'fn alpha() { fail("frame arrived too late"); }\nfn beta() {}\n'
        (root / 'core/pacer.rs').write_bytes(body)
        (root / 'tests/test_pacer.py').write_text('alpha = "frame arrived too late"\n', encoding='utf-8')
        span = body[:47]
        sha = hashlib.sha256(span).hexdigest()[:16]
        whole = hashlib.sha256(body).hexdigest()[:16]
        (root / 'route/manifest.json').write_text(json.dumps({'skip': '^$', 'corpus': 'x'}), encoding='utf-8')
        (root / 'route/symbols.jsonl').write_text(json.dumps(['core/pacer.rs', 'alpha', 'function', 0, 46]) + '\n', encoding='utf-8')
        (root / 'route/nodes.jsonl').write_text(json.dumps({'l': 'extract', 'f': 'core', 'k': 'symptom', 't': 'recording stops with a stall', 's': [['core/pacer.rs::alpha', sha, 0, 47]]}) + '\n' + json.dumps({'l': 'extract', 'f': 'core', 'k': 'purpose', 't': 'pacer file', 's': [['core/pacer.rs', whole, None, None]]}) + '\n', encoding='utf-8')
        return root, body

    def test_answers_and_refusals(self):
        root, _ = self.tree()
        route = tool.Map(root, root / 'route')
        self.assertEqual(route.error('frame arrived too late', 3)[0]['path'], 'core/pacer.rs')
        self.assertEqual(route.ask('recording stall', 1)[0]['kind'], 'symptom')
        self.assertEqual(route.find('alpha', 1)[0]['path'], 'core/pacer.rs')
        self.assertEqual(route.locate('recording stall', 3)[0]['path'], 'core/pacer.rs')
        self.assertEqual(route.ask('zqxjkvq', 3), [])
        self.assertEqual(route.error('the of and', 3), [])
        self.assertEqual(route.tests('alpha', 3), ['tests/test_pacer.py'])

    def test_freshness_must_fail_on_change(self):
        root, body = self.tree()
        state = lambda: tool.Map(root, root / 'route').ask('recording stall', 1)[0]['state']
        self.assertEqual(state(), 'FRESH')
        (root / 'core/pacer.rs').write_bytes(body.replace(b'\n', b'\r\n'))
        self.assertEqual(state(), 'FRESH')
        (root / 'core/pacer.rs').write_bytes(b'// moved\n' + body)
        self.assertEqual(state(), 'SHIFT')
        (root / 'core/pacer.rs').write_bytes(body.replace(b'too late', b'too soon'))
        self.assertEqual(state(), 'STALE')
        (root / 'core/pacer.rs').unlink()
        self.assertEqual(state(), 'STALE')

    def test_freshness_requires_all_citations_and_never_assumes_empty_is_fresh(self):
        root, _ = self.tree()
        route = tool.Map(root, root / 'route')
        node = route.nodes[0]
        fresh = node['s'][0]
        unknown = ['missing.rs::unknown', None, None, None]
        stale = [fresh[0], '0' * 16, fresh[2], fresh[3]]
        for citations, expected in [([], 'UNKNOWN'), ([fresh, unknown], 'UNKNOWN'),
                                    ([fresh] * 6 + [unknown], 'UNKNOWN'),
                                    ([fresh] * 6 + [stale], 'STALE'),
                                    ([unknown, stale], 'STALE')]:
            with self.subTest(expected=expected, citations=len(citations)):
                row = route.row({**node, 's': citations})
                self.assertEqual(row['state'], expected)
                self.assertLessEqual(len(row['sids']), 4)
                self.assertLessEqual(len(row['code']), 3)

    def test_show_rejects_escape_before_reading_and_keeps_current_source_access(self):
        root, body = self.tree()
        route = tool.Map(root, root / 'route')
        self.assertEqual(route.show('core/pacer.rs')['code'], body.decode())
        self.assertEqual(route.show('core/pacer.rs@0-2')['code'], 'fn')
        self.assertEqual(route.show('core/pacer.rs::alpha')['code'], body[:46].decode())
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'outside.rs'
            target.write_text('harmless external fixture', encoding='utf-8')
            (root / 'escape.rs').symlink_to(target)
            (root / 'escaped_dir').symlink_to(target.parent, target_is_directory=True)
            bad = [str(target), '../outside.rs', './core/pacer.rs', 'core/../core/pacer.rs',
                   'core//pacer.rs', 'core\\pacer.rs', 'C:/outside.rs',
                   'escape.rs', 'escaped_dir/outside.rs']
            with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('rejected path read bytes')):
                for query in bad:
                    with self.subTest(query=query), self.assertRaisesRegex(ValueError, '^source path rejected$'):
                        route.show(query)
        with mock.patch.object(route, 'tracked_all', return_value=[]):
            with self.assertRaisesRegex(ValueError, '^source path rejected$'):
                route.show('core/pacer.rs')
        refused = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/route_cafe.py'),
                                  'show', '../outside.rs'], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, '')
        self.assertIn('source path rejected', refused.stderr)
        self.assertNotIn('Traceback', refused.stderr)

    def test_show_requires_nonempty_in_bounds_byte_spans(self):
        root, body = self.tree()
        route = tool.Map(root, root / 'route')
        self.assertEqual(route.show('core/pacer.rs@0-' + str(len(body)))['code'], body.decode())
        for span in ['0-0', '5-2', '0-' + str(len(body) + 1), '-1-2', '0-one', '0-2trailing']:
            with self.subTest(span=span), self.assertRaisesRegex(ValueError, '^source byte range rejected$'):
                route.show('core/pacer.rs@' + span)
        refused = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/route_cafe.py'),
                                  'show', 'tools/route_cafe.py@0-0'], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, '')
        self.assertIn('source byte range rejected', refused.stderr)
        self.assertNotIn('Traceback', refused.stderr)

    def test_cli_rejects_nonpositive_k_and_preserves_positive_limits(self):
        for count in ['0', '-1']:
            with self.subTest(count=count):
                result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/route_cafe.py'),
                                         'find', 'alpha', '--k', count], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('--k must be positive', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        root, _ = self.tree()
        route = tool.Map(root, root / 'route')
        output = io.StringIO()
        with mock.patch.object(tool, 'Map', return_value=route), contextlib.redirect_stdout(output):
            self.assertEqual(tool.main(['find', 'alpha', '--k', '1']), 0)
        self.assertEqual(len(json.loads(output.getvalue())['rows']), 1)

    def test_all_source_queries_skip_external_symlinks_before_read(self):
        root, _ = self.tree()
        route = tool.Map(root, root / 'route')
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            target = outside / 'outside.py'
            target.write_text('marker = "externalcanary"\n', encoding='utf-8')
            (root / 'linked.py').symlink_to(target)
            (root / 'tests/test_linked.py').symlink_to(target)
            (root / 'core/linked_dir').symlink_to(outside, target_is_directory=True)
            (root / 'tests/linked_dir').symlink_to(outside, target_is_directory=True)
            original_text, original_bytes = Path.read_text, Path.read_bytes
            def checked_text(path, *args, **kwargs):
                self.assertFalse(path.resolve().is_relative_to(outside), 'external text read')
                return original_text(path, *args, **kwargs)
            def checked_bytes(path, *args, **kwargs):
                self.assertFalse(path.resolve().is_relative_to(outside), 'external byte read')
                return original_bytes(path, *args, **kwargs)
            with mock.patch.object(Path, 'read_text', checked_text), mock.patch.object(Path, 'read_bytes', checked_bytes):
                self.assertEqual(route.error('externalcanary', 8), [])
                self.assertEqual(route.tests('externalcanary', 8), [])
                self.assertEqual(route.locate('externalcanary', 8), [])
                self.assertEqual(route.literals(['core/linked_dir/outside.py']), [])
                with mock.patch.object(route, 'tracked', return_value=['tests/linked_dir/outside.py']):
                    self.assertEqual(route.tests('externalcanary', 8), [])
                self.assertEqual(route.error('frame arrived too late', 3)[0]['path'], 'core/pacer.rs')
                self.assertEqual(route.tests('alpha', 3), ['tests/test_pacer.py'])

    def test_invalid_citation_spans_are_unknown_before_hash_comparison(self):
        root, body = self.tree()
        route = tool.Map(root, root / 'route')
        node = route.nodes[0]
        sid = node['s'][0][0]
        cases = [(-2, 1, tool.digest(body[-2:-1])), (0, 0, tool.digest(b'')),
                 (0, len(body) + 9, tool.digest(body)), ('0', 2, tool.digest(body[:2])),
                 (True, 1, tool.digest(body[1:2])), (0, False, tool.digest(b'')),
                 (None, 2, tool.digest(body[:2])), (0, None, tool.digest(body))]
        for start, length, digest in cases:
            with self.subTest(start=start, length=length):
                citation = [sid, digest, start, length]
                self.assertEqual(route.cite(citation), ('UNKNOWN', 'core/pacer.rs', None, None))
                self.assertEqual(route.row({**node, 's': [citation]})['state'], 'UNKNOWN')
        self.assertEqual(route.cite([sid, tool.digest(body), None, None])[0], 'FRESH')
        self.assertEqual(route.cite([sid, tool.digest(body[:2]), 0, 2]),
                         ('FRESH', 'core/pacer.rs', 0, 2))

    def test_published_data_stays_filtered(self):
        manifest = ROOT / 'route/manifest.json'
        nodes = ROOT / 'route/nodes.jsonl'
        if not nodes.exists():
            self.skipTest('findings not installed')
        removed = set(json.loads(manifest.read_text(encoding='utf-8'))['removed_sha256'])
        for line in nodes.read_text(encoding='utf-8').splitlines():
            text = json.loads(line)['t']
            self.assertIsNone(BLOCKED.search(text))
            self.assertNotIn(hashlib.sha256(text.encode()).hexdigest(), removed)


if __name__ == '__main__':
    unittest.main()
