"""Synthetic loaded-event fixtures for stats only; no JSONL or live services.

All channels, logins, sessions, messages and clocks below are invented fixtures.
The caller's trusted DataReader handoff is modeled with already-loaded events
and its five-field scan dictionary. Storage parsing remains in its own tests.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
import builtins
import inspect
import io
import itertools
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import get_args
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_jsonl_stats as stats
from cafe import mcp_server as mcp


FIXTURE_CHANNEL = 'fixture_channel'
START = '2000-01-01T00:00:00Z'
END = '2000-01-01T00:03:00Z'
ERRORS = {
    'invalid_arguments', 'provider_unavailable', 'invalid_snapshot', 'stale_cursor',
    'query_unavailable', 'scan_limited', 'response_too_large',
}


def fixture_event(utc=START, text='fixture', login='fixture_a', **changes):
    """Return one synthetic event with the base reader's full event shape."""
    value = {
        'schema': 'cafe.chat_event.v1', 'channel': FIXTURE_CHANNEL,
        'received_at': {'monotonic_ns': '7', 'utc': utc}, 'sent_at': None,
        'message_id': None, 'user_login': login, 'display_name': None,
        'badges': [], 'text': text, 'kind': 'message', 'session_id': 'fixture-session',
    }
    value.update(changes)
    return value


def fixture_scan(**changes):
    """Synthetic counters, never measurements or invented runtime scan data."""
    value = {'corrupt_lines': 0, 'truncated_lines': 0, 'bytes_scanned': 8192,
             'files_scanned': 1, 'scan_limited': False}
    value.update(changes)
    return value


def query_args(**changes):
    value = {'channel': FIXTURE_CHANNEL, 'from': START, 'to': END}
    value.update(changes)
    return value


def fixture_second(second):
    return (datetime(2000, 1, 1) + timedelta(seconds=second)).isoformat() + 'Z'


class SinglePassFixture:
    """Loaded fixture that exposes an accidental second aggregation pass."""

    def __init__(self, rows):
        self.rows = rows
        self.iterations = 0
        self.yielded = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError('synthetic_second_pass')
        for row in self.rows:
            self.yielded += 1
            yield row


