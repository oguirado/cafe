"""Storage-root identity and alias regressions; no network or new packages."""
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import twitch_storage as storage


class TwitchPathTests(unittest.TestCase):
    def test_plain_root_is_canonical(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            writer = storage.JsonlWriter(root, 'example')
            try:
                self.assertEqual(writer.root, root)
                self.assertEqual(writer.folder, root / 'twitch' / 'example')
            finally:
                writer.close()

    def test_same_identity_alias_is_canonicalized_before_child_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            alias = root.parent / (root.name + '_alias')
            original_stat, original_lstat, original_resolve = Path.stat, Path.lstat, Path.resolve
            resolutions = []
            def mapped(path):
                return root if path == alias else path
            def resolve(path, *args, **kwargs):
                if path == alias:
                    resolutions.append(path)
                return original_resolve(mapped(path), *args, **kwargs)
            with mock.patch.object(Path, 'stat', lambda path, *a, **k: original_stat(mapped(path), *a, **k)), mock.patch.object(Path, 'lstat', lambda path: original_lstat(mapped(path))), mock.patch.object(Path, 'resolve', resolve):
                writer = storage.JsonlWriter(alias, 'example')
                try:
                    self.assertEqual(writer.root, root)
                    self.assertEqual(resolutions, [alias])
                    self.assertTrue((root / 'twitch' / 'example').is_dir())
                finally:
                    writer.close()

    def test_symlink_root_is_rejected_before_resolving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            original = Path.lstat
            def lstat(path):
                info = original(path)
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_ino=info.st_ino, st_dev=info.st_dev) if path == root else info
            with mock.patch.object(Path, 'lstat', lstat):
                with self.assertRaisesRegex(storage.ChatError, '^unsafe_path$'):
                    storage.JsonlWriter(root, 'example')

    def test_symlink_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); child = root / 'child'; child.mkdir()
            original = Path.lstat
            def lstat(path):
                info = original(path)
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_ino=info.st_ino, st_dev=info.st_dev) if path == root else info
            with mock.patch.object(Path, 'lstat', lstat):
                with self.assertRaisesRegex(storage.ChatError, '^unsafe_path$'):
                    storage.JsonlWriter(child, 'example')

    def test_windows_reparse_guard_remains_closed(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_ino=7, st_dev=11, st_file_attributes=0)
        with mock.patch.object(storage, 'os', SimpleNamespace(name='nt')), mock.patch.object(storage.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, create=True):
            storage._safe_stat(info, directory=True)
            info.st_file_attributes = 0x400
            with self.assertRaisesRegex(storage.ChatError, '^unsafe_path$'):
                storage._safe_stat(info, directory=True)

    def test_parent_components_and_missing_root_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for value in [root / '..' / root.name, root / 'absent']:
                with self.subTest(case=value.name):
                    with self.assertRaisesRegex(storage.ChatError, '^unsafe_path$'):
                        storage.JsonlWriter(value, 'example')

    def test_root_identity_change_during_resolution_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); original = Path.lstat; seen = 0
            def lstat(path):
                nonlocal seen
                info = original(path)
                if path == root:
                    seen += 1
                    if seen > 1:
                        return SimpleNamespace(st_mode=info.st_mode, st_ino=info.st_ino + 1, st_dev=info.st_dev, st_file_attributes=getattr(info, 'st_file_attributes', 0))
                return info
            with mock.patch.object(Path, 'lstat', lstat):
                with self.assertRaisesRegex(storage.ChatError, '^unsafe_path$'):
                    storage.JsonlWriter(root, 'example')
