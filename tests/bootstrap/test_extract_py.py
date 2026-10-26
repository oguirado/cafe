"""Independent handwritten Python inventory and AST refusal/read-boundary oracles."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'connectors/cafe/authority_channel.py'
FILES = [SOURCE]
# Hand-transcribed from the original P08 source BEFORE running extraction.
GOLD = [{'path': SOURCE, 'symbol': 'cafe.authority_channel.serve_connector',
         'name': 'serve_connector', 'line': 27, 'owner': 'connectors/', 'kind': 'async_function'}]
SPEC = importlib.util.spec_from_file_location('cafe_python_surface', ROOT / 'tools/extract_py.py')
x = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = x
SPEC.loader.exec_module(x)
q = x.q


def policy(root):
    (root / 'catalogue').mkdir(exist_ok=True)
    (root / 'catalogue/ownership.json').write_text(json.dumps({'schema': 'cafe.ownership.v1', 'roots': [
        {'path': 'connectors/', 'owner': 'Python connectors + MCP', 'language': 'Python',
         'role': 'fixture', 'extractor': 'R7'}]}))


class TestPythonExtraction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'connectors/cafe').mkdir(parents=True)
        policy(self.root)
        self.path = self.root / SOURCE

    def extract(self, text):
        self.path.write_text(text, encoding='utf-8')
        return x.extract(self.root, FILES, FILES)['files'][0]['symbols']

    def test_handwritten_current_public_inventory(self):
        inventory, _ = q.load_inventory(ROOT, os.environ.get('CAFE_ROUTER_INVENTORY'))
        report = x.extract(ROOT, FILES, inventory)
        self.assertEqual(report['schema'], 'cafe.python-surface.v1')
        self.assertEqual(report['parser'], 'stdlib.ast')
        self.assertEqual(report['files'][0]['symbols'], GOLD)
        self.assertEqual(report['files'][0]['source_sha256'], hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest())

    def test_deleted_public_function_breaks_manual_gold(self):
        source = (ROOT / SOURCE).read_text()
        self.assertEqual(source.count('async def serve_connector('), 1)
        rows = self.extract(source.replace('async def serve_connector(', 'async def _serve_connector(', 1))
        with self.assertRaises(AssertionError):
            self.assertEqual(rows, GOLD)

    def test_strings_docstrings_and_private_declarations_not_extracted(self):
        text = '\n'.join(['"""def doc_fake(): pass"""', 'text = "def string_fake(): pass"',
                          "_value = '''async def another_fake(): pass'''", '# class CommentFake: pass',
                          'def _private(): pass', 'class _Hidden: pass', 'async def real(): pass', ''])
        rows = self.extract(text)
        self.assertEqual([(r['name'], r['line'], r['kind']) for r in rows], [('text', 2, 'constant'), ('real', 7, 'async_function')])
        self.assertFalse(any('fake' in r['name'] or r['name'].startswith('_') for r in rows))

    def test_functions_classes_literal_constants_and_local_scope(self):
        rows = self.extract('COUNT = 3\nanswer: int = 42\ndef run():\n    def hidden(): pass\n    return 1\nclass Public:\n    def method(self): pass\n')
        self.assertEqual([(r['name'], r['line'], r['kind']) for r in rows], [('COUNT', 1, 'constant'), ('answer', 2, 'constant'), ('run', 3, 'function'), ('Public', 6, 'class')])
        self.assertEqual([r['symbol'] for r in rows], ['cafe.authority_channel.' + n for n in ['COUNT', 'answer', 'run', 'Public']])
        self.assertTrue(all(r['owner'] == 'connectors/' for r in rows))

    def test_literal_all_restricts_declared_exports(self):
        rows = self.extract("__all__ = ['run']\nCOUNT = 3\ndef run(): pass\ndef other(): pass\n")
        self.assertEqual([(r['name'], r['line']) for r in rows], [('run', 3)])
        self.assertEqual(self.extract('__all__: list[str] = []\ndef hidden_by_all(): pass\n'), [])
        for text in ["__all__ = ['missing']\n", "from x import value\n__all__=['value']\n", "__all__=['_private']\ndef _private(): pass\n", "__all__=['run','run']\ndef run(): pass\n"]:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_calculated_or_mutated_all_abstains(self):
        for text in ["__all__ = compute()\n", "__all__ = ('run',)\n", "__all__ = ['run'] + []\n", "__all__=[]\n__all__.append('run')\n", "__all__=[]\n__all__ += ['run']\n", "__all__=[]\n_alias=__all__\n", "if True:\n    __all__ = []\n", "def __all__(): pass\n", "from x import __all__\n"]:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_star_import_exec_globals_and_syntax_abstain(self):
        for text in ['from x import *\n', "__all__=[]\nfrom x import *\n", "exec('def hidden(): pass')\n", "globals()['hidden'] = 1\n", "if True:\n    globals()['hidden'] = 1\n", 'def broken(:\n', 'return 1\n', 'if True:\n    def conditional(): pass\n']:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)
        # Deferred function bodies cannot fabricate an import-time declaration.
        rows = self.extract("def public():\n    exec('def deferred(): pass')\n")
        self.assertEqual([r['name'] for r in rows], ['public'])

    def test_imported_helpers_not_assumed_local_exports(self):
        self.assertEqual(self.extract('from x import helper, VALUE\nimport os\n'), [])
        for text in ['def run(): pass\nfrom x import run\n', 'from x import run\ndef run(): pass\n', 'def run(): pass\nrun=1\n']:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_unlisted_source_never_read_or_imported(self):
        self.path.write_text('from .outside import imported\ndef real(): pass\n')
        outside = self.root / 'connectors/cafe/outside.py'
        outside.write_bytes(b'\xffexec(\'unsafe\')')
        original = Path.read_bytes
        reads = []
        def read(path):
            if path == outside:
                raise AssertionError('unlisted source opened')
            reads.append(path)
            return original(path)
        with mock.patch.object(Path, 'read_bytes', read):
            report = x.extract(self.root, FILES, FILES + ['connectors/cafe/outside.py'])
        self.assertEqual(reads, [self.path])
        self.assertEqual([r['name'] for r in report['files'][0]['symbols']], ['real'])

    def test_source_never_executed(self):
        marker = self.root / 'must_not_exist'
        self.extract("__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('executed')\ndef run(): pass\n")
        self.assertFalse(marker.exists())

    def test_hash_bytes_before_encoding_detection_and_decode(self):
        raw = b'# coding: latin-1\r\nname = "caf\xe9"\r\n'
        self.path.write_bytes(raw)
        report = x.extract(self.root, FILES, FILES)['files'][0]
        self.assertEqual(report['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report['symbols'][0]['line'], 2)
        self.path.write_bytes(b'\xff')
        seen = []
        original_hash = x.hashlib.sha256
        original_encoding = x.tokenize.detect_encoding
        def digest(data):
            seen.append('hashed')
            return original_hash(data)
        def encoding(reader):
            self.assertEqual(seen, ['hashed'])
            return original_encoding(reader)
        with mock.patch.object(x.hashlib, 'sha256', side_effect=digest), mock.patch.object(x.tokenize, 'detect_encoding', side_effect=encoding), self.assertRaises(x.ExtractionError):
            x.extract(self.root, FILES, FILES)

    def test_paths_versioning_ownership_and_order_fail_before_reads(self):
        self.path.write_text('def run(): pass\n')
        for paths, inventory in [([], FILES), (FILES + FILES, FILES), (['../escape.py'], FILES), (FILES, []), (['connectors/cafe/missing.py'], ['connectors/cafe/missing.py'])]:
            with self.subTest(paths=paths), mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('source read before path validation')), self.assertRaises(x.ExtractionError):
                x.extract(self.root, paths, inventory)
        (self.root / 'connectors/cafe/alias.py').symlink_to(self.path)
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, ['connectors/cafe/alias.py'], ['connectors/cafe/alias.py'])
        (self.root / 'catalogue/ownership.json').write_text('{}')
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, FILES, FILES)

    def test_package_init_and_multiple_files(self):
        init = self.root / 'connectors/cafe/__init__.py'
        init.write_text('VERSION = 1\n')
        self.path.write_text('def run(): pass\n')
        files = sorted(FILES + ['connectors/cafe/__init__.py'])
        rows = x.extract(self.root, files, files)['files']
        self.assertEqual(rows[0]['symbols'][0]['symbol'], 'cafe.VERSION')
        self.assertEqual(rows[1]['symbols'][0]['symbol'], 'cafe.authority_channel.run')

    def test_cli_determinism(self):
        command = [sys.executable, '-B', str(ROOT / 'tools/extract_py.py'), '--files', SOURCE]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        first = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
        second = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertTrue(first.stdout.endswith(b'\n'))
        self.assertEqual(json.loads(first.stdout)['files'][0]['symbols'], GOLD)

    def test_cli_refusal_status_and_sanitized_errors(self):
        import shutil
        tools = self.root / 'tools'
        tools.mkdir()
        for name in ['extract_py.py', 'qualify_cafe_router.py']:
            shutil.copyfile(ROOT / 'tools' / name, tools / name)
        inventory = self.root / 'inventory.json'
        inventory.write_text(json.dumps(FILES))
        cases = [("__all__ = compute()\n", 'computed_or_mutated_all'),
                 ('from x import *\n', 'star_import_unsupported'),
                 ("globals()['exported'] = 1\n", 'dynamic_module_namespace'),
                 ("exec('def exported(): pass')\n", 'dynamic_module_namespace'),
                 ('def bad(:\n', 'syntax_error')]
        for source, code in cases:
            self.path.write_text(source)
            result = subprocess.run([sys.executable, '-B', str(tools / 'extract_py.py'), '--files', SOURCE, '--inventory', str(inventory)], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            error = json.loads(result.stderr)
            self.assertEqual(error['error'], code)
            self.assertEqual(error['path'], SOURCE)
            self.assertNotIn(str(self.root), result.stderr)

    def test_python_qualification_requires_executed_passing_nonempty_test(self):
        for check in [q.Check('python', True, 0, 1), q.Check('python', False, 1, 1), q.Check('python', False, 0, 0)]:
            def selection(root, directory, pattern):
                return check if pattern == 'test_extract_py.py' else q.Check(pattern, True, 0, 1)
            with self.subTest(check=check), mock.patch.object(q, 'run_selection', side_effect=selection) as invoked, mock.patch.object(q, 'load_inventory', return_value=(['fixture'], 'fixture')), mock.patch.object(q, 'check_ownership', return_value=q.Check('ownership', True)):
                legs, source = q.collect(self.root)
                report = q.summarize(legs, source)
                q.validate_report(report, legs)
                self.assertEqual(report['legs']['extractor_python']['status'], 'pass' if check.passed else 'fail')
                self.assertEqual(q.exit_code(legs), 0 if check.passed else 1)
                self.assertEqual(report['legs']['resolver_index']['status'], 'pass')
                self.assertEqual(report['verdict'], 'router available (uncalibrated)' if check.passed else 'router unavailable')
                invoked.assert_any_call(self.root, 'tests/bootstrap', 'test_extract_py.py')


if __name__ == '__main__':
    unittest.main()
