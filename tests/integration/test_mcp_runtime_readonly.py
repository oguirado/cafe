"""Synthetic library contract checks; no live acceptance or MCP registration.

Run with the standard library runner from the repository root:
    python -B -m unittest discover -s tests/integration -p test_mcp_runtime_readonly.py

The injected provider below describes the proposed native projection contract.
It does not acquire native state, prove product integration, or close live markers.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import inspect
import io
import itertools
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_control as control
from cafe import mcp_queries as queries
from cafe import mcp_runtime_readonly as runtime
from cafe import mcp_server as server


TOOLS = {'list_capsules', 'list_candidate_windows', 'recording_status',
         'obs_status', 'messages_by_user', 'messages_between'}
ERRORS = {'invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
          'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large'}
FROM = '2025-11-20T18:00:00Z'
TO = '2025-11-20T19:00:00Z'
CHAT = {'channel': 'demo_channel', 'from': FROM, 'to': TO}


def scan(**changes):
    result = {'corrupt_lines': 0, 'truncated_lines': 0, 'bytes_scanned': 1234,
              'files_scanned': 1, 'scan_limited': False}
    result.update(changes)
    return result


def event(index=1, **changes):
    result = {
        'schema': 'cafe.chat_event.v1', 'channel': 'demo_channel',
        'received_at': {'monotonic_ns': str(index),
                        'utc': '2025-11-20T18:00:%02dZ' % (index % 60)},
        'sent_at': None, 'message_id': 'message_synth_%02d' % index,
        'user_login': 'synthetic_guest_01', 'display_name': 'Synthetic guest 01',
        'badges': [], 'text': 'Synthetic message %02d' % index,
        'kind': 'message', 'session_id': 'session_synth_chat_01',
    }
    result.update(changes)
    return result


def capsule(index=1, generation='7'):
    identity = 'capsule_synth_%02d' % index
    game = 'feed_synth_game_%02d' % index
    camera = 'feed_synth_camera_%02d' % index
    return {'capsule_id': identity, 'person_label': 'synthetic_guest_%02d' % index,
            'generation': generation,
            'feeds': {game: {'feed_id': game, 'role': 'game', 'generation': '8'},
                      camera: {'feed_id': camera, 'role': 'camera', 'generation': '9'}}}


def window(index=1):
    return {'handle': 'window_synth_%02d' % index, 'generation': '3',
            'layout_revision': '4', 'process_id': 100 + index,
            'executable': 'synthetic_app.exe', 'title': 'Synthetic shared title',
            'class': 'SyntheticWindowClass',
            'rect': {'x': -10, 'y': 20, 'width': 1920, 'height': 1080},
            'visible': True, 'minimized': False}


class SyntheticProvider:
    """Owned in-memory fixture, never a product/native snapshot acquisition."""

    def __init__(self):
        self.session_id = 'session_synth_native_01'
        self.epoch = '1'
        self.snapshot_id = 'snapshot_synth_01'
        self.capsules = [capsule()]
        self.windows = [window()]
        self.recordings = [{
            'capsule_id': 'capsule_synth_01', 'state': 'recording',
            'feeds': [{'feed_id': 'feed_synth_game_01', 'state': 'recording',
                       'written_frames': '18446744073709551615', 'backpressure': None},
                      {'feed_id': 'feed_synth_camera_01', 'state': 'stopped',
                       'written_frames': None, 'backpressure': '0'}],
        }]
        self.recording_available = True
        self.obs_available = True
        self.obs_state = 'configured'
        self.partial = False
        self.transform = None
        self.calls = []

    def __call__(self, kind, arguments):
        self.calls.append((kind, deepcopy(arguments)))
        page = {'offset': 0, 'next_offset': None, 'complete': not self.partial}
        if kind in {'list_capsules', 'list_candidate_windows'}:
            source = self.capsules if kind == 'list_capsules' else self.windows
            offset = arguments['offset']
            rows = deepcopy(source[offset:offset + arguments['limit']])
            more = offset + len(rows) < len(source)
            page = {'offset': offset, 'next_offset': offset + len(rows) if more else None,
                    'complete': not more and not self.partial}
            data = ({'capsules': {row['capsule_id']: row for row in rows}}
                    if kind == 'list_capsules' else {'windows': rows})
        elif kind == 'recording_status':
            data = {'recordings': deepcopy(self.recordings),
                    'recording_available': self.recording_available}
        elif kind == 'obs_status':
            data = {'obs_available': self.obs_available, 'services': {'obs': self.obs_state}}
        elif kind in {'messages_between', 'messages_by_user'}:
            data = {}
        else:
            raise AssertionError('unexpected synthetic provider query')
        result = {'session_id': self.session_id, 'authority_epoch': self.epoch,
                  'snapshot_id': self.snapshot_id, 'data': data, 'page': page}
        if self.transform is not None:
            self.transform(result)
        return result


class ReadModelTests(unittest.TestCase):
    def setUp(self):
        self.provider = SyntheticProvider()
        self.model = runtime.ReadModel(self.provider)

    def assert_result(self, result, *, error=None, available=True):
        self.assertIs(type(result), dict)
        self.assertEqual(set(result), {'schema', 'availability', 'data', 'page', 'scan', 'error'})
        self.assertEqual(result['schema'], 'cafe.mcp_read.v1')
        self.assertEqual(result['availability'], 'available' if available else 'unavailable')
        self.assertIn(type(result['data']), (dict, list, type(None)))
        self.assertIs(type(result['page']), dict)
        self.assertEqual(set(result['page']), {'next_cursor', 'complete'})
        self.assertIs(type(result['page']['complete']), bool)
        cursor = result['page']['next_cursor']
        self.assertTrue(cursor is None or type(cursor) is str and len(cursor.encode('utf-8')) <= 512)
        self.assertTrue(result['error'] is None or result['error'] in ERRORS)
        self.assertEqual(result['error'], error)
        if result['scan'] is not None:
            self.assertIs(type(result['scan']), dict)
            self.assertEqual(set(result['scan']), set(scan()))
            self.assertIs(type(result['scan']['scan_limited']), bool)
            for key in set(scan()) - {'scan_limited'}:
                self.assertIs(type(result['scan'][key]), int)
                self.assertGreaterEqual(result['scan'][key], 0)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode('utf-8')) + 1,
                             server.MAX_RESPONSE)
        if not available:
            self.assertIsNone(result['data'])
            self.assertFalse(result['page']['complete'])
            self.assertIsNone(result['page']['next_cursor'])
        return result

    def assert_error(self, result, code):
        return self.assert_result(result, error=code, available=False)

    def chat(self, events, *, name='messages_between', arguments=None, stats=None):
        return self.model.execute(name, dict(CHAT) if arguments is None else arguments,
                                  events=iter(events), scan=scan() if stats is None else stats)

    def test_frozen_signatures_have_keyword_only_events_and_scan(self):
        self.assertEqual(str(inspect.signature(runtime.ReadModel)), '(snapshot_provider=None)')
        params = inspect.signature(runtime.ReadModel.execute).parameters
        self.assertEqual(list(params), ['self', 'name', 'arguments', 'events', 'scan'])
        self.assertEqual(params['events'].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(params['scan'].kind, inspect.Parameter.KEYWORD_ONLY)
        with self.assertRaises(TypeError):
            self.model.execute('messages_between', CHAT, [], scan())

    def test_tool_definitions_are_closed_typed_readonly_and_disjoint(self):
        definitions = runtime.tool_definitions()
        self.assertIs(type(definitions), list)
        self.assertEqual(len(definitions), 6)
        self.assertEqual({item['name'] for item in definitions}, TOOLS)
        self.assertTrue(TOOLS.isdisjoint({item['name'] for item in server.tools()}))
        for item in definitions:
            with self.subTest(tool=item['name']):
                self.assertIs(type(item), dict)
                self.assertIs(type(item['description']), str)
                schema = item['inputSchema']
                self.assertEqual(schema['type'], 'object')
                self.assertIs(schema['additionalProperties'], False)
                self.assertIs(type(schema['properties']), dict)
                self.assertIs(type(schema.get('required', [])), list)
                self.assertTrue(set(schema.get('required', [])) <= set(schema['properties']))
                self.assertIs(item['annotations']['readOnlyHint'], True)
                self.assertIs(item['annotations']['destructiveHint'], False)
                if 'limit' in schema['properties']:
                    self.assertEqual(schema['properties']['limit']['type'], 'integer')
                    self.assertEqual(schema['properties']['limit']['default'], 50)
                    self.assertEqual(schema['properties']['limit']['maximum'], 200)
        definitions[0]['inputSchema']['properties']['synthetic_poison'] = {}
        self.assertNotIn('synthetic_poison', runtime.tool_definitions()[0]['inputSchema']['properties'])

    def test_list_capsules_happy_path_preserves_ids_clocks_and_display_label(self):
        result = self.assert_result(self.model.execute('list_capsules', {}))
        self.assertTrue(result['page']['complete'])
        self.assertIsNone(result['scan'])
        self.assertEqual(len(result['data']), 1)
        row = result['data'][0]
        self.assertEqual(set(row), {'capsule_id', 'slot', 'person_label', 'generation', 'feeds'})
        self.assertEqual({key: row[key] for key in ('capsule_id', 'slot', 'person_label', 'generation')},
                         {'capsule_id': 'capsule_synth_01', 'slot': 'slot-1',
                          'person_label': 'synthetic_guest_01', 'generation': '7'})
        self.assertEqual({feed['role']: feed for feed in row['feeds']},
                         {feed['role']: feed for feed in capsule()['feeds'].values()})
        self.assertEqual(self.provider.calls, [('list_capsules',
                         {'limit': 50, 'offset': 0, 'snapshot_id': None})])

    def test_list_candidate_windows_projects_native_fields_only(self):
        self.provider.windows = [window(1), window(2)]
        result = self.assert_result(self.model.execute('list_candidate_windows', {}))
        self.assertEqual([row['window_token'] for row in result['data']],
                         ['window_synth_01', 'window_synth_02'])
        for row in result['data']:
            self.assertEqual(set(row), {'window_token', 'generation', 'layout_revision',
                                       'title', 'executable', 'rect', 'visible', 'minimized'})
            self.assertEqual(row['rect'], {'width': 1920, 'height': 1080})
            self.assertEqual(row['generation'], '3')
            self.assertEqual(row['layout_revision'], '4')
            self.assertIs(row['visible'], True)
            self.assertIs(row['minimized'], False)
        self.assertEqual(result['data'][0]['title'], result['data'][1]['title'])

    def test_recording_status_happy_path_and_optional_capsule_filter(self):
        other = deepcopy(self.provider.recordings[0])
        other['capsule_id'] = 'capsule_synth_02'
        other['feeds'] = []
        self.provider.recordings.append(other)
        result = self.assert_result(self.model.execute('recording_status',
                                                       {'capsule_id': 'capsule_synth_01'}))
        self.assertEqual(result['data'], {'capsules': [self.provider.recordings[0]],
                                         'recorder_available': True})
        self.assertEqual(self.provider.calls[-1], ('recording_status',
                         {'capsule_id': 'capsule_synth_01', 'snapshot_id': None}))
        all_rows = self.assert_result(self.model.execute('recording_status', {}))
        self.assertEqual(len(all_rows['data']['capsules']), 2)

    def test_recording_unknown_values_remain_null_and_u64_remains_string(self):
        self.provider.recording_available = None
        result = self.assert_result(self.model.execute('recording_status', {}))
        self.assertIsNone(result['data']['recorder_available'])
        feeds = result['data']['capsules'][0]['feeds']
        self.assertEqual(feeds[0]['written_frames'], '18446744073709551615')
        self.assertIsNone(feeds[0]['backpressure'])
        self.assertIsNone(feeds[1]['written_frames'])
        self.assertEqual(feeds[1]['backpressure'], '0')
        self.assertEqual(set(feeds[0]), {'feed_id', 'state', 'written_frames', 'backpressure'})

    def test_slot_display_alias_is_never_resolved_for_status(self):
        row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
        result = self.assert_result(self.model.execute('recording_status', {'capsule_id': row['slot']}))
        self.assertEqual(result['data']['capsules'], [])
        self.assertEqual(self.provider.calls[-1][1]['capsule_id'], 'slot-1')

    def test_obs_status_happy_path_and_unknown_native_states(self):
        for available, state, configured in [(True, 'configured', True),
                                              (False, 'absent', False),
                                              (True, 'synthetic_unknown', None),
                                              (None, None, None)]:
            with self.subTest(state=state):
                self.provider.obs_available = available
                self.provider.obs_state = state
                result = self.assert_result(self.model.execute('obs_status', {}))
                self.assertEqual(result['data'], {'available': available,
                                                 'configured': configured, 'state': state})

    def test_messages_between_happy_path_matches_existing_query_order(self):
        rows = [event(3), event(1), event(2)]
        expected = queries.execute('recent_messages', {'channel': 'demo_channel'}, iter(rows), 0)
        result = self.assert_result(self.chat(rows))
        self.assertEqual(result['data'], expected)
        self.assertTrue(result['page']['complete'])
        self.assertEqual(result['scan'], scan())
        for row in result['data']:
            self.assertTrue(server._valid_event(row))
        self.assertEqual(self.provider.calls[-1], ('messages_between',
                         dict(CHAT, limit=50, snapshot_id=None)))

    def test_messages_by_user_filters_channel_kind_and_exact_login(self):
        rows = [event(1), event(2, user_login='synthetic_guest_02'),
                event(3, channel='synthetic_other'), event(4, kind='notice'),
                event(5, user_login=None), event(6)]
        args = dict(CHAT, user_login='synthetic_guest_01')
        result = self.assert_result(self.chat(rows, name='messages_by_user', arguments=args))
        self.assertEqual([row['message_id'] for row in result['data']],
                         ['message_synth_01', 'message_synth_06'])

    def test_without_provider_all_six_tools_are_unavailable_even_with_events(self):
        model = runtime.ReadModel()
        for name in TOOLS:
            args = dict(CHAT) if name.startswith('messages_') else {}
            if name == 'messages_by_user':
                args['user_login'] = 'synthetic_guest_01'
            with self.subTest(tool=name):
                self.assert_error(model.execute(name, args, events=iter([event()]), scan=scan()),
                                  'provider_unavailable')

    def test_none_and_exception_providers_are_unavailable_without_text_leaks(self):
        marker = 'SYNTHETIC_PROVIDER_EXCEPTION_DO_NOT_RETURN'
        for provider in [lambda name, args: None,
                         mock.Mock(side_effect=RuntimeError(marker))]:
            with self.subTest(provider_type=type(provider).__name__):
                result = runtime.ReadModel(provider).execute('list_capsules', {})
                self.assert_error(result, 'provider_unavailable')
                self.assertNotIn(marker, json.dumps(result))

    def test_noncallable_provider_is_invalid_snapshot(self):
        for provider in [0, False, {}, [], 'synthetic_provider']:
            with self.subTest(provider_type=type(provider).__name__):
                self.assert_error(runtime.ReadModel(provider).execute('list_capsules', {}),
                                  'invalid_snapshot')

    def test_malformed_provider_envelope_fails_closed(self):
        mutations = [lambda value: value.update(extra=True),
                     lambda value: value.pop('session_id'),
                     lambda value: value.update(session_id=''),
                     lambda value: value.update(snapshot_id=''),
                     lambda value: value.update(authority_epoch=True),
                     lambda value: value.update(authority_epoch='01'),
                     lambda value: value.update(data=[]),
                     lambda value: value.update(page=None)]
        for mutate in mutations:
            with self.subTest(case=mutations.index(mutate)):
                self.provider.transform = mutate
                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
                                  'invalid_snapshot')
        for malformed in [True, [], 'synthetic_snapshot', 1]:
            self.assert_error(runtime.ReadModel(lambda name, args, v=malformed: v)
                              .execute('list_capsules', {}), 'invalid_snapshot')

    def test_explicitly_unavailable_native_query_never_returns_empty_success(self):
        self.provider.transform = lambda value: value.update(data=None)
        self.assert_error(self.model.execute('list_capsules', {}), 'query_unavailable')

    def test_epoch_change_invalidates_call_and_resets_slot_mapping(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        first = self.assert_result(self.model.execute('list_capsules', {}))
        initial = {row['capsule_id']: row['slot'] for row in first['data']}
        self.assertEqual(initial['capsule_synth_02'], 'slot-2')
        self.provider.epoch = '2'
        self.provider.capsules = [capsule(2)]
        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')
        fresh = self.assert_result(self.model.execute('list_capsules', {}))
        self.assertEqual(fresh['data'][0]['slot'], 'slot-1')

    def test_native_session_change_also_fails_once_and_resets_aliases(self):
        self.assert_result(self.model.execute('list_capsules', {}))
        self.provider.session_id = 'session_synth_native_02'
        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')
        self.assertEqual(self.assert_result(self.model.execute('list_capsules', {}))
                         ['data'][0]['slot'], 'slot-1')

    def test_slots_follow_opaque_identity_and_generation_not_array_position(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        first = self.assert_result(self.model.execute('list_capsules', {}))
        slots = {row['capsule_id']: row['slot'] for row in first['data']}
        self.provider.capsules.reverse()
        again = self.assert_result(self.model.execute('list_capsules', {}))
        self.assertEqual({row['capsule_id']: row['slot'] for row in again['data']}, slots)
        self.provider.capsules = [capsule(1, generation='8')]
        self.provider.snapshot_id = 'snapshot_synth_02'
        changed = self.assert_result(self.model.execute('list_capsules', {}))
        self.assertNotEqual(changed['data'][0]['slot'], slots['capsule_synth_01'])

    def test_limit_rejects_bool_float_string_and_out_of_range_before_provider(self):
        for limit in [True, False, 1.0, '1', 0, -1, 201, None]:
            with self.subTest(limit=limit):
                self.assert_error(self.model.execute('list_capsules', {'limit': limit}),
                                  'invalid_arguments')
        self.assertEqual(self.provider.calls, [])

    def test_limit_one_and_two_hundred_are_valid(self):
        for limit in [1, 200]:
            with self.subTest(limit=limit):
                self.assert_result(self.model.execute('list_capsules', {'limit': limit}))

    def test_unknown_and_existing_chat_tool_names_are_not_new_tools(self):
        for name in ['synthetic_unknown', *[item['name'] for item in server.tools()]]:
            with self.subTest(tool=name):
                self.assert_error(self.model.execute(name, {}), 'query_unavailable')
        self.assertEqual(self.provider.calls, [])

    def test_extra_arguments_and_control_flags_fail_before_provider(self):
        for name, args in [('obs_status', {'limit': 1}),
                           ('recording_status', {'cursor': None}),
                           ('list_capsules', {'confirmed': True}),
                           ('list_capsules', {'folder': 'synthetic_directory'}),
                           ('messages_between', dict(CHAT, query='synthetic'))]:
            with self.subTest(tool=name, fields=sorted(args)):
                self.assert_error(self.model.execute(name, args), 'invalid_arguments')
        self.assertEqual(self.provider.calls, [])

    def test_wrong_argument_objects_and_missing_required_filters_fail(self):
        for args in [None, [], True, 'synthetic', 1]:
            self.assert_error(self.model.execute('obs_status', args), 'invalid_arguments')
        for key in CHAT:
            args = dict(CHAT)
            del args[key]
            self.assert_error(self.model.execute('messages_between', args), 'invalid_arguments')
        self.assert_error(self.model.execute('messages_by_user', dict(CHAT)), 'invalid_arguments')
        self.assertEqual(self.provider.calls, [])

    def test_invalid_filter_identifiers_and_intervals_fail_closed(self):
        cases = [dict(CHAT, channel='DEMO_CHANNEL'), dict(CHAT, channel='ab'),
                 dict(CHAT, channel='a' * 26), dict(CHAT, to=FROM),
                 dict(CHAT, to='2025-11-20T17:00:00Z'),
                 dict(CHAT, **{'from': '2025-02-30T18:00:00Z'}),
                 dict(CHAT, **{'from': '2025-11-20T18:00:00+00:00'}),
                 dict(CHAT, **{'from': FROM[:-1] + '.' + '1' * 10 + 'Z'}),
                 dict(CHAT, **{'from': '2025-11-20T18:00:00Z\n'})]
        for args in cases:
            self.assert_error(self.model.execute('messages_between', args), 'invalid_arguments')
        for login in [None, '', 'SyntheticGuest', 'a' * 26, True]:
            self.assert_error(self.model.execute('messages_by_user', dict(CHAT, user_login=login)),
                              'invalid_arguments')
        for identity in ['', '_synthetic', 'a' * 97, True, None]:
            self.assert_error(self.model.execute('recording_status', {'capsule_id': identity}),
                              'invalid_arguments')
        self.assertEqual(self.provider.calls, [])

    def test_invalid_and_oversized_cursor_fails_before_provider(self):
        for cursor in [True, 1, {}, [], '', 'cursor_synthetic_unknown', 'x' * 513, '\u00e9' * 257]:
            with self.subTest(cursor_type=type(cursor).__name__):
                self.assert_error(self.model.execute('list_capsules', {'cursor': cursor}),
                                  'invalid_arguments')
        self.assertEqual(self.provider.calls, [])
        self.assert_result(self.model.execute('list_capsules', {'cursor': None}))

    def test_native_pagination_uses_requested_page_and_stable_snapshot(self):
        self.provider.windows = [window(index) for index in range(1, 6)]
        collected = []
        cursor = None
        for offset in [0, 2, 4]:
            result = self.assert_result(self.model.execute('list_candidate_windows',
                                                           {'limit': 2, 'cursor': cursor}))
            collected.extend(row['window_token'] for row in result['data'])
            self.assertEqual(self.provider.calls[-1][1],
                             {'limit': 2, 'offset': offset,
                              'snapshot_id': None if offset == 0 else 'snapshot_synth_01'})
            cursor = result['page']['next_cursor']
            self.assertEqual(result['page']['complete'], offset == 4)
            self.assertEqual(cursor is None, offset == 4)
        self.assertEqual(collected, ['window_synth_%02d' % index for index in range(1, 6)])

    def test_capsule_pagination_keeps_slot_numbers_across_pages(self):
        self.provider.capsules = [capsule(index) for index in range(1, 4)]
        first = self.assert_result(self.model.execute('list_capsules', {'limit': 2}))
        second = self.assert_result(self.model.execute('list_capsules',
            {'limit': 2, 'cursor': first['page']['next_cursor']}))
        self.assertEqual([row['slot'] for row in first['data'] + second['data']],
                         ['slot-1', 'slot-2', 'slot-3'])

    def test_one_partial_native_page_never_implies_exhaustive_window_inventory(self):
        self.provider.partial = True
        result = self.assert_result(self.model.execute('list_candidate_windows', {}))
        self.assertEqual(len(result['data']), 1)
        self.assertFalse(result['page']['complete'])
        self.assertIsNone(result['page']['next_cursor'])

    def test_malformed_native_page_metadata_and_extra_rows_fail_closed(self):
        mutations = [lambda value: value['page'].update(offset=True),
                     lambda value: value['page'].update(offset=1),
                     lambda value: value['page'].update(next_offset=0, complete=False),
                     lambda value: value['page'].update(next_offset=1),
                     lambda value: value['page'].update(complete=1),
                     lambda value: value['page'].update(extra=True),
                     lambda value: value['data']['windows'].append(window(2))]
        for mutate in mutations:
            with self.subTest(case=mutations.index(mutate)):
                self.provider.transform = mutate
                self.assert_error(runtime.ReadModel(self.provider)
                                  .execute('list_candidate_windows', {'limit': 1}),
                                  'invalid_snapshot')

    def test_cross_query_cursor_and_changed_limit_are_rejected(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
        before = len(self.provider.calls)
        self.assert_error(self.model.execute('list_candidate_windows', {'limit': 1, 'cursor': cursor}),
                          'invalid_arguments')
        self.assert_error(self.model.execute('list_capsules', {'limit': 2, 'cursor': cursor}),
                          'invalid_arguments')
        self.assertEqual(len(self.provider.calls), before)

    def test_cursor_cannot_be_reinterpreted_by_another_model(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
        other = runtime.ReadModel(self.provider)
        self.assert_error(other.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
                          'invalid_arguments')

    def test_changed_snapshot_rejects_native_continuation(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
        self.provider.snapshot_id = 'snapshot_synth_02'
        self.assert_error(self.model.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
                          'stale_cursor')

    def test_observed_native_snapshot_conflict_invalidates_existing_cursors(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
        cursor = first['page']['next_cursor']
        # The adapter has violated immutable snapshot identity. Even a fresh
        # read must notice contradictory evidence already observed locally.
        self.provider.capsules[0]['person_label'] = 'synthetic_changed_label'
        self.assert_error(self.model.execute('list_capsules', {'limit': 1}),
                          'invalid_snapshot')
        self.assert_error(self.model.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
                          'invalid_arguments')

    def test_changed_replayed_native_page_is_stale_and_invalidates_its_cursor(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
        args = {'limit': 1, 'cursor': first['page']['next_cursor']}
        self.assert_result(self.model.execute('list_capsules', args))
        self.provider.capsules[1]['person_label'] = 'synthetic_changed_second_page'
        self.assert_error(self.model.execute('list_capsules', args), 'stale_cursor')
        self.assert_error(self.model.execute('list_capsules', args), 'invalid_arguments')

    def test_native_identity_bookkeeping_limit_preserves_existing_alias(self):
        self.provider.capsules = [capsule(1), capsule(2)]
        with mock.patch.object(runtime, '_IDENTITY_CAP', 3):
            first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
            self.assertEqual(first['data'][0]['slot'], 'slot-1')
            self.assert_error(self.model.execute('list_capsules',
                              {'limit': 1, 'cursor': first['page']['next_cursor']}),
                              'query_unavailable')
            again = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
            self.assertEqual(again['data'][0]['slot'], 'slot-1')

    def test_slot_capacity_never_recycles_existing_generation_aliases(self):
        with mock.patch.object(runtime, '_IDENTITY_CAP', 3):
            for index, generation in enumerate(['7', '8', '9'], 1):
                self.provider.snapshot_id = 'snapshot_synth_capacity_%d' % index
                self.provider.capsules = [capsule(1, generation)]
                row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
                self.assertEqual(row['slot'], 'slot-%d' % index)
            self.provider.snapshot_id = 'snapshot_synth_capacity_4'
            self.provider.capsules = [capsule(1, '10')]
            self.assert_error(self.model.execute('list_capsules', {}), 'query_unavailable')
            self.provider.snapshot_id = 'snapshot_synth_capacity_5'
            self.provider.capsules = [capsule(1, '7')]
            row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
            self.assertEqual(row['slot'], 'slot-1')

    def test_chat_pagination_selects_newest_then_moves_to_older_rows(self):
        rows = [event(index) for index in [2, 5, 1, 4, 3]]
        args = dict(CHAT, limit=2)
        first = self.assert_result(self.chat(rows, arguments=args))
        self.assertEqual(first['data'], queries.execute('recent_messages',
                         {'channel': 'demo_channel', 'limit': 2}, iter(rows), 0))
        self.assertFalse(first['page']['complete'])
        args['cursor'] = first['page']['next_cursor']
        second = self.assert_result(self.chat(rows, arguments=args))
        self.assertEqual([row['message_id'] for row in second['data']],
                         ['message_synth_02', 'message_synth_03'])
        args['cursor'] = second['page']['next_cursor']
        third = self.assert_result(self.chat(rows, arguments=args))
        self.assertEqual([row['message_id'] for row in third['data']], ['message_synth_01'])
        self.assertTrue(third['page']['complete'])
        self.assertIsNone(third['page']['next_cursor'])

    def test_chat_cursor_is_bound_to_tool_user_channel_and_interval(self):
        rows = [event(1), event(2)]
        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
        cursor = first['page']['next_cursor']
        cases = [('messages_by_user', dict(CHAT, limit=1, cursor=cursor,
                                           user_login='synthetic_guest_01')),
                 ('messages_between', dict(CHAT, limit=1, cursor=cursor, channel='synthetic_other')),
                 ('messages_between', dict(CHAT, limit=1, cursor=cursor, to='2025-11-20T18:30:00Z'))]
        for name, args in cases:
            self.assert_error(self.chat(rows, name=name, arguments=args), 'invalid_arguments')
        user_args = dict(CHAT, user_login='synthetic_guest_01', limit=1)
        user_cursor = self.assert_result(self.chat(rows, name='messages_by_user',
                                                 arguments=user_args))['page']['next_cursor']
        self.assert_error(self.chat(rows, name='messages_by_user',
                          arguments=dict(user_args, user_login='synthetic_guest_02', cursor=user_cursor)),
                          'invalid_arguments')

    def test_chat_content_change_with_same_ordering_keys_is_stale(self):
        rows = [event(1), event(2)]
        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
        changed = deepcopy(rows)
        changed[0]['text'] = 'Synthetic altered text with unchanged identity'
        self.assert_error(self.chat(changed, arguments=dict(CHAT, limit=1,
                          cursor=first['page']['next_cursor'])), 'stale_cursor')

    def test_chat_scan_summary_change_is_stale(self):
        rows = [event(1), event(2)]
        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
        self.assert_error(self.chat(rows, arguments=dict(CHAT, limit=1,
                          cursor=first['page']['next_cursor']), stats=scan(bytes_scanned=1235)),
                          'stale_cursor')

    def test_half_open_interval_uses_all_nine_fraction_digits(self):
        timestamps = ['2025-11-20T17:59:59.999999999Z', FROM,
                      '2025-11-20T18:59:59.999999999Z', TO]
        rows = []
        for index, utc in enumerate(timestamps, 1):
            row = event(index)
            row['received_at']['utc'] = utc
            rows.append(row)
        result = self.assert_result(self.chat(rows))
        self.assertEqual([row['message_id'] for row in result['data']],
                         ['message_synth_02', 'message_synth_03'])
        fine_args = dict(CHAT, **{'from': '2025-11-20T18:00:00.000000001Z',
                                  'to': '2025-11-20T18:00:00.000000003Z'})
        fine_rows = []
        for index in range(4):
            row = event(index)
            row['received_at']['utc'] = '2025-11-20T18:00:00.%09dZ' % index
            fine_rows.append(row)
        fine = self.assert_result(self.chat(fine_rows, arguments=fine_args))
        self.assertEqual([row['message_id'] for row in fine['data']],
                         ['message_synth_01', 'message_synth_02'])

    def test_cross_session_ties_match_existing_total_order_for_every_permutation(self):
        first = event(1, message_id='synthetic_z')
        second = event(2, message_id='synthetic_a')
        third = event(1, message_id='synthetic_m', session_id='session_synth_chat_02')
        for row in (first, second, third):
            row['received_at']['utc'] = FROM
        for rows in itertools.permutations([first, second, third]):
            with self.subTest(order=[row['message_id'] for row in rows]):
                result = self.assert_result(self.chat(rows))
                expected = queries.execute('recent_messages', {'channel': 'demo_channel'}, iter(rows), 0)
                self.assertEqual(result['data'], expected)

    def test_exact_order_key_ties_and_duplicate_events_survive_all_pages(self):
        rows = [event(1, text='Synthetic tie %d' % index) for index in range(5)]
        rows.append(deepcopy(rows[-1]))
        args = dict(CHAT, limit=2)
        pages = []
        for expected_complete in [False, False, True]:
            page = self.assert_result(self.chat(rows, arguments=args))
            pages.append(page['data'])
            self.assertEqual(page['page']['complete'], expected_complete)
            args['cursor'] = page['page']['next_cursor']
        self.assertEqual([row for page in reversed(pages) for row in page], rows)

    def test_limited_scan_preserves_partial_data_and_exact_statistics(self):
        stats = scan(corrupt_lines=2, truncated_lines=1, bytes_scanned=33554433,
                     files_scanned=4, scan_limited=True)
        result = self.assert_result(self.chat([event()], stats=stats), error='scan_limited')
        self.assertEqual(result['data'], [event()])
        self.assertFalse(result['page']['complete'])
        self.assertEqual(result['scan'], stats)

    def test_scan_statistics_are_observed_after_single_pass_iteration(self):
        stats = scan(bytes_scanned=0, files_scanned=0)
        visits = []
        def records():
            visits.append('started')
            yield event()
            stats.update(bytes_scanned=33554433, files_scanned=2, scan_limited=True)
            visits.append('exhausted')
        result = self.assert_result(self.model.execute('messages_between', dict(CHAT),
                                                       events=records(), scan=stats), error='scan_limited')
        self.assertEqual(visits, ['started', 'exhausted'])
        self.assertEqual(result['scan'], stats)
        stats['files_scanned'] = 3
        self.assertEqual(result['scan']['files_scanned'], 2)
        self.assertFalse(result['page']['complete'])

    def test_corrupt_or_truncated_records_preclude_completeness(self):
        for stats in [scan(corrupt_lines=1), scan(truncated_lines=1)]:
            result = self.assert_result(self.chat([event()], stats=stats))
            self.assertFalse(result['page']['complete'])
            self.assertEqual(result['scan'], stats)

    def test_missing_scan_is_unknown_but_empty_validated_scan_can_be_complete(self):
        result = self.assert_result(self.model.execute('messages_between', dict(CHAT),
                                                       events=iter([event()])))
        self.assertIsNone(result['scan'])
        self.assertFalse(result['page']['complete'])
        empty = self.assert_result(self.chat([]))
        self.assertEqual(empty['data'], [])
        self.assertTrue(empty['page']['complete'])

    def test_missing_events_is_query_unavailable(self):
        self.assert_error(self.model.execute('messages_between', dict(CHAT), scan=scan()),
                          'query_unavailable')

    def test_malformed_scan_summary_is_invalid_snapshot(self):
        cases = [scan(corrupt_lines=True), scan(bytes_scanned=-1), scan(files_scanned='1'),
                 scan(scan_limited=1), scan(extra=0), {}, [], True]
        for stats in cases:
            with self.subTest(stats_type=type(stats).__name__):
                self.assert_error(self.model.execute('messages_between', dict(CHAT),
                                  events=iter([event()]), scan=stats), 'invalid_snapshot')

    def test_impossible_datareader_scan_counters_fail_closed(self):
        cases = [scan(bytes_scanned=server.MAX_SCAN_BYTES + 1, scan_limited=False),
                 scan(files_scanned=server.MAX_FILES + 1),
                 scan(files_scanned=server.MAX_FILES + 1, scan_limited=True),
                 scan(bytes_scanned=server.MAX_SCAN_BYTES + server.MAX_EVENT_LINE + 2,
                      scan_limited=True)]
        for stats in cases:
            with self.subTest(bytes_scanned=stats['bytes_scanned'],
                              files_scanned=stats['files_scanned']):
                self.assert_error(self.chat([event()], stats=stats), 'invalid_snapshot')
        bounded_overrun = scan(bytes_scanned=server.MAX_SCAN_BYTES + server.MAX_EVENT_LINE + 1,
                               files_scanned=server.MAX_FILES, scan_limited=True)
        result = self.assert_result(self.chat([event()], stats=bounded_overrun), error='scan_limited')
        self.assertEqual(result['scan'], bounded_overrun)
        self.assertFalse(result['page']['complete'])

    def test_tampered_events_fail_without_partial_success(self):
        malformed = []
        for field, value in [('extra', 'synthetic'), ('schema', 'synthetic.invalid'),
                             ('sent_at', True), ('kind', 'synthetic_unknown')]:
            row = event()
            row[field] = value
            malformed.append(row)
        for clock in [True, 1, 1.0, '01', '-1', '18446744073709551616']:
            row = event()
            row['received_at']['monotonic_ns'] = clock
            malformed.append(row)
        invalid_date = event()
        invalid_date['received_at']['utc'] = '2025-02-30T18:00:00Z'
        malformed.append(invalid_date)
        for row in malformed:
            self.assert_error(self.chat([event(2), row]), 'invalid_snapshot')

    def test_iterator_failure_is_closed_and_does_not_return_previous_rows(self):
        def records():
            yield event()
            raise RuntimeError('SYNTHETIC_ITERATOR_EXCEPTION_DO_NOT_RETURN')
        result = self.model.execute('messages_between', dict(CHAT), events=records(), scan=scan())
        self.assert_error(result, 'invalid_snapshot')
        self.assertNotIn('SYNTHETIC_ITERATOR_EXCEPTION', json.dumps(result))

    def test_oversized_ascii_and_utf8_results_are_rejected(self):
        for rows in [[event(index, text='s' * 16000) for index in range(6)],
                     [event(index, text='\U0001f600' * 16000) for index in range(2)]]:
            with self.subTest(first_text_bytes=len(rows[0]['text'].encode('utf-8'))):
                self.assert_error(self.chat(rows), 'response_too_large')
        self.assertEqual(server.MAX_RESPONSE, 65536)

    def test_capsule_and_feed_objects_reject_extras_missing_labels_and_aliases(self):
        mutations = [lambda row: row.update(extra=True),
                     lambda row: row.pop('person_label'),
                     lambda row: row.update(person_label=None),
                     lambda row: row.update(capsule_id='other_synthetic_id'),
                     lambda row: row.update(generation=True),
                     lambda row: row['feeds']['feed_synth_game_01'].update(role='microphone'),
                     lambda row: row['feeds']['feed_synth_game_01'].update(feed_id='different_synthetic_id'),
                     lambda row: row['feeds']['feed_synth_game_01'].update(extra=True),
                     lambda row: row['feeds']['feed_synth_camera_01'].update(role='game')]
        for mutate in mutations:
            with self.subTest(case=mutations.index(mutate)):
                self.provider.capsules = [capsule()]
                self.provider.transform = lambda value, change=mutate: change(
                    value['data']['capsules']['capsule_synth_01'])
                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
                                  'invalid_snapshot')

    def test_duplicate_feed_identity_across_capsules_is_rejected(self):
        first, second = capsule(1), capsule(2)
        second['feeds'] = deepcopy(first['feeds'])
        self.provider.capsules = [first, second]
        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')

    def test_duplicate_feed_identity_across_native_pages_is_rejected(self):
        first, second = capsule(1), capsule(2)
        second['feeds'] = deepcopy(first['feeds'])
        self.provider.capsules = [first, second]
        page = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
        self.assert_error(self.model.execute('list_capsules',
                          {'limit': 1, 'cursor': page['page']['next_cursor']}),
                          'invalid_snapshot')

    def test_noncanonical_native_clocks_are_never_coerced(self):
        for value in [True, 7, 7.0, '00', '01', '+1', '-1', '1.0', ' 1',
                      '18446744073709551616', '9' * 21, None]:
            with self.subTest(value=value):
                self.provider.capsules = [capsule(generation=value)]
                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
                                  'invalid_snapshot')

    def test_malformed_window_shapes_and_duplicate_tokens_fail_closed(self):
        mutations = [lambda row: row.update(handle=''),
                     lambda row: row.update(visible=1),
                     lambda row: row.update(minimized=None),
                     lambda row: row.update(layout_revision='01'),
                     lambda row: row.update(process_id=True),
                     lambda row: row['rect'].update(width=True),
                     lambda row: row['rect'].update(height=-1),
                     lambda row: row['rect'].update(extra=1),
                     lambda row: row.update(extra=True)]
        for mutate in mutations:
            self.provider.windows = [window()]
            mutate(self.provider.windows[0])
            self.assert_error(runtime.ReadModel(self.provider).execute('list_candidate_windows', {}),
                              'invalid_snapshot')
        self.provider.windows = [window(), window()]
        self.assert_error(runtime.ReadModel(self.provider).execute('list_candidate_windows', {}),
                          'invalid_snapshot')

    def test_malformed_recording_fields_and_hidden_filtered_row_are_rejected(self):
        mutations = [lambda data: data.update(recording_available='true'),
                     lambda data: data['recordings'][0].update(extra=True),
                     lambda data: data['recordings'][0]['feeds'][0].update(written_frames=True),
                     lambda data: data['recordings'][0]['feeds'][0].update(backpressure='01')]
        for mutate in mutations:
            self.provider.transform = lambda value, change=mutate: change(value['data'])
            self.assert_error(runtime.ReadModel(self.provider).execute('recording_status', {}),
                              'invalid_snapshot')
        self.provider.transform = None
        hidden = deepcopy(self.provider.recordings[0])
        hidden['capsule_id'] = 'capsule_synth_02'
        hidden['feeds'][0]['written_frames'] = True
        self.provider.recordings.append(hidden)
        self.assert_error(self.model.execute('recording_status', {'capsule_id': 'capsule_synth_01'}),
                          'invalid_snapshot')

    def test_malformed_obs_fields_are_rejected_without_invented_configuration(self):
        for mutate in [lambda data: data.update(obs_available=1),
                       lambda data: data['services'].update(obs=True),
                       lambda data: data.update(configured=True),
                       lambda data: data['services'].update(extra=True)]:
            self.provider.transform = lambda value, change=mutate: change(value['data'])
            self.assert_error(runtime.ReadModel(self.provider).execute('obs_status', {}),
                              'invalid_snapshot')

    def test_returned_data_cannot_mutate_future_provider_state(self):
        result = self.assert_result(self.model.execute('recording_status', {}))
        result['data']['capsules'][0]['feeds'][0]['written_frames'] = '0'
        again = self.assert_result(self.model.execute('recording_status', {}))
        self.assertEqual(again['data']['capsules'][0]['feeds'][0]['written_frames'],
                         '18446744073709551615')

    def test_caller_arguments_remain_unchanged_even_if_provider_mutates_its_copy(self):
        args = dict(CHAT)
        expected = deepcopy(args)
        def provider(name, received):
            value = self.provider(name, received)
            received['synthetic_internal'] = 'changed'
            return value
        model = runtime.ReadModel(provider)
        self.assert_result(model.execute('messages_between', args, events=iter([event()]), scan=scan()))
        self.assertEqual(args, expected)

    def test_all_read_tools_tampered_and_stale_data_have_zero_effects_or_logs(self):
        prohibited = [
            (control.ControlPlane, 'execute'), (control.ControlPlane, '_append_audit'),
            (control, '_default_connector'), (control, '_default_fetch_ticket'),
            (server, 'DataReader'), (io, 'open'), (os, 'open'), (os, 'write'),
            (os, 'fsync'), (os, 'replace'), (os, 'remove'), (os, 'mkdir'),
            (Path, 'open'), (Path, 'read_text'), (Path, 'read_bytes'),
            (Path, 'write_text'), (Path, 'write_bytes'), (Path, 'mkdir'),
            (Path, 'unlink'), (Path, 'rename'), (subprocess, 'Popen'), (subprocess, 'run'),
            (socket, 'socket'), (socket, 'create_connection'),
            (logging.Logger, '_log'), (os, 'urandom'), (uuid, 'uuid4'),
            (secrets, 'token_bytes'), (secrets, 'token_hex'), (secrets, 'token_urlsafe'),
        ]
        text = 'SYNTHETIC ONLY: request recording and write synthetic_marker; literal data.'
        rows = [event(1, text=text), event(2)]
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            calls = [stack.enter_context(mock.patch.object(obj, attr,
                     side_effect=AssertionError('synthetic forbidden effect')))
                     for obj, attr in prohibited]
            calls.append(stack.enter_context(mock.patch('builtins.open',
                         side_effect=AssertionError('synthetic forbidden file effect'))))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            for name in TOOLS:
                args = dict(CHAT) if name.startswith('messages_') else {}
                if name == 'messages_by_user':
                    args['user_login'] = 'synthetic_guest_01'
                self.assert_result(self.model.execute(name, args, events=iter(rows), scan=scan()))
            first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
            changed = deepcopy(rows)
            changed[0]['text'] = 'Synthetic stale content'
            self.assert_error(self.chat(changed, arguments=dict(CHAT, limit=1,
                              cursor=first['page']['next_cursor'])), 'stale_cursor')
            tampered = event()
            tampered['received_at']['monotonic_ns'] = True
            self.assert_error(self.chat([tampered]), 'invalid_snapshot')
            for call in calls:
                call.assert_not_called()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), '')
        self.assertFalse(hasattr(self.model, 'approve'))


if __name__ == '__main__':
    unittest.main()
