"""Host sink tests: actual filesystem negatives, bounded input and cross-reader."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import logging_storage as sink
from cafe import runtime_logging as trace


def row(seq=1):
    return {'schema': 'cafe.runtime_trace.v1', 'seq': seq, 'elapsed_ms': 0,
            'event': 'host_start', 'op': 'none', 'bridge': 'unknown', 'reason': 'none',
            'result_state': 'unknown', 'result_code': 'unknown_result', 'stage': 'none',
            'capsule_count': None, 'paired_feed_count': None, 'listed_window_count': None,
            'minimized_window_count': None, 'visible_window_count': None, 'recorded_frames': None,
            'recording_state': 'unknown', 'base_commit': None, 'authority_sha256': 'a'*64}


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


class DiagnosticSink(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-sink-test-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / 'diagnostics'

    def open(self, root=None):
        value = sink.Sink(self.root if root is None else root)
        self.addCleanup(value.close)
        return value

    def alias(self, link, target):
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, 'native junction fixture creation failed')
            self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        else:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(lambda: link.unlink() if link.is_symlink() else None)

    def test_fresh_exclusive_stream_cross_reader_and_permissions(self):
        value = self.open(); value.write(row()); value.write(row(2)); value.close()
        self.assertRegex(value.name, r'^trace-[a-f0-9]{32}\.jsonl$')
        self.assertEqual(trace.recent(self.root)['rows'], [row(), row(2)])
        other = self.open(); self.assertNotEqual(value.name, other.name)
        other.write(row()); other.close()
        if os.name != 'nt':
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(value.path.stat().st_mode & 0o777, 0o600)
        original = value.path.read_bytes()
        with mock.patch.object(sink.secrets, 'token_hex', return_value=value.name[6:-6]):
            self.assertEqual(sink.consume(self.root, io.BytesIO(encoded(row()))), sink.EXIT_UNAVAILABLE)
        self.assertEqual(value.path.read_bytes(), original)

    def test_existing_root_and_ancestor_link_or_junction_rejected(self):
        target = self.parent / 'actual'; target.mkdir()
        self.alias(self.root, target)
        with self.assertRaises((OSError, ValueError)): sink.Sink(self.root)
        self.assertEqual(list(target.iterdir()), [])
        nested = target / 'diagnostics'; nested.mkdir()
        alias = self.parent / 'alias'
        self.alias(alias, target)
        with self.assertRaises((OSError, ValueError)): sink.Sink(alias / 'diagnostics')
        self.assertEqual(list(nested.iterdir()), [])

    def test_rename_swap_while_open_never_redirects(self):
        value = self.open(); value.write(row())
        moved = self.parent / 'moved'
        if os.name == 'nt':
            # A GENERIC_WRITE handle could issue FSCTL_SET_REPARSE_POINT. It must
            # fail specifically with sharing violation while anchors are held.
            handle = value.windows.api.CreateFileW(str(self.root), 0x40000000, 0x1 | 0x2 | 0x4,
                                                   None, 3, 0x02000000 | 0x00200000, None)
            error = value.windows.ctypes.get_last_error()
            if handle != value.windows.invalid:
                value.windows.api.CloseHandle(handle)
            self.assertEqual(handle, value.windows.invalid, 'anchor admitted GENERIC_WRITE')
            self.assertEqual(error, 32, 'anchor write handle was not denied by sharing')
            # This must be a real native denial, never a skip or mocked proof.
            with self.assertRaises(OSError): self.root.rename(moved)
            with self.assertRaises(OSError): value.path.rename(self.parent / 'stolen.jsonl')
            value.write(row(2))
            self.assertEqual(trace.recent(self.root)['rows'], [row(), row(2)])
        else:
            self.root.rename(moved); self.root.mkdir(mode=0o700)
            with self.assertRaises((OSError, ValueError)): value.write(row(2))
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual((moved / value.name).read_bytes(), encoded(row()))

    def test_hardlink_after_open_rejected_or_natively_denied(self):
        value = self.open(); value.write(row())
        alias = self.parent / 'leak.jsonl'
        if os.name == 'nt':
            # Native Windows sharing must deny the link operation itself.
            with self.assertRaises(OSError): os.link(value.path, alias)
            self.assertFalse(alias.exists())
            value.write(row(2))
        else:
            os.link(value.path, alias)
            with self.assertRaises((OSError, ValueError)): value.write(row(2))
            self.assertEqual(alias.read_bytes(), encoded(row()))

    def test_complete_bad_input_and_canaries_never_written(self):
        for index, raw in enumerate([b'{"secret":"CANARY"}\n', b'{}\n', b'\n', b'{}',
                                     encoded({**row(), 'reason': 'CANARY'}),
                                     encoded({**row(), 'seq': True}),
                                     encoded(row()).replace(b'"seq":1', b'"seq":1,"seq":1'),
                                     encoded(row()).replace(b'"elapsed_ms":0', b'"elapsed_ms":NaN')]):
            with self.subTest(index=index):
                parent = self.parent / str(index); parent.mkdir()
                root = parent / 'diagnostics'
                self.assertEqual(sink.consume(root, io.BytesIO(raw)), sink.EXIT_INVALID_INPUT)
                self.assertEqual(b''.join(p.read_bytes() for p in root.iterdir()), b'')
        value = self.open(); value.write(row())
        with self.assertRaises(ValueError): value.write({**row(2), 'extra': 'CANARY'})
        self.assertNotIn(b'CANARY', value.path.read_bytes())
        with self.assertRaises(ValueError): value.write(row(3))
        self.assertEqual(value.path.read_bytes(), encoded(row()))

    def test_eof_limits_and_partial_final_line(self):
        self.assertEqual(sink.consume(self.root, io.BytesIO(b'')), sink.EXIT_INVALID_INPUT)
        for label, raw in [('partial', encoded(row())[:-1]),
                           ('line', b' ' * sink.MAX_LINE_BYTES + b'\n'),
                           ('rows', b''.join(encoded(row(i)) for i in range(1, sink.MAX_ROWS+2)))]:
            with self.subTest(label=label):
                parent = self.parent / label; parent.mkdir()
                self.assertEqual(sink.consume(parent/'diagnostics', io.BytesIO(raw)), sink.EXIT_INVALID_INPUT)
        parent = self.parent / 'bytes'; parent.mkdir()
        # Valid JSON with excess whitespace must count toward the stdin byte cap too.
        raw = encoded(row())
        with mock.patch.object(sink, 'MAX_FILE_BYTES', len(raw)-1):
            self.assertEqual(sink.consume(parent/'diagnostics', io.BytesIO(raw)), sink.EXIT_INVALID_INPUT)
        value = self.open()
        with mock.patch.object(sink, 'MAX_FILE_BYTES', len(encoded(row()))-1):
            with self.assertRaises(ValueError): value.write(row())
        self.assertEqual(value.path.read_bytes(), b'')

    def test_final_fsync_failure_returns_unavailable_and_cli_emits_no_payload(self):
        with mock.patch.object(sink.os, 'fsync', side_effect=OSError('CANARY')):
            self.assertEqual(sink.consume(self.root, io.BytesIO(encoded(row()))), sink.EXIT_UNAVAILABLE)
        stdin = mock.Mock(buffer=io.BytesIO(encoded(row())))
        with mock.patch.object(sink.sys, 'stdin', stdin), mock.patch.object(sink.sys, 'stdout', io.StringIO()) as out, mock.patch.object(sink.sys, 'stderr', io.StringIO()) as err:
            self.assertEqual(sink.main(['--root', str(self.root)]), sink.EXIT_OK)
            self.assertEqual(sink.main(['--unknown', 'CANARY']), sink.EXIT_UNAVAILABLE)
            self.assertEqual(out.getvalue(), '')
            self.assertEqual(err.getvalue(), '')

    def test_direct_isolated_script_launch_has_no_output(self):
        parent = self.parent / 'isolated'; parent.mkdir()
        result = subprocess.run([sys.executable, '-I', '-S', '-B',
                                 str(ROOT/'connectors/cafe/logging_storage.py'),
                                 '--root', str(parent/'diagnostics')],
                                input=encoded(row()), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.parent, timeout=10)
        self.assertEqual(result.returncode, sink.EXIT_OK)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')
        self.assertEqual(trace.recent(parent/'diagnostics')['rows'], [row()])

    def test_schema_pin_root_spelling_and_existing_file_substitution(self):
        with mock.patch.object(trace, 'SCHEMA_SHA256', '0'*64):
            self.assertEqual(sink.consume(self.root, io.BytesIO(encoded(row()))), sink.EXIT_UNAVAILABLE)
        self.assertFalse(self.root.exists())
        for root in (Path('diagnostics'), self.parent/'other', self.parent/'x'/'..'/'diagnostics'):
            with self.assertRaises(ValueError): sink.Sink(root)
        value = self.open(); value.write(row())
        if os.name == 'nt':
            with self.assertRaises(OSError): value.path.unlink()
        else:
            value.path.unlink(); value.path.write_bytes(b'CANARY')
            with self.assertRaises((OSError, ValueError)): value.write(row(2))
            self.assertEqual(value.path.read_bytes(), b'CANARY')


if __name__ == '__main__':
    unittest.main()
