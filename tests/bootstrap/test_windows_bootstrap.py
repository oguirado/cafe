"""Portable failure contrasts for Windows aliases, shells and closed handles."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from host_shell import select_bash, posix_argument

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import build_cafe_nodes as builder
import verify_toolchain
import extract_rust
import extract_py

SPEC = importlib.util.spec_from_file_location('windows_queue_contrasts', ROOT / 'ctl/link_queue.py')
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class WindowsBootstrapTests(unittest.TestCase):
    def test_exact_toolchain_guard_rejects_version_drift_and_missing_output(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, command[0] + ' 1.97.0 (fixture)\n', '')
        self.assertEqual(verify_toolchain.verify('1.97.0', run)['status'], 'pass')
        self.assertEqual(verify_toolchain.verify('1.99.1', run)['status'], 'blocked')
        missing = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '', '')
        self.assertEqual(verify_toolchain.verify('1.97.0', missing)['status'], 'blocked')

    def test_missing_sources_keep_closed_extractor_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(strict=True)
            with self.assertRaises(extract_rust.ExtractionError):
                extract_rust.extract(root, ['core/src/main.rs'], ['core/src/main.rs'])
            with self.assertRaises(extract_py.ExtractionError):
                extract_py.extract(root, ['connectors/cafe/missing.py'], ['connectors/cafe/missing.py'])

    def test_wsl_bash_is_not_a_git_bash_fallback(self):
        which = lambda name: r'C:\Windows\System32\bash.exe' if name == 'bash' else None
        with self.assertRaisesRegex(RuntimeError, 'BLOCKED'):
            select_bash(windows=True, which=which, exists=lambda _: True, environ={})

    def test_non_git_path_bash_cannot_mask_missing_git_bash(self):
        which = lambda name: str(Path('other-bash') / 'bash.exe') if name == 'bash' else None
        with self.assertRaisesRegex(RuntimeError, 'BLOCKED'):
            select_bash(windows=True, which=which, exists=lambda _: True, environ={})

    def test_git_bash_is_selected_explicitly(self):
        git = r'C:\Program Files\Git\cmd\git.exe'
        bash = r'C:\Program Files\Git\bin\bash.exe'
        selected = select_bash(windows=True,
            which=lambda name: git if name == 'git' else r'C:\Windows\System32\bash.exe',
            exists=lambda path: path == bash, environ={})
        self.assertEqual(selected, bash)

    def test_native_paths_preserve_spaces_in_a_single_argument(self):
        self.assertEqual(posix_argument(r'D:\a folder\cafe\ctl\unit.sh'), '/d/a folder/cafe/ctl/unit.sh')
        self.assertEqual(posix_argument('-n'), '-n')
        with self.assertRaisesRegex(RuntimeError, 'BLOCKED'):
            posix_argument(chr(92) * 2 + 'server' + chr(92) + 'share' + chr(92) + 'unit.sh')

    def test_real_escape_still_rejected_after_root_canonicalization(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(strict=True)
            repo = root / 'repo'; repo.mkdir()
            target = repo / 'file.txt'
            outside = root / 'outside.txt'
            original = Path.resolve
            def resolve(path, *args, **kwargs):
                if path == target:
                    return outside
                return original(path, *args, **kwargs)
            self.assertEqual(queue.safe_path(repo, 'file.txt'), target)
            # Simulates an OS-level junction/alias resolving outside the owner.
            with mock.patch.object(Path, 'resolve', resolve):
                with self.assertRaisesRegex(ValueError, 'escaping_patch_path'):
                    queue.safe_path(repo, 'file.txt')
            self.assertFalse(outside.exists())
            with self.assertRaisesRegex(ValueError, 'unsafe_patch_path'):
                queue.safe_path(repo, '../outside.txt')

    def test_index_temporary_handle_closed_before_replace_or_cleanup(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(strict=True)
                (root / 'catalogue').mkdir()
                destination = root / 'catalogue/nodes.jsonl'
                destination.write_bytes(b'before')
                real_factory = tempfile.NamedTemporaryFile
                real_replace = os.replace
                streams = []
                def factory(*args, **kwargs):
                    stream = real_factory(*args, **kwargs); streams.append(stream)
                    return stream
                def replace(source, target):
                    self.assertTrue(streams[-1].closed)
                    if fail:
                        raise PermissionError('injected replacement failure')
                    return real_replace(source, target)
                with mock.patch.object(builder.tempfile, 'NamedTemporaryFile', factory), mock.patch.object(builder.os, 'replace', replace):
                    if fail:
                        with self.assertRaises(PermissionError):
                            builder.write_products(root, {'catalogue/nodes.jsonl': b'after'})
                    else:
                        builder.write_products(root, {'catalogue/nodes.jsonl': b'after'})
                self.assertEqual(destination.read_bytes(), b'before' if fail else b'after')
                self.assertEqual(list((root / 'catalogue').iterdir()), [destination])


if __name__ == '__main__':
    unittest.main()
