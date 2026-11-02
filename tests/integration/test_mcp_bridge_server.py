"""Synthetic dispatcher contract tests; no live native or dependency acceptance.

The optional modules are strict in-process contract doubles. Cursor tests prove
delegation and preservation of closed errors, not the absent modules' algorithms.
Every guide live marker stays pending and every identifier below is synthetic.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import inspect
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_server as mcp

ORIGINAL_NAMES = {
    'list_channels_and_days', 'recent_messages', 'search_messages',
    'top_chatters', 'message_rate', 'session_summary',
}
RUNTIME_NAMES = {'list_capsules', 'list_candidate_windows', 'recording_status', 'obs_status'}
CHAT_NAMES = {'messages_by_user', 'messages_between'}
STATISTICS_NAMES = {'chat_statistics', 'chat_activity'}
READ_NAMES = RUNTIME_NAMES | CHAT_NAMES | STATISTICS_NAMES
ALL_NAMES = ORIGINAL_NAMES | READ_NAMES | {'demo_guide'}
MODULE_NAMES = ('cafe.mcp_runtime_queries', 'cafe.mcp_chat_digest', 'cafe.mcp_operator_runbook')
GUIDE_URI = 'cafe://demo/guide'
INTERVAL = {'channel': 'synthetic_channel', 'from': '2000-01-01T00:00:00Z',
            'to': '2000-01-01T00:01:00Z'}
SCAN_FIELDS = {'corrupt_lines', 'truncated_lines', 'bytes_scanned', 'files_scanned', 'scan_limited'}


def request(method, params=None, identity=1):
    result = {'jsonrpc': '2.0', 'id': identity, 'method': method}
    if params is not None:
        result['params'] = params
    return result


def initialize(server):
    result = server.handle(request('initialize', {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'synthetic-peer', 'version': '1'},
    }))
    assert result['result']['protocolVersion'] == '2025-11-25'
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return result


def read_result(data=None, *, error=None, scan=None, cursor=None, complete=True):
    return {'schema': 'cafe.mcp_read.v1',
            'availability': 'available' if error in (None, 'scan_limited') else 'unavailable',
            'data': data, 'page': {'next_cursor': cursor, 'complete': complete},
            'scan': scan, 'error': error}


def synthetic_event(second=0, text='synthetic literal'):
    return {'schema': 'cafe.chat_event.v1', 'channel': 'synthetic_channel',
            'received_at': {'monotonic_ns': str(second),
                            'utc': '2000-01-01T00:00:' + str(second).zfill(2) + 'Z'},
            'sent_at': None, 'message_id': 'synthetic-message-' + str(second),
            'user_login': 'synthetic_user', 'display_name': 'Synthetic',
            'badges': [], 'text': text, 'kind': 'message', 'session_id': 'synthetic-session'}


def runtime_data(name):
    if name == 'list_capsules':
        return [{'capsule_id': 'synthetic-capsule', 'slot': 'slot-1',
                 'person_label': 'Synthetic', 'generation': '1',
                 'feeds': [{'feed_id': 'synthetic-feed', 'role': 'stream', 'generation': '1'}]}]
    if name == 'list_candidate_windows':
        return [{'window_token': 'synthetic-window', 'generation': '1', 'layout_revision': '1',
                 'title': 'Synthetic window', 'executable': 'synthetic.exe',
                 'rect': {'width': 640, 'height': 480}, 'visible': True, 'minimized': False}]
    if name == 'recording_status':
        return {'capsules': [{'capsule_id': 'synthetic-capsule', 'state': 'idle',
                             'feeds': [{'feed_id': 'synthetic-feed', 'state': 'idle',
                                        'written_frames': None, 'backpressure': '0'}]}],
                'recorder_available': None}
    return {'available': None, 'configured': None, 'state': None}


def aggregate():
    return {'message_count': 3, 'unique_chatters': 1,
            'messages_per_bucket': [{'start': INTERVAL['from'], 'count': 3}],
            'top_words': [{'word': 'synthetic', 'count': 3}],
            'peaks': [{'start': INTERVAL['from'], 'count': 3}],
            'top_emotes': None, 'emotes_status': 'metadata_unavailable', 'complete': True}


def guide(step=None, error=None):
    ids = list(range(1, 8)) if step is None else [step]
    steps = [{'id': i, 'instruction': 'Synthetic instruction',
              'expected': 'Synthetic expected result', 'evidence_required': 'Synthetic evidence',
              'depends_on': [] if i == 1 else [i - 1], 'live_status': 'pending'} for i in ids]
    return {'schema': 'cafe.demo_guide.v1', 'steps': [] if error else steps,
            'control_mode': 'direct_with_optional_confirmation', 'error': error}


def definitions(names):
    page = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
            'cursor': {'type': ['string', 'null'], 'maxLength': 512}}
    interval = {'channel': {'type': 'string', 'pattern': '^[a-z0-9_]{3,25}$'},
                'from': {'type': 'string', 'maxLength': 30},
                'to': {'type': 'string', 'maxLength': 30}}
    result = []
    for name in sorted(names):
        required = []
        if name in {'list_capsules', 'list_candidate_windows'}:
            fields = deepcopy(page)
        elif name in CHAT_NAMES or name == 'chat_activity':
            fields = {**deepcopy(interval), **deepcopy(page)}
            required = ['channel', 'from', 'to']
        elif name == 'chat_statistics':
            fields = {**deepcopy(interval),
                      'bucket_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 60},
                      'top_limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
            required = ['channel', 'from', 'to']
        elif name == 'recording_status':
            fields = {'capsule_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$'}}
        elif name == 'demo_guide':
            fields = {'step': {'type': 'integer', 'minimum': 1, 'maximum': 7}}
        else:
            fields = {}
        result.append({'name': name, 'description': 'Synthetic contract: ' + name,
                       'inputSchema': {'type': 'object', 'properties': fields,
                                       'additionalProperties': False, 'required': required},
                       'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                       'idempotentHint': True, 'openWorldHint': False}})
    return result


class ContractModules:
    """Frozen call signatures only; no native snapshot or aggregate implementation."""

    def __init__(self):
        owner = self
        self.instances = []
        self.providers = []
        self.read_execute = mock.Mock(side_effect=self.default_read)
        self.stats_execute = mock.Mock(side_effect=self.default_stats)
        self.guide_call = mock.Mock(side_effect=lambda arguments: guide(arguments.get('step')))
        self.resource_call = mock.Mock(side_effect=lambda uri: guide() if uri == GUIDE_URI else guide(error='unknown_resource'))
        read = ModuleType(MODULE_NAMES[0])
        stats = ModuleType(MODULE_NAMES[1])
        demo = ModuleType(MODULE_NAMES[2])

        class ReadModel:
            def __init__(self, snapshot_provider=None):
                owner.providers.append(snapshot_provider)
                owner.instances.append(self)

            def execute(self, name, arguments, *, events=None, scan=None):
                return owner.read_execute(name, arguments, events=events, scan=scan)

        def execute(name, arguments, events, *, scan):
            return owner.stats_execute(name, arguments, events, scan=scan)

        read.ReadModel = ReadModel
        read.tool_definitions = mock.Mock(return_value=definitions(RUNTIME_NAMES | CHAT_NAMES))
        stats.execute = execute
        stats.tool_definitions = mock.Mock(return_value=definitions(STATISTICS_NAMES))
        demo.tool_definitions = mock.Mock(return_value=definitions({'demo_guide'}))
        demo.demo_guide = self.guide_call
        demo.resource_definitions = mock.Mock(return_value=[{
            'uri': GUIDE_URI, 'name': 'demo_guide', 'mimeType': 'application/json',
        }])
        demo.read_resource = self.resource_call
        self.modules = dict(zip(MODULE_NAMES, (read, stats, demo)))

    def default_read(self, name, arguments, *, events=None, scan=None):
        if name in CHAT_NAMES:
            return read_result(list(events), scan=scan)
        return read_result(runtime_data(name))

    def default_stats(self, name, arguments, events, *, scan):
        list(events)
        return read_result(aggregate(), scan=scan)


class McpBridgeDispatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cafe-bridge-synthetic-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.contracts = ContractModules()
        self.provider = mock.Mock(return_value=None)

    def server(self, *, modules=None, provider=True, control=None, query=None, clock=None):
        installed = self.contracts.modules if modules is None else modules
        with mock.patch.dict(sys.modules, installed):
            server = mcp.Server(self.root, readonly_provider=self.provider if provider else None,
                                control=control, query=query, clock=clock)
        initialize(server)
        return server

    def call(self, server, name, arguments=None):
        return server.handle(request('tools/call', {'name': name,
                                                  'arguments': {} if arguments is None else arguments}))

    def typed(self, response, error=None):
        self.assertNotIn('error', response)
        result = response['result']
        payload = result['structuredContent']
        self.assertEqual(set(payload), {'data', 'scan'})
        value = payload['data']
        self.assertEqual(set(value), {'schema', 'availability', 'data', 'page', 'scan', 'error'})
        self.assertEqual(value['schema'], 'cafe.mcp_read.v1')
        self.assertEqual(set(value['page']), {'next_cursor', 'complete'})
        self.assertEqual(value['scan'], payload['scan'])
        self.assertEqual(value['error'], error)
        self.assertEqual(json.loads(result['content'][0]['text']), payload)
        self.assertEqual(result['isError'], error is not None)
        return value

    def write_events(self, rows, tail=b''):
        folder = self.root / 'twitch' / 'synthetic_channel'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / '2000-01-01.jsonl'
        target.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n'
                                   for row in rows) + tail)
        return target

    def test_constructor_adds_only_optional_readonly_provider(self):
        signature = inspect.signature(mcp.Server)
        self.assertEqual(list(signature.parameters), ['data', 'query', 'clock', 'control', 'diagnostics', 'readonly_provider', 'organizer'])
        for name in ['query', 'clock', 'control', 'diagnostics', 'readonly_provider', 'organizer']:
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIsNone(signature.parameters[name].default)

    def test_exact_listing_stays_closed_even_with_explicit_control(self):
        control = mock.Mock()
        server = self.server(control=control)
        listed = server.handle(request('tools/list'))['result']['tools']
        reads = [item for item in listed if item['name'] in ALL_NAMES]
        self.assertEqual({item['name'] for item in reads}, ALL_NAMES)
        self.assertEqual({item['name'] for item in listed} - ALL_NAMES, set(mcp._CONTROL_TOOL_NAMES))
        for item in reads:
            self.assertIs(item['inputSchema']['additionalProperties'], False)
            self.assertIs(item['annotations']['readOnlyHint'], True)
        control.execute.assert_not_called()
        control.approve.assert_not_called()

    def test_absent_modules_fail_closed_without_breaking_original_queries(self):
        server = self.server(modules=dict.fromkeys(MODULE_NAMES))
        for name in sorted(READ_NAMES | {'demo_guide'}):
            arguments = INTERVAL if name in CHAT_NAMES | STATISTICS_NAMES else {}
            with self.subTest(name=name):
                value = self.typed(self.call(server, name, arguments), 'provider_unavailable')
                self.assertEqual(value['availability'], 'unavailable')
                self.assertIsNone(value['data'])
                self.assertFalse(value['page']['complete'])
        original = self.call(server, 'list_channels_and_days')['result']
        self.assertFalse(original['isError'])
        self.assertEqual(original['structuredContent']['data'], [])

    def test_missing_group_does_not_disable_available_groups(self):
        modules = {**self.contracts.modules, MODULE_NAMES[0]: None}
        server = self.server(modules=modules)
        self.typed(self.call(server, 'list_capsules'), 'provider_unavailable')
        self.typed(self.call(server, 'chat_statistics', INTERVAL))
        self.assertEqual(self.call(server, 'demo_guide')['result']['structuredContent'], guide())

    def test_runtime_routes_only_to_read_model_and_never_reads_chat_or_controls(self):
        control = mock.Mock()
        query = mock.Mock(side_effect=AssertionError('unexpected synthetic query'))
        clock = mock.Mock(side_effect=AssertionError('unexpected synthetic clock'))
        server = self.server(control=control, query=query, clock=clock)
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            forbidden = [stack.enter_context(mock.patch.object(mcp, 'DataReader')),
                         stack.enter_context(mock.patch('builtins.open')),
                         stack.enter_context(mock.patch.object(mcp.os, 'open')),
                         stack.enter_context(mock.patch.object(socket, 'socket')),
                         stack.enter_context(mock.patch.object(subprocess, 'Popen')),
                         stack.enter_context(mock.patch.object(mcp.os, 'system'))]
            for name in sorted(RUNTIME_NAMES):
                self.assertEqual(self.typed(self.call(server, name))['data'], runtime_data(name))
            for effect in forbidden:
                effect.assert_not_called()
        self.assertEqual(self.contracts.providers, [self.provider])
        self.assertEqual(len(self.contracts.instances), 1)
        for call in self.contracts.read_execute.call_args_list:
            self.assertEqual(call.kwargs, {'events': None, 'scan': None})
        query.assert_not_called()
        clock.assert_not_called()
        control.execute.assert_not_called()
        control.approve.assert_not_called()
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))

    def test_no_provider_never_claims_an_empty_runtime_success(self):
        server = self.server(provider=False)
        for name in sorted(RUNTIME_NAMES):
            with self.subTest(name=name):
                value = self.typed(self.call(server, name), 'provider_unavailable')
                self.assertEqual(value['availability'], 'unavailable')
                self.assertIsNone(value['data'])
                self.assertFalse(value['page']['complete'])
        self.contracts.read_execute.assert_not_called()

    def test_chat_readers_use_single_validated_scan_and_preserve_unknown_clocks(self):
        rows = [synthetic_event(i) for i in range(3)]
        invalid = synthetic_event(4)
        invalid['received_at']['monotonic_ns'] = '04'
        target = self.write_events(rows + [invalid])
        original_reader = mcp.DataReader
        for name in sorted(CHAT_NAMES):
            with self.subTest(name=name):
                server = self.server()
                with mock.patch.object(mcp, 'DataReader', wraps=original_reader) as reader:
                    value = self.typed(self.call(server, name, INTERVAL), 'scan_limited')
                reader.assert_called_once_with(self.root)
                self.assertEqual(value['data'], rows)
                self.assertEqual(set(value['scan']), SCAN_FIELDS)
                self.assertEqual(value['scan']['files_scanned'], 1)
                self.assertEqual(value['scan']['bytes_scanned'], target.stat().st_size)
                self.assertEqual(value['scan']['corrupt_lines'], 1)
                self.assertFalse(value['page']['complete'])
                self.assertIsNone(value['data'][0]['sent_at'])

    def test_statistics_signature_gets_one_materialized_validated_iterable(self):
        rows = [synthetic_event(i) for i in range(3)]
        target = self.write_events(rows)
        original_reader = mcp.DataReader
        server = self.server()
        for name in sorted(STATISTICS_NAMES):
            with self.subTest(name=name):
                with mock.patch.object(mcp, 'DataReader', wraps=original_reader) as reader:
                    value = self.typed(self.call(server, name, INTERVAL))
                reader.assert_called_once_with(self.root)
                passed = self.contracts.stats_execute.call_args
                self.assertEqual(passed.args[:2], (name, INTERVAL))
                self.assertEqual(list(passed.args[2]), rows)
                self.assertEqual(passed.kwargs['scan'], value['scan'])
                self.assertEqual(value['scan']['bytes_scanned'], target.stat().st_size)
                self.assertEqual(value['data'], aggregate())

    def test_extra_parameters_and_invalid_page_arguments_fail_before_dispatch(self):
        cases = [
            ('list_capsules', {'extra': True}), ('list_capsules', {'limit': True}),
            ('list_capsules', {'limit': 0}), ('list_capsules', {'limit': 201}),
            ('list_candidate_windows', {'cursor': 3}), ('list_candidate_windows', {'cursor': []}),
            ('list_candidate_windows', {'cursor': 'x' * 513}),
            ('list_candidate_windows', {'cursor': '\u00e9' * 257}),
            ('list_candidate_windows', {'cursor': '\ud800'}),
            ('recording_status', {'capsule_id': '../synthetic'}),
            ('recording_status', {'capsule_id': 's' * 97}),
            ('obs_status', {'extra': 'synthetic'}),
            ('messages_by_user', {**INTERVAL, 'user_login': 'synthetic_user'}),
            ('messages_between', {**INTERVAL, 'limit': False}),
            ('messages_between', {**INTERVAL, 'cursor': {}}),
            ('chat_statistics', {**INTERVAL, 'top_limit': 51}),
            ('chat_statistics', {**INTERVAL, 'top_limit': True}),
            ('chat_statistics', {**INTERVAL, 'bucket_seconds': 0}),
            ('chat_statistics', {**INTERVAL, 'bucket_seconds': 3601}),
            ('chat_statistics', {**INTERVAL, 'bucket_seconds': False}),
            ('chat_activity', {**INTERVAL, 'bucket_seconds': 60}),
            ('chat_activity', {**INTERVAL, 'limit': 201}),
        ]
        server = self.server()
        with mock.patch.object(mcp, 'DataReader') as reader:
            for name, arguments in cases:
                with self.subTest(name=name, arguments=arguments):
                    value = self.typed(self.call(server, name, arguments), 'invalid_arguments')
                    self.assertIsNone(value['data'])
            reader.assert_not_called()
        self.contracts.read_execute.assert_not_called()
        self.contracts.stats_execute.assert_not_called()

    def test_utc_calendar_offset_and_reversed_interval_rejected(self):
        server = self.server()
        invalid = [
            {**INTERVAL, 'from': '2000-02-31T00:00:00Z'},
            {**INTERVAL, 'from': '2000-01-01T00:00:00+00:00'},
            {**INTERVAL, 'from': '2000-01-01T00:02:00Z'},
            {**INTERVAL, 'to': '2000-01-01T24:00:00Z'},
            {**INTERVAL, 'to': '2000-01-01T00:01:00.' + '0' * 9 + '1Z'},
        ]
        for arguments in invalid:
            self.typed(self.call(server, 'messages_between', arguments), 'invalid_arguments')
        self.contracts.read_execute.assert_not_called()

    def test_unknown_tools_and_extra_rpc_parameters_keep_original_codes(self):
        server = self.server()
        for name in ['unknown_tool', 'approve', 'approve_recording', 'start_recording']:
            self.assertEqual(self.call(server, name)['error']['code'], -32602)
        response = server.handle(request('tools/call', {'name': 'list_capsules',
                                                       'arguments': {}, 'extra': True}))
        self.assertEqual(response['error']['code'], -32602)
        self.contracts.read_execute.assert_not_called()

    def test_interval_order_uses_exact_nanoseconds_not_strings_or_microseconds(self):
        server = self.server()
        equal = {**INTERVAL, 'from': '2000-01-01T00:00:00Z',
                 'to': '2000-01-01T00:00:00.000000000Z'}
        reverse = {**INTERVAL, 'from': '2000-01-01T00:00:00.000000002Z',
                   'to': '2000-01-01T00:00:00.000000001Z'}
        forward = {**INTERVAL, 'from': '2000-01-01T00:00:00.000000001Z',
                   'to': '2000-01-01T00:00:00.000000002Z'}
        self.typed(self.call(server, 'messages_between', equal), 'invalid_arguments')
        self.typed(self.call(server, 'messages_between', reverse), 'invalid_arguments')
        self.contracts.read_execute.assert_not_called()
        self.typed(self.call(server, 'messages_between', forward))
        self.assertEqual(self.contracts.read_execute.call_args.args,
                         ('messages_between', forward))

    def test_opaque_cursor_and_epoch_errors_are_delegated_without_rebuilding_model(self):
        self.contracts.read_execute.side_effect = [
            read_result(runtime_data('list_capsules'), cursor='synthetic-cursor-a', complete=False),
            read_result(error='stale_cursor', complete=False),
            read_result(error='stale_cursor', complete=False),
        ]
        server = self.server()
        first = self.typed(self.call(server, 'list_capsules', {'limit': 1}))
        cursor = first['page']['next_cursor']
        self.typed(self.call(server, 'list_capsules', {'limit': 1, 'cursor': cursor}), 'stale_cursor')
        self.typed(self.call(server, 'list_candidate_windows', {'limit': 1, 'cursor': cursor}), 'stale_cursor')
        self.assertEqual(len(self.contracts.instances), 1)
        self.assertEqual(self.contracts.read_execute.call_args_list[1].args,
                         ('list_capsules', {'limit': 1, 'cursor': cursor}))
        self.assertEqual(self.contracts.read_execute.call_args_list[2].args,
                         ('list_candidate_windows', {'limit': 1, 'cursor': cursor}))

    def test_cross_filter_and_invalid_cursor_errors_preserved(self):
        self.contracts.read_execute.side_effect = [
            read_result(error='stale_cursor', complete=False),
            read_result(error='invalid_arguments', complete=False),
        ]
        server = self.server()
        changed = {**INTERVAL, 'to': '2000-01-01T00:02:00Z', 'cursor': 'synthetic-prior-query'}
        self.typed(self.call(server, 'messages_between', changed), 'stale_cursor')
        self.typed(self.call(server, 'list_capsules', {'cursor': 'synthetic-invalid'}), 'invalid_arguments')
        self.assertEqual(self.contracts.read_execute.call_args_list[0].args,
                         ('messages_between', changed))

    def test_malformed_result_is_closed_and_exception_text_is_absent(self):
        server = self.server()
        malformed = [None, [], {}, {**read_result({}), 'error': 'synthetic-private-error'},
                     {**read_result({}), 'availability': 'perhaps'},
                     {**read_result({}), 'page': {'next_cursor': None, 'complete': 'yes'}},
                     {**read_result({}), 'extra': 'synthetic-private-marker'}]
        for result in malformed:
            with self.subTest(kind=type(result).__name__):
                self.contracts.read_execute.side_effect = None
                self.contracts.read_execute.return_value = result
                response = self.call(server, 'obs_status')
                self.typed(response, 'invalid_snapshot')
                self.assertNotIn('synthetic-private', json.dumps(response))

    def test_paginated_output_cannot_exceed_requested_or_default_limit(self):
        server = self.server()
        self.contracts.read_execute.side_effect = None
        for arguments, count in [({}, 51), ({'limit': 2}, 3)]:
            rows = []
            for index in range(count):
                row = runtime_data('list_capsules')[0]
                row['capsule_id'] = 'synthetic-capsule-' + str(index)
                row['slot'] = 'slot-' + str(index + 1)
                rows.append(row)
            self.contracts.read_execute.return_value = read_result(rows)
            self.typed(self.call(server, 'list_capsules', arguments), 'invalid_snapshot')
        for name in sorted(CHAT_NAMES):
            self.contracts.read_execute.return_value = read_result([synthetic_event(0), synthetic_event(1)])
            self.typed(self.call(server, name, {**INTERVAL, 'limit': 1}), 'invalid_snapshot')
        activity = aggregate()
        activity['messages_per_bucket'].append({'start': '2000-01-01T00:01:00Z', 'count': 1})
        self.contracts.stats_execute.side_effect = None
        self.contracts.stats_execute.return_value = read_result(activity)
        self.typed(self.call(server, 'chat_activity', {
            **INTERVAL, 'to': '2000-01-01T00:02:00Z', 'limit': 1,
        }), 'invalid_snapshot')

    def test_page_cannot_claim_complete_while_emitting_a_next_cursor(self):
        server = self.server()
        self.contracts.read_execute.side_effect = None
        self.contracts.read_execute.return_value = read_result(
            runtime_data('list_capsules'), cursor='synthetic-next-page', complete=True)
        self.typed(self.call(server, 'list_capsules'), 'invalid_snapshot')

    def test_closed_dependency_errors_never_become_empty_success(self):
        server = self.server()
        for error in ['provider_unavailable', 'invalid_snapshot', 'stale_cursor', 'query_unavailable']:
            self.contracts.read_execute.side_effect = None
            self.contracts.read_execute.return_value = read_result(error=error, complete=False)
            value = self.typed(self.call(server, 'list_capsules'), error)
            self.assertEqual(value['availability'], 'unavailable')
            self.assertIsNone(value['data'])
            self.assertFalse(value['page']['complete'])

    def test_runtime_projection_rejects_extra_private_fields_and_non_json_values(self):
        server = self.server()
        self.contracts.read_execute.side_effect = None
        private = {**runtime_data('obs_status'), 'confirmation_token': 'synthetic-private-marker'}
        invalid = [private, {'available': float('nan'), 'configured': None, 'state': None},
                   {'available': None, 'configured': None, 'state': float('inf')}]
        for data in invalid:
            self.contracts.read_execute.return_value = read_result(data)
            response = self.call(server, 'obs_status')
            self.typed(response, 'invalid_snapshot')
            self.assertNotIn('synthetic-private-marker', json.dumps(response))
        for name in ['list_capsules', 'list_candidate_windows']:
            for generation in [1, True, '01', '18446744073709551616']:
                data = runtime_data(name)
                data[0]['generation'] = generation
                self.contracts.read_execute.return_value = read_result(data)
                self.typed(self.call(server, name), 'invalid_snapshot')

    def test_statistics_rejects_invalid_counts_metadata_and_private_extra_fields(self):
        server = self.server()
        self.contracts.stats_execute.side_effect = None
        invalid = [{**aggregate(), 'message_count': True},
                   {**aggregate(), 'message_count': -1},
                   {**aggregate(), 'unique_chatters': None},
                   {**aggregate(), 'top_emotes': []},
                   {**aggregate(), 'emotes_status': 'available'},
                   {**aggregate(), 'confirmation_token': 'synthetic-private-marker'}]
        for data in invalid:
            self.contracts.stats_execute.return_value = read_result(data)
            response = self.call(server, 'chat_statistics', INTERVAL)
            self.typed(response, 'invalid_snapshot')
            self.assertNotIn('synthetic-private-marker', json.dumps(response))
        excessive_words = aggregate()
        excessive_words['top_words'].append({'word': 'synthetic_second', 'count': 1})
        self.contracts.stats_execute.return_value = read_result(excessive_words)
        self.typed(self.call(server, 'chat_statistics', {**INTERVAL, 'top_limit': 1}), 'invalid_snapshot')

    def test_dependency_reported_incomplete_statistics_cannot_claim_completeness(self):
        target = self.write_events([synthetic_event(i) for i in range(3)])
        server = self.server()
        self.contracts.stats_execute.side_effect = None
        for availability in ['available', 'unavailable']:
            partial = read_result(aggregate(), error='scan_limited', complete=True)
            partial['availability'] = availability
            self.contracts.stats_execute.return_value = partial
            result = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
            self.assertEqual(result['availability'], availability)
            self.assertFalse(result['page']['complete'])
            self.assertFalse(result['data']['complete'])
            self.assertEqual(result['data']['message_count'], 3)
            self.assertEqual(result['scan']['files_scanned'], 1)
            self.assertEqual(result['scan']['bytes_scanned'], target.stat().st_size)
        private = {**aggregate(), 'confirmation_token': 'synthetic-private-marker'}
        partial = read_result(private, error='scan_limited', complete=True)
        partial['availability'] = 'unavailable'
        self.contracts.stats_execute.return_value = partial
        response = self.call(server, 'chat_statistics', INTERVAL)
        self.typed(response, 'invalid_snapshot')
        self.assertNotIn('synthetic-private-marker', json.dumps(response))

    def test_recognized_chat_event_cannot_bypass_canonical_clock_validation(self):
        server = self.server()
        malformed = synthetic_event()
        malformed['received_at']['monotonic_ns'] = '01'
        self.contracts.read_execute.side_effect = None
        self.contracts.read_execute.return_value = read_result([malformed])
        self.typed(self.call(server, 'messages_between', INTERVAL), 'invalid_snapshot')

    def test_recording_clock_strings_remain_canonical_and_null_is_unknown(self):
        server = self.server()
        self.contracts.read_execute.side_effect = None
        for clock in ['0', '18446744073709551615', None]:
            data = runtime_data('recording_status')
            data['capsules'][0]['feeds'][0]['written_frames'] = clock
            self.contracts.read_execute.return_value = read_result(data)
            result = self.typed(self.call(server, 'recording_status'))
            self.assertEqual(result['data']['capsules'][0]['feeds'][0]['written_frames'], clock)
        for clock in ['00', '01', '-1', '1e2', '18446744073709551616', 0, True, 1.0]:
            data = runtime_data('recording_status')
            data['capsules'][0]['feeds'][0]['written_frames'] = clock
            self.contracts.read_execute.return_value = read_result(data)
            self.typed(self.call(server, 'recording_status'), 'invalid_snapshot')

    def test_corrupt_and_torn_scan_keeps_statistics_but_never_exact_completeness(self):
        target = self.write_events([synthetic_event(i) for i in range(3)], b'not json\n{"torn":')
        original = target.read_bytes()
        server = self.server()
        value = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
        self.assertEqual(value['data']['message_count'], 3)
        self.assertFalse(value['data']['complete'])
        self.assertFalse(value['page']['complete'])
        self.assertEqual(value['scan']['corrupt_lines'], 1)
        self.assertEqual(value['scan']['truncated_lines'], 1)
        self.assertEqual(value['scan']['bytes_scanned'], len(original))
        self.assertEqual(target.read_bytes(), original)

    def test_dependency_cannot_mutate_authoritative_scan_or_hide_corruption(self):
        target = self.write_events([synthetic_event()], b'not json\n')
        size = target.stat().st_size

        def altered(name, arguments, events, *, scan):
            list(events)
            scan.update({'bytes_scanned': 0, 'corrupt_lines': 0, 'scan_limited': False,
                         'confirmation_token': 'synthetic-private-marker'})
            return read_result(aggregate(), scan=scan)

        self.contracts.stats_execute.side_effect = altered
        server = self.server()
        response = self.call(server, 'chat_statistics', INTERVAL)
        value = self.typed(response, 'scan_limited')
        self.assertEqual(set(value['scan']), SCAN_FIELDS)
        self.assertEqual(value['scan']['bytes_scanned'], size)
        self.assertEqual(value['scan']['corrupt_lines'], 1)
        self.assertFalse(value['data']['complete'])
        self.assertNotIn('synthetic-private-marker', json.dumps(response))

    def test_scan_byte_and_file_caps_cannot_be_reported_complete(self):
        self.write_events([synthetic_event()])
        server = self.server()
        for constant in ['MAX_SCAN_BYTES', 'MAX_FILES']:
            with self.subTest(constant=constant), mock.patch.object(mcp, constant, 0):
                value = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
                self.assertTrue(value['scan']['scan_limited'])
                self.assertFalse(value['data']['complete'])
                self.assertFalse(value['page']['complete'])

    def test_scan_closes_on_failure_and_does_not_expose_private_exception(self):
        self.write_events([synthetic_event()])
        original_reader = mcp.DataReader
        closed = []

        class ObservedReader(original_reader):
            def events(self, channel=None):
                try:
                    yield from super().events(channel)
                finally:
                    closed.append(True)

        self.contracts.stats_execute.side_effect = ValueError('synthetic-private-exception')
        server = self.server()
        with mock.patch.object(mcp, 'DataReader', ObservedReader):
            response = self.call(server, 'chat_statistics', INTERVAL)
        self.typed(response, 'query_unavailable')
        self.assertEqual(closed, [True])
        self.assertNotIn('synthetic-private-exception', json.dumps(response))

    def test_oversized_read_result_preserves_cap_and_scan_with_typed_error(self):
        self.write_events([synthetic_event(i, text='x' * 16000) for i in range(4)])
        server = self.server()
        response = self.call(server, 'messages_between', INTERVAL)
        value = self.typed(response, 'response_too_large')
        self.assertIsNone(value['data'])
        self.assertFalse(value['page']['complete'])
        self.assertEqual(value['scan']['files_scanned'], 1)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1, 65536)

    def test_duplicate_keys_and_non_json_numbers_are_parse_errors_then_recover(self):
        server = self.server()
        raws = [b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":1,"limit":2}}}\n',
                b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":NaN}}}\n',
                b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":Infinity}}}\n']
        for raw in raws:
            output = io.BytesIO()
            mcp.serve(server, io.BytesIO(raw + json.dumps(request('ping', identity=2)).encode() + b'\n'), output)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[0]['error'], {'code': -32700, 'message': 'Parse error'})
            self.assertEqual(rows[1]['result'], {})
        self.contracts.read_execute.assert_not_called()

    def test_guide_tool_and_resource_roundtrip_have_same_flat_shape(self):
        server = self.server()
        listed = server.handle(request('resources/list'))['result']['resources']
        self.assertEqual([item['uri'] for item in listed], [GUIDE_URI])
        tool = self.call(server, 'demo_guide')['result']
        resource = server.handle(request('resources/read', {'uri': GUIDE_URI}))['result']
        self.assertEqual(set(tool['structuredContent']), {'schema', 'steps', 'control_mode', 'error'})
        self.assertEqual(tool['structuredContent'], guide())
        self.assertEqual(json.loads(tool['content'][0]['text']), guide())
        self.assertEqual(len(resource['contents']), 1)
        self.assertEqual(resource['contents'][0]['uri'], GUIDE_URI)
        self.assertEqual(resource['contents'][0]['mimeType'], 'application/json')
        self.assertEqual(json.loads(resource['contents'][0]['text']), tool['structuredContent'])
        self.assertTrue(all(step['live_status'] == 'pending' for step in tool['structuredContent']['steps']))
        self.assertEqual(self.call(server, 'demo_guide', {'step': 7})['result']['structuredContent'], guide(7))

    def test_guide_step_errors_and_unknown_resource_keep_flat_closed_shape(self):
        server = self.server()
        for arguments in [{'step': 0}, {'step': 8}, {'step': True}, {'step': '1'}, {'extra': 1}]:
            result = self.call(server, 'demo_guide', arguments)['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent'], guide(error='invalid_arguments'))
        self.contracts.guide_call.assert_not_called()
        resource = server.handle(request('resources/read', {'uri': 'cafe://demo/unknown'}))['result']
        self.assertEqual(json.loads(resource['contents'][0]['text']), guide(error='unknown_resource'))
        for method, arguments in [('resources/list', {'extra': True}),
                                  ('resources/read', {}),
                                  ('resources/read', {'uri': GUIDE_URI, 'extra': True})]:
            self.assertEqual(server.handle(request(method, arguments))['error']['code'], -32602)

    def test_guide_paths_do_not_read_chat_open_network_or_invoke_effects(self):
        control = mock.Mock()
        server = self.server(control=control)
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            forbidden = [stack.enter_context(mock.patch.object(mcp, 'DataReader')),
                         stack.enter_context(mock.patch('builtins.open')),
                         stack.enter_context(mock.patch.object(mcp.os, 'open')),
                         stack.enter_context(mock.patch.object(socket, 'socket')),
                         stack.enter_context(mock.patch.object(subprocess, 'Popen')),
                         stack.enter_context(mock.patch.object(mcp.os, 'system'))]
            self.call(server, 'demo_guide')
            server.handle(request('resources/read', {'uri': GUIDE_URI}))
            for effect in forbidden:
                effect.assert_not_called()
        control.execute.assert_not_called()
        control.approve.assert_not_called()
        self.provider.assert_not_called()
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))

    def test_malformed_guide_cannot_close_live_markers_or_emit_private_extra_fields(self):
        server = self.server()
        changed = guide()
        changed['steps'][0]['live_status'] = 'passed'
        private = guide()
        private['synthetic_private_field'] = 'synthetic-private-marker'
        for result in [changed, private]:
            self.contracts.guide_call.side_effect = None
            self.contracts.guide_call.return_value = result
            response = self.call(server, 'demo_guide')
            self.typed(response, 'invalid_snapshot')
            self.assertNotIn('synthetic-private-marker', json.dumps(response))

    def test_oversized_guide_keeps_original_transport_cap(self):
        server = self.server()
        oversized = guide()
        oversized['steps'][0]['instruction'] = 'x' * 65536
        self.contracts.guide_call.side_effect = None
        self.contracts.guide_call.return_value = oversized
        response = self.call(server, 'demo_guide')
        self.typed(response, 'response_too_large')
        self.assertLessEqual(len(json.dumps(response).encode('utf-8')) + 1, 65536)

    def test_lone_surrogate_guide_content_fails_closed_for_tool_and_resource(self):
        server = self.server()
        malformed = guide()
        malformed['steps'][0]['instruction'] = '\ud800'
        self.contracts.guide_call.side_effect = None
        self.contracts.guide_call.return_value = malformed
        self.contracts.resource_call.side_effect = None
        self.contracts.resource_call.return_value = malformed
        self.typed(self.call(server, 'demo_guide'), 'invalid_snapshot')
        response = server.handle(request('resources/read', {'uri': GUIDE_URI}))
        result = json.loads(response['result']['contents'][0]['text'])
        self.assertEqual(result['schema'], 'cafe.mcp_read.v1')
        self.assertEqual(result['error'], 'invalid_snapshot')
        self.assertEqual(result['availability'], 'unavailable')
        self.assertIsNone(result['data'])


if __name__ == '__main__':
    unittest.main()
