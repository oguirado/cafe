"""Bounded independent privacy, confinement and stdio diagnostic tests."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import runtime_logging as trace
from cafe import mcp_server as mcp


def row(seq=1):
    return {'schema': 'cafe.runtime_trace.v1', 'seq': seq, 'elapsed_ms': 0,
            'event': 'host_start', 'op': 'none', 'bridge': 'unknown', 'reason': 'none',
            'result_state': 'unknown', 'result_code': 'unknown_result', 'stage': 'none',
            'capsule_count': None, 'paired_feed_count': None, 'listed_window_count': None,
            'minimized_window_count': None, 'visible_window_count': None, 'recorded_frames': None,
            'recording_state': 'unknown', 'base_commit': None, 'authority_sha256': 'a' * 64}


def ready(server):
    server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'protocolVersion': mcp.PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': {}}})
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return server


def call(server, arguments=None):
    return server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                          'params': {'name': 'recent_diagnostics', 'arguments': {} if arguments is None else arguments}})


class DiagnosticTrace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-trace-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / ('trace-' + 'a' * 32 + '.jsonl')
        self.write([row()])

    def write(self, rows):
        self.file.write_bytes(b''.join(json.dumps(value).encode() + b'\n' for value in rows))

    def test_tail_flags_and_newest_deterministic(self):
        self.write([row(i) for i in range(1, 4)])
        result = trace.recent(self.root, 2)
        self.assertEqual([x['seq'] for x in result['rows']], [2, 3])
        self.assertEqual((result['incomplete_tail'], result['truncated']), (False, True))
        with self.file.open('ab') as stream:
            stream.write(b'{"unfinished":')
        self.assertTrue(trace.recent(self.root)['incomplete_tail'])
        other = self.root / ('trace-' + 'b' * 32 + '.jsonl')
        value = row(); value['event'] = 'snapshot'
        other.write_text(json.dumps(value) + '\n')
        os.utime(self.file, ns=(123000000000, 123000000000))
        os.utime(other, ns=(123000000000, 123000000000))
        self.assertEqual(trace.recent(self.root)['rows'][0]['event'], 'snapshot')

    def test_exact_fields_types_counts_enums_and_secrets(self):
        mutations = [('extra', 'SECRET_CANARY'), ('seq', True), ('seq', 0), ('elapsed_ms', True),
                     ('elapsed_ms', 86400001), ('capsule_count', 3), ('paired_feed_count', 5),
                     ('listed_window_count', 4), ('visible_window_count', -1),
                     ('minimized_window_count', False), ('recorded_frames', 4294967296),
                     ('recorded_frames', 0.5), ('result_code', 'SECRET_CANARY'),
                     ('authority_sha256', 'SECRET_CANARY'), ('base_commit', 'A' * 40),
                     ('event', []), ('recording_state', None)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                value_row = row(); value_row[key] = value
                self.write([value_row])
                result = call(ready(mcp.Server(diagnostics=self.root)))
                self.assertTrue(result['result']['isError'])
                self.assertNotIn('SECRET_CANARY', json.dumps(result))
        value = row(); del value['reason']; self.write([value])
        with self.assertRaises(trace.TraceError): trace.recent(self.root)
        value = row(); value.update(capsule_count=2, paired_feed_count=4, listed_window_count=3,
                                  minimized_window_count=3, visible_window_count=3, recorded_frames=4294967295,
                                  base_commit='b'*40)
        self.write([value]); self.assertEqual(trace.recent(self.root)['rows'], [value])

    def test_bad_complete_lines_empty_and_sequence_gaps_fail(self):
        cases = [b'', b'\n', b'{}\n', b'{"unfinished":', b'{"seq":1,"seq":1}\n',
                 json.dumps(row()).replace('"elapsed_ms": 0', '"elapsed_ms": NaN').encode() + b'\n',
                 json.dumps(row()).encode() + b'\n' + json.dumps(row(3)).encode() + b'\n']
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                self.file.write_bytes(raw)
                with self.assertRaises(trace.TraceError): trace.recent(self.root)
        self.write([row()]); self.file.write_bytes(self.file.read_bytes() + b'bad complete\n')
        with self.assertRaises(trace.TraceError): trace.recent(self.root)

    def test_byte_row_and_entry_caps(self):
        self.file.write_bytes(b'x' * (trace.MAX_FILE_BYTES + 1))
        with self.assertRaises(trace.TraceError): trace.recent(self.root)
        self.file.write_bytes(b'x' * (trace.MAX_LINE_BYTES + 1))
        with self.assertRaises(trace.TraceError): trace.recent(self.root)
        self.file.write_bytes(b'x' * trace.MAX_LINE_BYTES + b'\n')
        with self.assertRaises(trace.TraceError): trace.recent(self.root)
        self.write([row(i) for i in range(1, trace.MAX_ROWS + 2)])
        with self.assertRaises(trace.TraceError): trace.recent(self.root)
        self.write([row()])
        for i in range(trace.MAX_ENTRIES):
            (self.root / ('trace-' + f'{i:032x}' + '.jsonl')).write_bytes(self.file.read_bytes())
        with self.assertRaises(trace.TraceError): trace.recent(self.root)

    def test_symlink_ancestor_hardlink_special_and_unknown_entries_rejected(self):
        if os.name != 'nt':
            alternate = self.root.parent / (self.root.name + '-link')
            alternate.symlink_to(self.root, target_is_directory=True)
            self.addCleanup(alternate.unlink)
            with self.assertRaises(trace.TraceError): trace.recent(alternate)
            nested = self.root / 'nested'; nested.mkdir()
            with self.assertRaises(trace.TraceError): trace.recent(alternate / 'nested')
            nested.rmdir()
            linked = self.root / ('trace-' + 'b'*32 + '.jsonl')
            os.link(self.file, linked)
            with self.assertRaises(trace.TraceError): trace.recent(self.root)
            linked.unlink()
            self.file.unlink(); self.file.symlink_to(ROOT / 'src/contracts/runtime_logging.json')
            with self.assertRaises(trace.TraceError): trace.recent(self.root)
            self.file.unlink(); os.mkfifo(self.file)
            with self.assertRaises(trace.TraceError): trace.recent(self.root)
            self.file.unlink(); self.write([row()])
        (self.root / 'token.txt').write_text('SECRET_CANARY')
        with self.assertRaises(trace.TraceError): trace.recent(self.root)

    def test_missing_binding_and_schema_hash_fail_closed(self):
        missing = ready(mcp.Server(diagnostics=self.root / 'missing'))
        self.assertTrue(call(missing)['result']['isError'])
        with mock.patch.object(trace, 'SCHEMA_SHA256', '0' * 64):
            self.assertTrue(call(ready(mcp.Server(diagnostics=self.root)))['result']['isError'])
        with self.assertRaises(trace.TraceError): trace.recent(Path('relative'))
        with self.assertRaises(ValueError): mcp.Server()

    def test_tools_and_arguments_no_control_no_caller_paths(self):
        server = ready(mcp.Server(diagnostics=self.root))
        reply = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})['result']
        self.assertEqual([x['name'] for x in reply['tools']], ['recent_diagnostics'])
        self.assertTrue(reply['tools'][0]['annotations']['readOnlyHint'])
        self.assertFalse(reply['tools'][0]['inputSchema']['additionalProperties'])
        self.assertFalse(call(server)['result']['isError'])
        for args in ({'path': str(self.file)}, {'limit': True}, {'limit': 0}, {'limit': 33}, {'limit': 1.0}, []):
            self.assertEqual(call(server, args)['error']['code'], -32602)
        for limit in (1, 32): self.assertFalse(call(server, {'limit': limit})['result']['isError'])
        chat = ready(mcp.Server(self.root))
        tools = chat.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(len(tools), 6)
        self.assertEqual(call(chat)['error']['code'], -32602)
        both = ready(mcp.Server(self.root, diagnostics=self.root))
        self.assertEqual(len(both.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})['result']['tools']), 7)
        self.assertEqual(server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                      'params': {'name': 'start_recording', 'arguments': {'request_id': 'safe'}}})['error']['code'], -32602)

    def test_cli_diagnostics_only_real_framing_without_subprocess(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': mcp.PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': {}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'recent_diagnostics'}}]
        stdin = mock.Mock(buffer=io.BytesIO(b''.join(json.dumps(x).encode()+b'\n' for x in requests)))
        stdout = mock.Mock(buffer=io.BytesIO())
        with mock.patch.object(mcp.sys, 'stdin', stdin), mock.patch.object(mcp.sys, 'stdout', stdout):
            self.assertEqual(mcp.main(['--diagnostics', str(self.root)]), 0)
        replies = [json.loads(x) for x in stdout.buffer.getvalue().splitlines()]
        self.assertFalse(replies[-1]['result']['isError'])
        self.assertEqual(replies[-1]['result']['structuredContent']['data']['rows'], [row()])


if __name__ == '__main__':
    unittest.main()