class StatsContract(unittest.TestCase):
    def call(self, name='chat_statistics', rows=None, arguments=None, scan=None):
        return stats.execute(name, query_args() if arguments is None else arguments,
                             [] if rows is None else rows,
                             scan=fixture_scan() if scan is None else scan)

    def assert_envelope(self, result):
        self.assertEqual(set(result), {'schema', 'availability', 'data', 'page', 'scan', 'error'})
        self.assertEqual(result['schema'], 'cafe.mcp_read.v1')
        self.assertIn(result['availability'], {'available', 'unavailable'})
        self.assertEqual(set(result['page']), {'next_cursor', 'complete'})
        self.assertIs(type(result['page']['complete']), bool)
        cursor = result['page']['next_cursor']
        self.assertTrue(cursor is None or type(cursor) is str)
        if cursor is not None:
            self.assertLessEqual(len(cursor.encode('utf-8')), 512)
        self.assertIn(result['error'], ERRORS | {None})
        json.dumps(result, allow_nan=False)

    def assert_available(self, result, complete=True):
        self.assert_envelope(result)
        self.assertEqual(result['availability'], 'available')
        self.assertIs(type(result['data']), dict)
        self.assertIs(result['page']['complete'], complete)
        self.assertIs(result['data']['complete'], complete)
        self.assertEqual(result['error'], None if complete else 'scan_limited')

    def assert_error(self, result, error):
        self.assert_envelope(result)
        self.assertEqual(result['availability'], 'unavailable')
        self.assertIsNone(result['data'])
        self.assertEqual(result['page'], {'next_cursor': None, 'complete': False})
        self.assertEqual(result['error'], error)

    def test_frozen_signatures_and_closed_error_enum(self):
        self.assertEqual(list(inspect.signature(stats.tool_definitions).parameters), [])
        parameters = inspect.signature(stats.execute).parameters
        self.assertEqual(list(parameters), ['name', 'arguments', 'events', 'scan'])
        self.assertEqual(parameters['scan'].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameters['scan'].default, inspect.Parameter.empty)
        self.assertEqual(set(get_args(stats.ErrorCode)), ERRORS)

    def test_exactly_two_typed_closed_readonly_descriptors(self):
        definitions = stats.tool_definitions()
        self.assertIs(type(definitions), list)
        self.assertEqual(len(definitions), 2)
        self.assertEqual({item['name'] for item in definitions},
                         {'chat_statistics', 'chat_activity'})
        for item in definitions:
            with self.subTest(tool=item['name']):
                schema = item['inputSchema']
                self.assertEqual(schema['type'], 'object')
                self.assertIs(schema['additionalProperties'], False)
                self.assertEqual(set(schema['required']), {'channel', 'from', 'to'})
                props = schema['properties']
                expected = {'channel', 'from', 'to', 'bucket_seconds', 'top_limit'}
                if item['name'] == 'chat_activity':
                    expected |= {'limit', 'cursor'}
                self.assertEqual(set(props), expected)
                for field in ('channel', 'from', 'to'):
                    self.assertEqual(props[field]['type'], 'string')
                    self.assertIn('pattern', props[field])
                for field, maximum, default in [('bucket_seconds', 3600, 60),
                                                 ('top_limit', 50, 10)]:
                    self.assertEqual(props[field]['type'], 'integer')
                    self.assertEqual(props[field]['minimum'], 1)
                    self.assertEqual(props[field]['maximum'], maximum)
                    self.assertEqual(props[field]['default'], default)
                if item['name'] == 'chat_activity':
                    self.assertEqual(props['limit'],
                                     {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50})
                    self.assertEqual(set(props['cursor']['type']), {'string', 'null'})
                    self.assertEqual(props['cursor']['maxLength'], 512)
                self.assertEqual(item['annotations'], {
                    'readOnlyHint': True, 'destructiveHint': False,
                    'idempotentHint': True, 'openWorldHint': False,
                })

    def test_descriptor_results_and_existing_six_tools_are_not_mutated(self):
        expected_names = {'list_channels_and_days', 'recent_messages', 'search_messages',
                          'top_chatters', 'message_rate', 'session_summary'}
        original = deepcopy(mcp.tools())
        definitions = stats.tool_definitions()
        definitions[0]['name'] = 'fixture_mutation'
        definitions[0]['inputSchema']['properties']['top_limit']['maximum'] = 999
        self.call(rows=[fixture_event()])
        self.assertEqual(mcp.tools(), original)
        self.assertEqual({item['name'] for item in mcp.tools()}, expected_names)
        self.assertEqual(len(mcp.tools()), 6)
        self.assertEqual({item['name'] for item in stats.tool_definitions()},
                         {'chat_statistics', 'chat_activity'})
        self.assertEqual(stats.tool_definitions()[0]['inputSchema']['properties']['top_limit']['maximum'], 50)

    def test_small_aggregate_matches_hand_counted_result(self):
        rows = [
            fixture_event(text='Pear pear APPLE'),
            fixture_event('2000-01-01T00:00:59Z', 'apple fig', 'fixture_b'),
            fixture_event('2000-01-01T00:01:00Z', 'fig PEAR'),
            fixture_event('2000-01-01T00:02:00Z', 'apple fig', None),
            fixture_event(text='ignored', kind='notice'),
            fixture_event(text='ignored', channel='fixture_other'),
            fixture_event('1999-12-31T23:59:59Z', 'ignored'),
            fixture_event(END, 'ignored'),
        ]
        scan = fixture_scan()
        result = self.call(rows=rows, scan=scan)
        self.assert_available(result)
        self.assertIs(result['scan'], scan)
        self.assertIsNone(result['page']['next_cursor'])
        self.assertEqual(result['data'], {
            'message_count': 4, 'unique_chatters': 2,
            'messages_per_bucket': [
                {'start': START, 'count': 2},
                {'start': '2000-01-01T00:01:00Z', 'count': 1},
                {'start': '2000-01-01T00:02:00Z', 'count': 1},
            ],
            'top_words': [{'word': 'apple', 'count': 3}, {'word': 'fig', 'count': 3},
                          {'word': 'pear', 'count': 3}],
            'peaks': [{'start': START, 'count': 2}],
            'top_emotes': None, 'emotes_status': 'metadata_unavailable', 'complete': True,
        })

    def test_word_and_peak_ties_do_not_depend_on_event_order(self):
        rows = [fixture_event(text='zebra alpha'),
                fixture_event('2000-01-01T00:01:00Z', 'BETA alpha'),
                fixture_event('2000-01-01T00:02:00Z', 'zebra BETA')]
        for order in itertools.permutations(rows):
            result = self.call(rows=order)
            self.assert_available(result)
            self.assertEqual(result['data']['top_words'], [
                {'word': 'alpha', 'count': 2}, {'word': 'beta', 'count': 2},
                {'word': 'zebra', 'count': 2},
            ])
            self.assertEqual(result['data']['peaks'], [
                {'start': START, 'count': 1},
                {'start': '2000-01-01T00:01:00Z', 'count': 1},
                {'start': '2000-01-01T00:02:00Z', 'count': 1},
            ])

    def test_unicode_words_follow_documented_rule_exactly(self):
        text = ('Straße STRASSE É É １２٣ a²b _x_ cats cat cafe\u0301 e\u0301 Ⅳ ① '
                + 'q' * 64 + ' ' + 'r' * 65 + ' ' + 'ß' * 32 + ' ' + 'ß' * 33)
        result = self.call(rows=[fixture_event(text=text)], arguments=query_args(top_limit=50))
        self.assert_available(result)
        self.assertEqual(result['data']['top_words'], [
            {'word': 'strasse', 'count': 2}, {'word': 'é', 'count': 2},
            *[{'word': word, 'count': 1} for word in
              ['a', 'b', 'cafe', 'cat', 'cats', 'e', 'q' * 64, 's' * 64, 'x', '１２٣']],
        ])
        rule = ('normalize each message `text` by Unicode case folding, splitting on any sequence '
                'that is not a letter or decimal number, dropping empty tokens and tokens longer '
                'than 64 characters, without stemming.')
        self.assertIn(rule, ' '.join(stats.__doc__.split()))

    def test_default_and_custom_top_limits(self):
        words = ['aa', 'bb', 'cc', 'dd', 'ee', 'ff', 'gg', 'hh', 'ii', 'jj', 'kk', 'll']
        rows = [fixture_event(text=' '.join(reversed(words)))]
        for options, count in [({}, 10), ({'top_limit': 1}, 1), ({'top_limit': 50}, 12)]:
            with self.subTest(options=options):
                result = self.call(rows=rows, arguments=query_args(**options))
                self.assert_available(result)
                self.assertEqual(result['data']['top_words'],
                                 [{'word': word, 'count': 1} for word in words[:count]])

    def test_null_and_missing_logins_never_become_chatters(self):
        missing = fixture_event()
        del missing['user_login']
        rows = [fixture_event(login=None), fixture_event(login=None), missing,
                fixture_event(), fixture_event(), fixture_event(login='fixture_b')]
        before = deepcopy(rows)
        result = self.call(rows=rows)
        self.assert_available(result)
        self.assertEqual(result['data']['message_count'], 6)
        self.assertEqual(result['data']['unique_chatters'], 2)
        self.assertEqual(rows, before)
        self.assertNotIn('user_login', missing)

    def test_empty_history_and_unknown_channel_include_zero_buckets(self):
        for rows, channel in [([], FIXTURE_CHANNEL), ([fixture_event()], 'fixture_unknown')]:
            result = self.call(rows=rows, arguments=query_args(channel=channel))
            self.assert_available(result)
            self.assertEqual(result['data']['message_count'], 0)
            self.assertEqual(result['data']['unique_chatters'], 0)
            self.assertEqual(result['data']['messages_per_bucket'], [
                {'start': START, 'count': 0},
                {'start': '2000-01-01T00:01:00Z', 'count': 0},
                {'start': '2000-01-01T00:02:00Z', 'count': 0},
            ])
            self.assertEqual(result['data']['top_words'], [])
            self.assertEqual(result['data']['peaks'], [])
            self.assertIsNone(result['data']['top_emotes'])
            self.assertEqual(result['data']['emotes_status'], 'metadata_unavailable')

    def test_default_and_custom_buckets_are_anchored_at_from(self):
        arguments = query_args(**{'from': '2000-01-01T00:00:10Z', 'to': '2000-01-01T00:02:15Z'})
        rows = [fixture_event('2000-01-01T00:01:09Z'), fixture_event('2000-01-01T00:01:10Z'),
                fixture_event('2000-01-01T00:02:14.999999999Z')]
        default = self.call(rows=rows, arguments=arguments)
        self.assert_available(default)
        self.assertEqual(default['data']['messages_per_bucket'], [
            {'start': '2000-01-01T00:00:10Z', 'count': 1},
            {'start': '2000-01-01T00:01:10Z', 'count': 1},
            {'start': '2000-01-01T00:02:10Z', 'count': 1},
        ])
        custom = self.call(rows=rows, arguments={**arguments, 'bucket_seconds': 120})
        self.assert_available(custom)
        self.assertEqual(custom['data']['messages_per_bucket'], [
            {'start': '2000-01-01T00:00:10Z', 'count': 2},
            {'start': '2000-01-01T00:02:10Z', 'count': 1},
        ])
        maximum = self.call(rows=rows, arguments={**arguments, 'bucket_seconds': 3600})
        self.assert_available(maximum)
        self.assertEqual(maximum['data']['messages_per_bucket'],
                         [{'start': arguments['from'], 'count': 3}])

    def test_half_open_interval_and_bucket_edges_keep_nanosecond_precision(self):
        start = '2000-01-01T00:00:00.123456789Z'
        end = '2000-01-01T00:00:02.123456790Z'
        rows = [fixture_event(value) for value in [
            '2000-01-01T00:00:00.123456788Z', start,
            '2000-01-01T00:00:01.123456788Z', '2000-01-01T00:00:01.123456789Z',
            '2000-01-01T00:00:02.123456789Z', end,
        ]]
        result = self.call(rows=rows, arguments=query_args(**{'from': start, 'to': end, 'bucket_seconds': 1}))
        self.assert_available(result)
        self.assertEqual(result['data']['message_count'], 4)
        self.assertEqual(result['data']['messages_per_bucket'], [
            {'start': start, 'count': 2},
            {'start': '2000-01-01T00:00:01.123456789Z', 'count': 1},
            {'start': '2000-01-01T00:00:02.123456789Z', 'count': 1},
        ])

    def test_calendar_rollovers_and_extreme_valid_years(self):
        intervals = [
            ('2000-02-28T23:59:59Z', '2000-02-29T00:00:01Z', '2000-02-29T00:00:00Z'),
            ('2000-12-31T23:59:59Z', '2001-01-01T00:00:01Z', '2001-01-01T00:00:00Z'),
            ('0001-01-01T00:00:00Z', '0001-01-01T00:00:02Z', '0001-01-01T00:00:01Z'),
            ('9999-12-31T23:59:57Z', '9999-12-31T23:59:59Z', '9999-12-31T23:59:58Z'),
        ]
        for start, end, second in intervals:
            with self.subTest(start=start):
                result = self.call(rows=[fixture_event(second)],
                                   arguments=query_args(**{'from': start, 'to': end, 'bucket_seconds': 1}))
                self.assert_available(result)
                self.assertEqual(result['data']['messages_per_bucket'],
                                 [{'start': start, 'count': 0}, {'start': second, 'count': 1}])

    def test_equivalent_fraction_spellings_compare_as_instants(self):
        result = self.call(rows=[fixture_event('2000-01-01T00:00:00.100000000Z')],
                           arguments=query_args(**{'from': '2000-01-01T00:00:00.1Z',
                                                   'to': '2000-01-01T00:00:00.100000001Z'}))
        self.assert_available(result)
        self.assertEqual(result['data']['message_count'], 1)

    def test_empty_and_inverted_intervals_are_invalid(self):
        for start, end in [(START, START), (END, START),
                           ('2000-01-01T00:00:00.1Z', '2000-01-01T00:00:00.100000000Z')]:
            for name in ('chat_statistics', 'chat_activity'):
                self.assert_error(self.call(name, arguments=query_args(**{'from': start, 'to': end})),
                                  'invalid_arguments')

    def test_invalid_utc_formats_and_impossible_calendar_fields(self):
        invalid = [None, True, 0, 1.5, '', '2000-01-01', '2000-01-01T00:00:00',
                   '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00z',
                   '2000-01-01 00:00:00Z', '2000-01-01T00:00:00.Z',
                   '2000-01-01T00:00:00.' + ('1' * 10) + 'Z', '2000-01-01T00:00:00Z\n',
                   '2001-02-29T00:00:00Z', '2000-02-30T00:00:00Z',
                   '2000-13-01T00:00:00Z', '2000-01-00T00:00:00Z',
                   '2000-01-01T24:00:00Z', '2000-01-01T00:60:00Z',
                   '2000-01-01T00:00:60Z', '0000-01-01T00:00:00Z',
                   '２０００-01-01T00:00:00Z']
        for field, value in itertools.product(('from', 'to'), invalid):
            with self.subTest(field=field, value=value):
                self.assert_error(self.call(arguments=query_args(**{field: value})), 'invalid_arguments')
        for value in invalid:
            with self.subTest(event_utc=value):
                self.assert_error(self.call(rows=[fixture_event(value)]), 'provider_unavailable')

    def test_channels_are_strict_lowercase_ascii_and_bounded(self):
        for channel in ['abc', '0__', 'a' * 25]:
            self.assert_available(self.call(arguments=query_args(channel=channel)))
        for channel in [None, True, 3, [], '', 'ab', 'a' * 26, 'Fixture',
                        'fixture-channel', 'fixture channel', 'fixturé', 'abc\n', '../fixture']:
            with self.subTest(channel=channel):
                self.assert_error(self.call(arguments=query_args(channel=channel)), 'invalid_arguments')

    def test_numeric_arguments_reject_wrong_types_booleans_and_range_errors(self):
        for name in ('chat_statistics', 'chat_activity'):
            for field, maximum in [('bucket_seconds', 3600), ('top_limit', 50)]:
                for value in [None, False, True, '1', 1.0, [], {}, -1, 0, maximum + 1]:
                    with self.subTest(tool=name, field=field, value=value):
                        self.assert_error(self.call(name, arguments=query_args(**{field: value})),
                                          'invalid_arguments')
        for value in [None, False, True, '1', 1.0, [], {}, -1, 0, 201]:
            self.assert_error(self.call('chat_activity', arguments=query_args(limit=value)), 'invalid_arguments')

    def test_unknown_tools_missing_and_unknown_arguments_are_closed(self):
        for name in [None, True, 1, [], {}, '', 'fixture_unknown', 'start_recording', 'send_to_obs']:
            self.assert_error(self.call(name), 'invalid_arguments')
        for arguments in [None, [], (), 'fixture', True, 1]:
            self.assert_error(stats.execute('chat_statistics', arguments, [], scan=fixture_scan()),
                              'invalid_arguments')
        for name in ('chat_statistics', 'chat_activity'):
            for field in ('channel', 'from', 'to'):
                arguments = query_args()
                del arguments[field]
                self.assert_error(self.call(name, arguments=arguments), 'invalid_arguments')
            self.assert_error(self.call(name, arguments=query_args(fixture_extra=True)), 'invalid_arguments')
        for arguments in [query_args(limit=1), query_args(cursor=None)]:
            self.assert_error(self.call(arguments=arguments), 'invalid_arguments')

    def test_bucket_limit_uses_ceiling_and_rejects_1001_before_iteration(self):
        valid = query_args(**{'to': '2000-01-01T00:16:40Z', 'bucket_seconds': 1, 'limit': 200})
        result = self.call('chat_activity', arguments=valid)
        self.assert_available(result)
        self.assertEqual(len(result['data']['buckets']), 200)
        for end in ['2000-01-01T00:16:41Z', '2000-01-01T00:16:40.000000001Z']:
            for name in ('chat_statistics', 'chat_activity'):
                rows = SinglePassFixture([fixture_event()])
                self.assert_error(self.call(name, rows, query_args(**{'to': end, 'bucket_seconds': 1})),
                                  'invalid_arguments')
                self.assertEqual(rows.iterations, 0)

    def test_canonical_clock_boundaries_preserve_strings_and_null(self):
        for clock in ['0', '1', '9007199254740993', '18446744073709551615']:
            for sent in [None, clock]:
                value = fixture_event(sent_at=sent)
                value['received_at']['monotonic_ns'] = clock
                before = deepcopy(value)
                result = self.call(rows=[value])
                self.assert_available(result)
                self.assertEqual(value, before)
                self.assertIs(type(value['received_at']['monotonic_ns']), str)
                self.assertTrue(value['sent_at'] is None or type(value['sent_at']) is str)

    def test_noncanonical_clocks_fail_as_unavailable_provider(self):
        invalid = [True, False, 0, -1, 1.0, '', '00', '01', '+1', '-0', '1e3',
                   ' 1', '1 ', '1\n', '١', '18446744073709551616', '100000000000000000000']
        for field in ('monotonic_ns', 'sent_at'):
            for clock in invalid + ([None] if field == 'monotonic_ns' else []):
                with self.subTest(field=field, clock=clock):
                    value = fixture_event()
                    if field == 'monotonic_ns':
                        value['received_at'][field] = clock
                    else:
                        value[field] = clock
                    self.assert_error(self.call(rows=[value]), 'provider_unavailable')

    def test_malformed_event_fields_cannot_be_silently_ignored(self):
        changes = [
            {'schema': 'fixture.invalid'}, {'fixture_extra': True}, {'channel': 'Bad'},
            {'received_at': START}, {'received_at': {'monotonic_ns': '0', 'utc': START, 'extra': 1}},
            {'message_id': ''}, {'message_id': 1}, {'message_id': 'x' * 129},
            {'user_login': ''}, {'user_login': 'Bad'}, {'user_login': True},
            {'display_name': 1}, {'display_name': 'x' * 129},
            {'badges': ()}, {'badges': [{'name': 'fixture'}]},
            {'badges': [{'name': 'fixture', 'version': 1}]},
            {'badges': [{'name': 'fixture', 'version': '1', 'extra': True}]},
            {'badges': [{'name': 'x' * 65, 'version': '1'}]},
            {'badges': [{'name': 'fixture', 'version': '1'}] * 65},
            {'text': True}, {'text': {}}, {'text': 'x' * 16385}, {'text': '\ud800'},
            {'kind': []}, {'kind': 'fixture_invalid'}, {'session_id': ''},
            {'session_id': None}, {'session_id': 'fixture/session'}, {'session_id': 'x' * 97},
        ]
        for index, change in enumerate(changes):
            with self.subTest(fixture=index):
                self.assert_error(self.call(rows=[fixture_event(**change)]), 'provider_unavailable')
        for field in ('text', 'schema', 'received_at', 'sent_at', 'channel'):
            value = fixture_event()
            del value[field]
            self.assert_error(self.call(rows=[value]), 'provider_unavailable')
        self.assert_error(self.call(rows=[fixture_event(channel='fixture_other', sent_at=0)]),
                          'provider_unavailable')

    def test_missing_raw_json_and_malformed_providers_are_unavailable(self):
        # An upstream parse failure must be handed off as unavailable; this
        # module never parses JSONL or mistakes failed parsing for zero messages.
        for events in [None, True, 1, {}, '', 'not json', b'not json', [None],
                       [[], fixture_event()], [json.dumps(fixture_event())], io.StringIO('fixture')]:
            with self.subTest(provider_type=type(events).__name__):
                result = stats.execute('chat_statistics', query_args(), events, scan=fixture_scan())
                self.assert_error(result, 'provider_unavailable')
        for scan in [None, [], '', {'invalid': 0, 'truncated': 0}]:
            result = stats.execute('chat_statistics', query_args(), [], scan=scan)
            self.assert_error(result, 'provider_unavailable')
            self.assertIsNone(result['scan'])

    def test_scan_schema_is_exact_and_rejects_wrong_counter_types(self):
        for field in fixture_scan():
            scan = fixture_scan()
            del scan[field]
            self.assert_error(self.call(scan=scan), 'provider_unavailable')
        self.assert_error(self.call(scan=fixture_scan(invalid=1)), 'provider_unavailable')
        for field in ('corrupt_lines', 'truncated_lines', 'bytes_scanned', 'files_scanned'):
            for value in [None, False, True, -1, 1.5, '0', [], {}]:
                with self.subTest(field=field, value=value):
                    self.assert_error(self.call(scan=fixture_scan(**{field: value})), 'provider_unavailable')
        for value in [None, 0, 1, 'false', [], {}]:
            self.assert_error(self.call(scan=fixture_scan(scan_limited=value)), 'provider_unavailable')

    def test_corrupt_truncated_and_limited_scans_preserve_counts_and_identity(self):
        for change in [{'corrupt_lines': 1}, {'truncated_lines': 1}, {'scan_limited': True},
                       {'corrupt_lines': 2, 'truncated_lines': 3, 'scan_limited': True}]:
            for name in ('chat_statistics', 'chat_activity'):
                scan = fixture_scan(**change)
                before = dict(scan)
                result = self.call(name, [fixture_event()], scan=scan)
                self.assert_available(result, complete=False)
                self.assertIs(result['scan'], scan)
                self.assertEqual(scan, before)
                buckets = result['data']['messages_per_bucket' if name == 'chat_statistics' else 'buckets']
                self.assertEqual([bucket['count'] for bucket in buckets], [1, 0, 0])
                if name == 'chat_statistics':
                    self.assertEqual(result['data']['message_count'], 1)
        # No usable lines from a damaged scan cannot become a complete empty success.
        result = self.call(scan=fixture_scan(corrupt_lines=1))
        self.assert_available(result, complete=False)
        self.assertEqual(result['data']['message_count'], 0)

    def test_failed_or_changed_provider_discards_partial_work(self):
        def broken():
            yield fixture_event()
            raise ValueError('synthetic_provider_failure_not_for_output')
        result = self.call(rows=broken())
        self.assert_error(result, 'provider_unavailable')
        self.assertNotIn('synthetic_provider_failure', json.dumps(result))
        scan = fixture_scan()
        def changed():
            yield fixture_event()
            scan['bytes_scanned'] += 1
        self.assert_error(self.call(rows=changed(), scan=scan), 'provider_unavailable')

    def test_lazy_data_reader_is_rejected_before_its_file_reading_body(self):
        reader = object.__new__(mcp.DataReader)
        reader.stats = fixture_scan()
        records = reader.events(FIXTURE_CHANNEL)
        original_state = inspect.getgeneratorstate(records)
        try:
            with mock.patch.object(mcp, '_directory', side_effect=AssertionError('synthetic_io')) as directory:
                result = self.call(rows=records, scan=reader.stats)
            self.assert_error(result, 'provider_unavailable')
            self.assertEqual(inspect.getgeneratorstate(records), original_state)
            directory.assert_not_called()
        finally:
            records.close()

    def test_each_aggregate_uses_one_pass_and_never_mutates_loaded_inputs(self):
        rows = [fixture_event(), fixture_event('2000-01-01T00:01:00Z', 'fixture second')]
        for name in ('chat_statistics', 'chat_activity'):
            source = SinglePassFixture(rows)
            scan = fixture_scan()
            arguments = query_args()
            before = deepcopy((rows, scan, arguments))
            result = self.call(name, source, arguments, scan)
            self.assert_available(result)
            self.assertEqual(source.iterations, 1)
            self.assertEqual(source.yielded, 2)
            self.assertEqual((rows, scan, arguments), before)

    def test_activity_pages_equal_aggregate_and_complete_describes_whole_scan(self):
        rows = [fixture_event(), fixture_event('2000-01-01T00:01:00Z')]
        arguments = query_args(**{'to': '2000-01-01T00:01:45Z', 'bucket_seconds': 1})
        scan = fixture_scan()
        aggregate = self.call(rows=rows, arguments=arguments, scan=scan)
        self.assert_available(aggregate)
        first = self.call('chat_activity', rows, arguments, scan)
        self.assert_available(first)
        self.assertEqual(set(first['data']), {'channel', 'from', 'to', 'bucket_seconds', 'buckets', 'complete'})
        self.assertEqual(first['data']['channel'], FIXTURE_CHANNEL)
        self.assertEqual(first['data']['from'], START)
        self.assertEqual(first['data']['to'], arguments['to'])
        self.assertEqual(first['data']['bucket_seconds'], 1)
        self.assertEqual(len(first['data']['buckets']), 50)
        cursor = first['page']['next_cursor']
        self.assertIs(type(cursor), str)
        for private in [FIXTURE_CHANNEL, START, arguments['to'], 'fixture_a']:
            self.assertNotIn(private, cursor)
        second = self.call('chat_activity', rows, {**arguments, 'cursor': cursor}, scan)
        self.assert_available(second)
        self.assertEqual(len(second['data']['buckets']), 50)
        third = self.call('chat_activity', rows, {**arguments, 'cursor': second['page']['next_cursor']}, scan)
        self.assert_available(third)
        self.assertEqual(len(third['data']['buckets']), 5)
        self.assertIsNone(third['page']['next_cursor'])
        self.assertEqual(first['data']['buckets'] + second['data']['buckets'] + third['data']['buckets'],
                         aggregate['data']['messages_per_bucket'])
        replay = self.call('chat_activity', rows, {**arguments, 'cursor': cursor}, scan)
        self.assertEqual(replay, second)

    def test_cursor_chain_is_deterministic_and_defaults_are_normalized(self):
        rows = [fixture_event()]
        arguments = query_args(**{'to': '2000-01-01T01:00:00Z'})
        scan = fixture_scan()
        first = self.call('chat_activity', rows, arguments, scan)
        self.assert_available(first)
        again = self.call('chat_activity', deepcopy(rows), dict(reversed(list(arguments.items()))), dict(scan))
        self.assertEqual(first, again)
        explicit = {**arguments, 'bucket_seconds': 60, 'top_limit': 10, 'limit': 50,
                    'cursor': first['page']['next_cursor']}
        next_page = self.call('chat_activity', rows, explicit, scan)
        self.assert_available(next_page)
        self.assertEqual(len(next_page['data']['buckets']), 10)
        self.assertIsNone(next_page['page']['next_cursor'])
        self.assertEqual(self.call('chat_activity', rows, {**arguments, 'cursor': None}, scan), first)

    def test_cursor_is_bound_to_every_query_parameter(self):
        rows = [fixture_event()]
        arguments = query_args(limit=1)
        scan = fixture_scan()
        first = self.call('chat_activity', rows, arguments, scan)
        cursor = first['page']['next_cursor']
        for change in [
            {'channel': 'fixture_other'}, {'from': '2000-01-01T00:00:01Z'},
            {'from': '2000-01-01T00:00:00.0Z'}, {'to': '2000-01-01T00:04:00Z'},
            {'bucket_seconds': 30}, {'top_limit': 2}, {'limit': 2},
        ]:
            with self.subTest(change=change):
                result = self.call('chat_activity', rows, {**arguments, **change, 'cursor': cursor}, scan)
                self.assert_error(result, 'stale_cursor')

    def test_cursor_is_bound_to_events_outside_filter_and_scan_identity(self):
        rows = [fixture_event(), fixture_event(text='ignored fixture', kind='notice'),
                fixture_event(channel='fixture_other'), fixture_event(END)]
        arguments = query_args(limit=1)
        scan = fixture_scan()
        first = self.call('chat_activity', rows, arguments, scan)
        continuation = {**arguments, 'cursor': first['page']['next_cursor']}
        for index, field, value in [
            (0, 'text', 'changed fixture'), (0, 'session_id', 'fixture-other-session'),
            (1, 'text', 'changed ignored fixture'), (2, 'text', 'changed other fixture'),
            (3, 'text', 'changed outside fixture'),
        ]:
            changed = deepcopy(rows)
            changed[index][field] = value
            self.assert_error(self.call('chat_activity', changed, continuation, scan), 'stale_cursor')
        changed = deepcopy(rows)
        changed[0]['received_at']['monotonic_ns'] = '8'
        self.assert_error(self.call('chat_activity', changed, continuation, scan), 'stale_cursor')
        self.assert_error(self.call('chat_activity', rows, continuation,
                                    {**scan, 'bytes_scanned': scan['bytes_scanned'] + 1}), 'stale_cursor')
        reordered = [dict(reversed(list(value.items()))) for value in rows]
        self.assert_available(self.call('chat_activity', reordered, continuation, dict(reversed(list(scan.items())))))

    def test_cursor_tampering_wrong_types_and_utf8_byte_limit(self):
        arguments = query_args(limit=1)
        first = self.call('chat_activity', arguments=arguments)
        cursor = first['page']['next_cursor']
        altered = ('a' if cursor[0] != 'a' else 'b') + cursor[1:]
        for value in [altered, cursor + 'x', '', 'fixture_unknown_cursor', 'x' * 512, 'é' * 256]:
            self.assert_error(self.call('chat_activity', arguments={**arguments, 'cursor': value}), 'stale_cursor')
        for value in [True, 1, [], {}, b'fixture', 'x' * 513, 'é' * 257, '😀' * 129, '\ud800']:
            self.assert_error(self.call('chat_activity', arguments={**arguments, 'cursor': value}), 'invalid_arguments')

    def test_incomplete_scan_stays_incomplete_on_every_activity_page(self):
        arguments = query_args(limit=1)
        scan = fixture_scan(truncated_lines=1)
        result = self.call('chat_activity', [fixture_event()], arguments, scan)
        for index in range(3):
            self.assert_available(result, complete=False)
            self.assertIs(result['scan'], scan)
            self.assertEqual(len(result['data']['buckets']), 1)
            if index < 2:
                self.assertIsNotNone(result['page']['next_cursor'])
                result = self.call('chat_activity', [fixture_event()],
                                   {**arguments, 'cursor': result['page']['next_cursor']}, scan)
            else:
                self.assertIsNone(result['page']['next_cursor'])

    def test_oversized_aggregate_fails_closed_while_bucket_pages_fit(self):
        rows = [fixture_event(fixture_second(index)) for index in range(1000)]
        arguments = query_args(**{'to': fixture_second(1000), 'bucket_seconds': 1})
        scan = fixture_scan()
        result = self.call(rows=rows, arguments=arguments, scan=scan)
        self.assert_error(result, 'response_too_large')
        self.assertIs(result['scan'], scan)
        self.assertLessEqual(len(json.dumps(result).encode('utf-8')) + 1, mcp.MAX_RESPONSE)
        page = self.call('chat_activity', rows, {**arguments, 'limit': 200}, scan)
        self.assert_available(page)
        self.assertEqual(len(page['data']['buckets']), 200)
        self.assertTrue(all(bucket['count'] == 1 for bucket in page['data']['buckets']))
        self.assertIsNotNone(page['page']['next_cursor'])

    def test_available_results_fit_existing_doubled_mcp_wire_envelope(self):
        for name in ('chat_statistics', 'chat_activity'):
            result = self.call(name, [fixture_event(text='é fixture')])
            self.assert_available(result)
            payload = {'data': result, 'scan': result['scan']}
            text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            frame = {'jsonrpc': '2.0', 'id': '\x00' * 96,
                     'result': {'content': [{'type': 'text', 'text': text}],
                                'structuredContent': payload, 'isError': False}}
            self.assertLessEqual(len(json.dumps(frame, ensure_ascii=False).encode('utf-8')) + 1,
                                 mcp.MAX_RESPONSE)

    def test_success_error_and_pagination_paths_never_do_io_control_or_logging(self):
        rows = [fixture_event(text='fixture start_recording send_to_obs confirm')]
        large = [fixture_event(fixture_second(index)) for index in range(1000)]
        cases = [
            ('chat_statistics', query_args(), rows, fixture_scan(), None),
            ('chat_activity', query_args(limit=1), rows, fixture_scan(), None),
            ('chat_statistics', query_args(bucket_seconds=True), rows, fixture_scan(), 'invalid_arguments'),
            ('chat_statistics', query_args(), None, fixture_scan(), 'provider_unavailable'),
            ('chat_activity', query_args(limit=1, cursor='fixture_unknown'), rows, fixture_scan(), 'stale_cursor'),
            ('chat_statistics', query_args(), rows, fixture_scan(corrupt_lines=1), 'scan_limited'),
            ('chat_statistics', query_args(**{'to': fixture_second(1000), 'bucket_seconds': 1}),
             large, fixture_scan(), 'response_too_large'),
        ]
        targets = [(builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'listdir'),
                   (os, 'scandir'), (socket, 'socket'), (socket, 'create_connection'),
                   (subprocess, 'Popen'), (mcp.DataReader, '__init__'),
                   (mcp.Server, 'handle'), (logging.Logger, '_log')]
        output, errors = io.StringIO(), io.StringIO()
        effect_calls = []
        old_profile = sys.getprofile()
        def forbid_effect_call(frame, event, unused):
            module = frame.f_globals.get('__name__', '')
            if event == 'call' and module.startswith(('cafe.', 'connectors.cafe.')):
                leaf = module.rsplit('.', 1)[-1]
                if any(word in leaf for word in ('control', 'authority', 'approval', 'confirm',
                                                 'record', 'broker', 'obs_', 'twitch_')):
                    effect_calls.append(leaf)
                    raise AssertionError('synthetic_effect_forbidden')
        with ExitStack() as stack:
            guards = [stack.enter_context(mock.patch.object(owner, name,
                      side_effect=AssertionError('synthetic_io_or_effect_forbidden')))
                      for owner, name in targets]
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            sys.setprofile(forbid_effect_call)
            try:
                for name, arguments, events, scan, expected_error in cases:
                    result = stats.execute(name, arguments, events, scan=scan)
                    self.assert_envelope(result)
                    self.assertEqual(result['error'], expected_error)
            finally:
                sys.setprofile(old_profile)
        for guard in guards:
            guard.assert_not_called()
        self.assertEqual(effect_calls, [])
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
