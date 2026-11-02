"""Synthetic, stdlib-only proposal-queue regressions; no native integration claim.

Run: python -B -m unittest discover -s tests/integration -p test_mcp_control_intents.py -v
The fake view is an identity projection, not the (kind, arguments) read model.
No fixture authorizes an effect or supplies a native bind_window implementation.
"""

import ast
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_control_intents as proposals


CAPSULE = 'synthetic-capsule'
WINDOW = 'synthetic window \u2603 : !'
EPOCH = '7'
REVISION = '11'
NOW = (1 << 60) + 17
TTL_NS = 30_000_000_000
OPERATIONS = ('bind_window', 'start_recording', 'stop_recording', 'send_to_obs')
REPLY_KEYS = {'schema', 'proposal_id', 'request_id', 'state', 'error',
              'expires_in_ms'}
ERRORS = {None, 'invalid_proposal', 'provider_unavailable',
          'confirmation_required', 'stale_revision', 'request_id_conflict',
          'proposal_capacity', 'proposal_expired', 'proposal_consumed'}
BAD_CLOCKS = (None, True, False, 0, 1, 1.0, '', '00', '01', '+1', '-1',
              ' 1', '1 ', '1\n', '1.0', '1e0', '\u0661', '\uff11',
              '18446744073709551616', '9' * 21, [], {})
BAD_IDENTIFIERS = (None, True, False, 1, 1.0, '', '-synthetic',
                   'synthetic space', 'synthetic\n', '\u00e9', 's' * 97, [], {})
_DEFAULT_REQUEST = object()


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def make_view():
    return {
        'authority_epoch': EPOCH,
        'revision': REVISION,
        'capsules': {
            CAPSULE: {
                'capsule_id': CAPSULE,
                'generation': '3',
                'feeds': {
                    'game': {'feed_id': 'synthetic-feed-game', 'role': 'game',
                             'generation': '5'},
                    'camera': {'feed_id': 'synthetic-feed-camera',
                               'role': 'camera', 'generation': '9'},
                },
            },
        },
        'windows': [{'token': WINDOW, 'generation': '13',
                     'layout_revision': '17'}],
    }


class FakeProvider:
    def __init__(self, view=None):
        self.view = make_view() if view is None else view
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.view


def request(op='start_recording', request_id='synthetic-request'):
    body = {'request_id': request_id, 'op': op, 'capsule_id': CAPSULE,
            'authority_epoch': EPOCH, 'expected_revision': REVISION}
    if op == 'bind_window':
        body['window'] = {'token': WINDOW, 'generation': '13',
                          'layout_revision': '17', 'role': 'game'}
    return body


class ProposalQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = FakeProvider()
        self.queue = proposals.ProposalQueue(self.provider, clock_ns=self.clock)

    def assert_reply(self, reply, state, error, *, request_id=_DEFAULT_REQUEST):
        if request_id is _DEFAULT_REQUEST:
            request_id = (None if (state, error) == ('rejected', 'invalid_proposal')
                          else 'synthetic-request')
        self.assertIs(type(reply), dict)
        self.assertEqual(set(reply), REPLY_KEYS)
        self.assertEqual(reply['schema'], 'cafe.control_proposal.v1')
        self.assertEqual((reply['state'], reply['error']), (state, error))
        self.assertIn(reply['error'], ERRORS)
        self.assertEqual(reply['request_id'], request_id)
        for key in ('proposal_id', 'request_id'):
            if reply[key] is not None:
                self.assertIs(type(reply[key]), str)
                self.assertIsNotNone(re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}', reply[key]))
        remaining = reply['expires_in_ms']
        if state == 'pending':
            self.assertIsNotNone(reply['proposal_id'])
            self.assertIs(type(remaining), int)
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 30_000)
        else:
            self.assertTrue(remaining is None or type(remaining) is int)
            if remaining is not None:
                self.assertGreaterEqual(remaining, 0)
                self.assertLessEqual(remaining, 30_000)
        self.assertNotIn(WINDOW, json.dumps(reply, ensure_ascii=False))
        return reply

    def propose(self, body=None):
        return self.queue.propose(request() if body is None else body)

    def consume(self, proposal_id, **kwargs):
        kwargs.setdefault('authority_epoch', EPOCH)
        kwargs.setdefault('revision', REVISION)
        return self.queue.consume_for_native_decision(proposal_id, **kwargs)

    def test_each_operation_creates_only_a_pending_proposal(self):
        for op in OPERATIONS:
            with self.subTest(op=op):
                body = request(op, 'synthetic-' + op)
                reply = self.assert_reply(self.propose(body), 'pending', None,
                                          request_id=body['request_id'])
                self.assertEqual(reply['expires_in_ms'], 30_000)
                self.assertNotIn('receipt', reply)
                self.assertFalse(callable(getattr(self.queue, 'approve', None)))
                self.assertFalse(callable(getattr(self.queue, 'confirm', None)))

    def test_window_is_required_only_for_binding(self):
        body = request('bind_window')
        del body['window']
        self.assert_reply(self.propose(body), 'rejected', 'invalid_proposal')
        for op in OPERATIONS[1:]:
            with self.subTest(op=op):
                for window in (None, {}, request('bind_window')['window']):
                    body = request(op)
                    body['window'] = window
                    self.assert_reply(self.propose(body), 'rejected',
                                      'invalid_proposal')

    def test_closed_request_and_window_shapes(self):
        for key in request():
            with self.subTest(missing=key):
                body = request()
                del body[key]
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal',
                                  request_id=None)
        for extra in ('extra', 'folder', 'slot', 'confirmed', 'approval_token',
                      'confirmation_token', 'expires_in_ms'):
            with self.subTest(extra=extra):
                body = request()
                body[extra] = True
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal')
        for key in request('bind_window')['window']:
            with self.subTest(missing_window=key):
                body = request('bind_window')
                del body['window'][key]
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal')
        body = request('bind_window')
        body['window']['title'] = 'synthetic'
        self.assert_reply(self.propose(body), 'rejected', 'invalid_proposal')

    def test_non_objects_and_invalid_identifiers_are_never_echoed(self):
        for body in (None, [], True, False, 1, 'synthetic'):
            with self.subTest(input_type=type(body).__name__):
                reply = self.queue.propose(body)
                self.assert_reply(reply, 'rejected', 'invalid_proposal',
                                  request_id=None)
                self.assertIsNone(reply['proposal_id'])
        for key in ('request_id', 'capsule_id'):
            for value in BAD_IDENTIFIERS:
                with self.subTest(field=key, value_type=type(value).__name__):
                    body = request()
                    body[key] = value
                    reply = self.assert_reply(self.propose(body), 'rejected',
                                              'invalid_proposal',
                                              request_id=None)
                    self.assertIsNone(reply['proposal_id'])

    def test_mistyped_operations_and_roles_fail_closed(self):
        for value in (None, True, 1, [], {}, 'choose-folder', 'approve',
                      'set_folder', 'START_RECORDING'):
            with self.subTest(operation_type=type(value).__name__):
                body = request()
                body['op'] = value
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal')
        for value in (None, True, 1, [], {}, 'Game', 'synthetic'):
            with self.subTest(role_type=type(value).__name__):
                body = request('bind_window')
                body['window']['role'] = value
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal')

    def test_all_request_clocks_require_canonical_u64_strings(self):
        for field in ('authority_epoch', 'expected_revision', 'generation',
                      'layout_revision'):
            for value in BAD_CLOCKS:
                with self.subTest(field=field, value_type=type(value).__name__):
                    body = request('bind_window')
                    target = body if field in body else body['window']
                    target[field] = value
                    self.assert_reply(self.propose(body), 'rejected',
                                      'invalid_proposal')

    def test_zero_and_u64_maximum_clocks_are_preserved(self):
        for value in ('0', '18446744073709551615'):
            with self.subTest(clock=value):
                view = make_view()
                view['authority_epoch'] = view['revision'] = value
                capsule = view['capsules'][CAPSULE]
                capsule['generation'] = value
                for feed in capsule['feeds'].values():
                    feed['generation'] = value
                view['windows'][0]['generation'] = value
                view['windows'][0]['layout_revision'] = value
                queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
                body = request('bind_window')
                body['authority_epoch'] = body['expected_revision'] = value
                body['window']['generation'] = value
                body['window']['layout_revision'] = value
                reply = self.assert_reply(queue.propose(body), 'pending', None)
                bound = queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=value, revision=value)
                self.assertIsNotNone(bound)
                self.assert_reply(queue.inspect(reply['proposal_id']), 'rejected',
                                  'proposal_consumed')
                self.assertEqual(body['authority_epoch'], value)
                self.assertEqual(body['window']['generation'], value)

    def test_window_values_are_opaque_utf8_not_control_identifiers(self):
        values = (' ', '!synthetic window?', '\u00e9' * 64,
                  '\U0001f680' * 32, 's' * 128)
        for value in values:
            with self.subTest(byte_length=len(value.encode('utf-8'))):
                view = make_view()
                view['windows'][0]['token'] = value
                queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
                body = request('bind_window')
                body['window']['token'] = value
                self.assert_reply(queue.propose(body), 'pending', None)
        for value in (None, True, 1, [], {}, '', 's' * 129, '\u00e9' * 65,
                      'synthetic\0window', '\ud800'):
            with self.subTest(value_type=type(value).__name__):
                body = request('bind_window')
                body['window']['token'] = value
                self.assert_reply(self.propose(body), 'rejected',
                                  'invalid_proposal')

    def test_replay_is_idempotent_and_does_not_extend_deadline(self):
        body = request('bind_window')
        first = self.assert_reply(self.propose(body), 'pending', None)
        self.assertEqual(self.propose(copy.deepcopy(body)), first)
        reordered = dict(reversed(tuple(body.items())))
        reordered['window'] = dict(reversed(tuple(body['window'].items())))
        self.assertEqual(self.propose(reordered), first)
        self.clock.now += 1_000_000
        repeated = self.assert_reply(self.propose(body), 'pending', None)
        self.assertEqual(repeated['proposal_id'], first['proposal_id'])
        self.assertEqual(repeated['expires_in_ms'], 29_999)

    def test_any_changed_valid_body_conflicts_with_stored_request(self):
        body = request('bind_window')
        first = self.propose(body)
        changes = [({'op': 'stop_recording'}, True),
                   ({'capsule_id': 'synthetic-other-capsule'}, False),
                   ({'authority_epoch': '8'}, False),
                   ({'expected_revision': '12'}, False)]
        for updates, remove_window in changes:
            with self.subTest(changed=tuple(updates)):
                changed = copy.deepcopy(body)
                changed.update(updates)
                if remove_window:
                    del changed['window']
                self.assert_reply(self.propose(changed), 'rejected',
                                  'request_id_conflict')
        for field, value in (('token', 'synthetic-other-window'),
                             ('generation', '14'), ('layout_revision', '18'),
                             ('role', 'camera')):
            with self.subTest(changed_window=field):
                changed = copy.deepcopy(body)
                changed['window'][field] = value
                self.assert_reply(self.propose(changed), 'rejected',
                                  'request_id_conflict')
        self.assertEqual(self.queue.inspect(first['proposal_id']), first)
        self.assertIsNotNone(self.consume(first['proposal_id']))

    def test_malformed_replay_echoes_only_stored_id_without_corrupting_entry(self):
        first = self.propose()
        malformed = request()
        malformed['extra'] = True
        reply = self.assert_reply(self.propose(malformed), 'rejected',
                                  'invalid_proposal',
                                  request_id='synthetic-request')
        self.assertEqual(reply['proposal_id'], first['proposal_id'])
        self.assertEqual(self.queue.inspect(first['proposal_id']), first)
        self.assertIsNotNone(self.consume(first['proposal_id']))

    def test_known_inspect_and_reject_are_terminal_without_consumption(self):
        first = self.propose()
        identity = first['proposal_id']
        self.assertEqual(self.queue.inspect(identity), first)
        rejected = self.assert_reply(self.queue.reject(identity), 'rejected',
                                     'confirmation_required')
        self.assertEqual(rejected['proposal_id'], identity)
        self.assertEqual(self.queue.inspect(identity), rejected)
        self.assertEqual(self.queue.reject(identity), rejected)
        self.assertEqual(self.propose(), rejected)
        self.assertIsNone(self.consume(identity))

    def test_unknown_or_invalid_proposal_identifiers_never_create_state(self):
        values = ('synthetic-unknown', None, True, 1, '', '-synthetic', [], {})
        for value in values:
            for method in (self.queue.inspect, self.queue.reject):
                with self.subTest(method=method.__name__, value_type=type(value).__name__):
                    reply = self.assert_reply(method(value), 'rejected',
                                              'invalid_proposal', request_id=None)
                    self.assertIsNone(reply['proposal_id'])
            self.assertIsNone(self.consume(value))
        self.assert_reply(self.propose(), 'pending', None)

    def test_expiry_at_exact_deadline_never_rounds_a_nanosecond_away(self):
        first = self.propose()
        identity = first['proposal_id']
        self.clock.now = NOW + TTL_NS - 1
        almost = self.assert_reply(self.queue.inspect(identity), 'pending', None)
        self.assertEqual(almost['expires_in_ms'], 1)
        self.clock.now += 1
        expired = self.assert_reply(self.queue.inspect(identity), 'expired',
                                    'proposal_expired')
        self.assertEqual(expired['proposal_id'], identity)
        self.assertEqual(self.propose(), expired)
        self.assertEqual(self.queue.reject(identity), expired)
        self.assertIsNone(self.consume(identity))

    def test_consumption_rechecks_native_clock_without_a_ui_inspect(self):
        first = self.propose()
        self.clock.now += TTL_NS + 1
        self.assertIsNone(self.consume(first['proposal_id']))
        self.assert_reply(self.queue.inspect(first['proposal_id']), 'expired',
                          'proposal_expired')

    def test_pending_capacity_is_64_and_replay_works_while_full(self):
        replies = []
        for index in range(64):
            body = request(request_id='synthetic-pending-' + str(index))
            replies.append(self.assert_reply(self.propose(body), 'pending', None,
                                             request_id=body['request_id']))
        self.assertEqual(len({r['proposal_id'] for r in replies}), 64)
        overflow = self.propose(request(request_id='synthetic-overflow'))
        self.assert_reply(overflow, 'rejected', 'proposal_capacity',
                          request_id=None)
        self.assertIsNone(overflow['proposal_id'])
        self.assertEqual(self.propose(request(request_id='synthetic-pending-0')),
                         replies[0])
        self.queue.reject(replies[0]['proposal_id'])
        self.assert_reply(self.propose(request(request_id='synthetic-overflow')),
                          'pending', None, request_id='synthetic-overflow')

    def test_expiration_releases_pending_capacity_without_reusing_old_ids(self):
        queue = proposals.ProposalQueue(self.provider, clock_ns=self.clock,
                                        max_pending=1)
        first = queue.propose(request())
        self.clock.now += TTL_NS
        second = self.assert_reply(queue.propose(request(
            request_id='synthetic-new')), 'pending', None,
            request_id='synthetic-new')
        self.assertNotEqual(first['proposal_id'], second['proposal_id'])
        self.assert_reply(queue.propose(request()), 'expired', 'proposal_expired')

    def test_seen_bound_retains_all_1024_consumed_tombstones(self):
        first = None
        for index in range(1024):
            body = request(request_id='synthetic-seen-' + str(index))
            reply = self.assert_reply(self.propose(body), 'pending', None,
                                      request_id=body['request_id'])
            if first is None:
                first = reply
            self.assertIsNotNone(self.consume(reply['proposal_id']))
        self.assert_reply(self.propose(request(request_id='synthetic-seen-1024')),
                          'rejected', 'proposal_capacity',
                          request_id=None)
        self.assert_reply(self.queue.inspect(first['proposal_id']), 'rejected',
                          'proposal_consumed', request_id='synthetic-seen-0')
        self.assert_reply(self.propose(request(request_id='synthetic-seen-0')),
                          'rejected', 'proposal_consumed',
                          request_id='synthetic-seen-0')
        altered = request('stop_recording', 'synthetic-seen-0')
        self.assert_reply(self.propose(altered), 'rejected', 'request_id_conflict',
                          request_id='synthetic-seen-0')
        self.assertIsNone(self.consume(first['proposal_id']))

    def test_constructor_rejects_boolean_or_excessive_limits(self):
        for limit in (True, False, -1, 0, 65, 1.0, '64', None):
            with self.subTest(limit_type=type(limit).__name__):
                with self.assertRaises(ValueError):
                    proposals.ProposalQueue(self.provider, clock_ns=self.clock,
                                            max_pending=limit)
        for clock in (None, True, 1):
            with self.subTest(clock_type=type(clock).__name__):
                with self.assertRaises(ValueError):
                    proposals.ProposalQueue(self.provider, clock_ns=clock)

    def test_provider_absence_none_exception_or_wrong_signature_fails_closed(self):
        def unavailable():
            raise RuntimeError('synthetic-exception-must-not-escape')

        for provider in (None, lambda: None, unavailable, True,
                         lambda kind, arguments: make_view()):
            with self.subTest(provider_type=type(provider).__name__):
                queue = proposals.ProposalQueue(provider, clock_ns=self.clock)
                reply = self.assert_reply(queue.propose(request()), 'blocked',
                                          'provider_unavailable')
                self.assertNotIn('synthetic-exception-must-not-escape', str(reply))

    def test_provider_shape_is_closed_at_every_level(self):
        mutations = [
            lambda v: v.update(extra=True),
            lambda v: v.pop('revision'),
            lambda v: v.update(capsules=[]),
            lambda v: v.update(windows=()),
            lambda v: v['capsules'][CAPSULE].update(person_label='synthetic'),
            lambda v: v['capsules'][CAPSULE].pop('generation'),
            lambda v: v['capsules'][CAPSULE].update(capsule_id='synthetic-other'),
            lambda v: v['capsules'][CAPSULE].update(feeds=[]),
            lambda v: v['capsules'][CAPSULE]['feeds']['game'].update(extra=True),
            lambda v: v['capsules'][CAPSULE]['feeds']['game'].update(role='camera'),
            lambda v: v['capsules'][CAPSULE]['feeds']['game'].pop('generation'),
            lambda v: v['capsules'][CAPSULE]['feeds'].update(other={}),
            lambda v: v['windows'][0].update(title='synthetic'),
            lambda v: v['windows'][0].pop('layout_revision'),
            lambda v: v['windows'][0].update(token='synthetic\0window'),
            lambda v: v['windows'][0].update(token='s' * 129),
            lambda v: v['windows'][0].update(token='\ud800'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                view = make_view()
                mutate(view)
                queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
                self.assert_reply(queue.propose(request()), 'blocked',
                                  'provider_unavailable')
        for view in (None, [], True, 'synthetic'):
            queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
            self.assert_reply(queue.propose(request()), 'blocked',
                              'provider_unavailable')

    def test_provider_failure_is_remembered_and_never_resurrected(self):
        self.provider.view = None
        denied = self.assert_reply(self.propose(), 'blocked',
                                   'provider_unavailable')
        self.assertIsNone(denied['proposal_id'])
        self.provider.view = make_view()
        self.assertEqual(self.propose(), denied)
        self.assert_reply(self.propose(request('stop_recording')), 'rejected',
                          'request_id_conflict')
        self.assert_reply(self.propose(request(request_id='synthetic-new-request')),
                          'pending', None, request_id='synthetic-new-request')

    def test_native_snapshot_counts_are_bounded_without_a_three_window_limit(self):
        view = make_view()
        view['windows'] = [
            {'token': 'synthetic-enumerated-' + str(index), 'generation': '13',
             'layout_revision': '17'} for index in range(4096)]
        body = request('bind_window')
        body['window']['token'] = view['windows'][-1]['token']
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        reply = self.assert_reply(queue.propose(body), 'pending', None)
        self.assertIsNotNone(queue.consume_for_native_decision(
            reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
        view['windows'].append({'token': 'synthetic-overflow-window',
                                'generation': '13', 'layout_revision': '17'})
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'blocked',
                          'provider_unavailable')
        view = make_view()
        view['capsules']['synthetic-second'] = {
            'capsule_id': 'synthetic-second', 'generation': '1', 'feeds': {}}
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'pending', None)
        view['capsules']['synthetic-third'] = {
            'capsule_id': 'synthetic-third', 'generation': '1', 'feeds': {}}
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'blocked',
                          'provider_unavailable')

    def test_provider_clocks_are_validated_everywhere(self):
        targets = (
            lambda v: (v, 'authority_epoch'),
            lambda v: (v, 'revision'),
            lambda v: (v['capsules'][CAPSULE], 'generation'),
            lambda v: (v['capsules'][CAPSULE]['feeds']['game'], 'generation'),
            lambda v: (v['windows'][0], 'generation'),
            lambda v: (v['windows'][0], 'layout_revision'),
        )
        for index, target in enumerate(targets):
            for value in BAD_CLOCKS:
                with self.subTest(target=index, value_type=type(value).__name__):
                    view = make_view()
                    parent, key = target(view)
                    parent[key] = value
                    queue = proposals.ProposalQueue(lambda: view,
                                                    clock_ns=self.clock)
                    self.assert_reply(queue.propose(request()), 'blocked',
                                      'provider_unavailable')

    def test_duplicate_window_or_feed_identity_within_its_domain_is_invalid(self):
        view = make_view()
        view['windows'].append(copy.deepcopy(view['windows'][0]))
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'blocked',
                          'provider_unavailable')
        view = make_view()
        feeds = view['capsules'][CAPSULE]['feeds']
        feeds['camera']['feed_id'] = feeds['game']['feed_id']
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'blocked',
                          'provider_unavailable')
        view = make_view()
        other = copy.deepcopy(view['capsules'][CAPSULE])
        other['capsule_id'] = 'synthetic-second'
        view['capsules']['synthetic-second'] = other
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        self.assert_reply(queue.propose(request()), 'blocked',
                          'provider_unavailable')

    def test_identical_text_in_separate_identifier_domains_is_valid(self):
        same = 'synthetic-shared'
        capsule = {'capsule_id': same, 'generation': '3', 'feeds': {
            'game': {'feed_id': same, 'role': 'game', 'generation': '5'}}}
        view = {'authority_epoch': EPOCH, 'revision': REVISION,
                'capsules': {same: capsule}, 'windows': [
                    {'token': same, 'generation': '13', 'layout_revision': '17'}]}
        body = request('bind_window', same)
        body['capsule_id'] = same
        body['window']['token'] = same
        queue = proposals.ProposalQueue(lambda: view, clock_ns=self.clock)
        reply = self.assert_reply(queue.propose(body), 'pending', None,
                                  request_id=same)
        self.assertIsNotNone(queue.consume_for_native_decision(
            reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))

    def test_missing_capsule_and_missing_window_block_without_slot_resolution(self):
        body = request()
        body['capsule_id'] = 'slot-1'
        self.assert_reply(self.propose(body), 'blocked', 'confirmation_required')
        self.provider.view['windows'] = []
        body = request('bind_window', 'synthetic-missing-window')
        self.assert_reply(self.propose(body), 'blocked', 'confirmation_required',
                          request_id='synthetic-missing-window')

    def test_stale_epoch_revision_or_chosen_window_blocks_at_proposal(self):
        for field, value in (('authority_epoch', '8'),
                             ('expected_revision', '12'),
                             ('generation', '14'), ('layout_revision', '18')):
            with self.subTest(field=field):
                body = request('bind_window', 'synthetic-stale-' + field)
                target = body if field in body else body['window']
                target[field] = value
                self.assert_reply(self.propose(body), 'blocked', 'stale_revision',
                                  request_id=body['request_id'])

    def test_consume_hands_off_once_without_claiming_a_commit(self):
        for op in OPERATIONS:
            with self.subTest(op=op):
                body = request(op, 'synthetic-consume-' + op)
                reply = self.propose(body)
                bound = self.consume(reply['proposal_id'])
                self.assertIsNotNone(bound)
                self.assertNotIsInstance(bound, dict)
                with self.assertRaises(TypeError):
                    json.dumps(bound)
                self.assertIsNone(self.consume(reply['proposal_id']))
                self.assert_reply(self.queue.inspect(reply['proposal_id']),
                                  'rejected', 'proposal_consumed',
                                  request_id=body['request_id'])
                self.assert_reply(self.propose(body), 'rejected',
                                  'proposal_consumed', request_id=body['request_id'])
                self.assert_reply(self.queue.reject(reply['proposal_id']),
                                  'rejected', 'proposal_consumed',
                                  request_id=body['request_id'])

    def test_private_handoff_is_immutable_and_hides_identity_in_repr(self):
        reply = self.propose(request('bind_window'))
        bound = self.consume(reply['proposal_id'])
        self.assertIsNotNone(bound)
        for value, field, replacement in (
                (bound, 'op', 'stop_recording'),
                (bound.capsule, 'generation', '4'),
                (bound.capsule.feeds[0][1], 'generation', '6'),
                (bound.window, 'role', 'camera'),
                (bound.window.identity, 'generation', '14')):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(value, field, replacement)
        self.provider.view['capsules'][CAPSULE]['generation'] = '4'
        self.provider.view['windows'][0]['generation'] = '14'
        self.assertEqual(bound.capsule.generation, '3')
        self.assertEqual(bound.window.identity.generation, '13')
        for value in (bound, bound.capsule, bound.window, bound.window.identity):
            self.assertNotIn(CAPSULE, repr(value))
            self.assertNotIn(WINDOW, repr(value))
        self.assertFalse(hasattr(bound, 'receipt'))
        self.assertFalse(hasattr(bound, 'committed'))

    def test_clock_is_rechecked_after_a_provider_crosses_the_deadline(self):
        calls = 0
        view = make_view()

        def slow_provider():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.clock.now = NOW + TTL_NS
            return view

        queue = proposals.ProposalQueue(slow_provider, clock_ns=self.clock)
        reply = queue.propose(request())
        self.assertIsNone(queue.consume_for_native_decision(
            reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
        self.assert_reply(queue.inspect(reply['proposal_id']), 'expired',
                          'proposal_expired')

    def test_a_slow_initial_provider_cannot_restart_the_proposal_ttl(self):
        view = make_view()

        def slow_provider():
            self.clock.now += TTL_NS
            return view

        queue = proposals.ProposalQueue(slow_provider, clock_ns=self.clock)
        reply = self.assert_reply(queue.propose(request()), 'expired',
                                  'proposal_expired')
        self.assertIsNone(reply['proposal_id'])
        self.assertEqual(queue.propose(request()), reply)

    def test_clock_fault_or_regression_blocks_without_resurrection(self):
        values = (True, -1, 1.0, 'synthetic-clock', NOW - 1,
                  ValueError('synthetic-clock-failure'))
        for invalid in values:
            with self.subTest(clock_type=type(invalid).__name__):
                current = [NOW]

                def clock():
                    if isinstance(current[0], ValueError):
                        raise current[0]
                    return current[0]

                queue = proposals.ProposalQueue(self.provider, clock_ns=clock)
                reply = queue.propose(request())
                current[0] = invalid
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
                denied = self.assert_reply(queue.inspect(reply['proposal_id']),
                                           'blocked', 'provider_unavailable')
                self.assertNotIn('synthetic-clock-failure', str(denied))
                current[0] = NOW + 1
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
                self.assertEqual(queue.inspect(reply['proposal_id']), denied)

    def test_consume_one_nanosecond_before_deadline_is_still_pending(self):
        reply = self.propose()
        self.clock.now = NOW + TTL_NS - 1
        self.assertIsNotNone(self.consume(reply['proposal_id']))
        self.clock.now = NOW + TTL_NS
        self.assert_reply(self.queue.inspect(reply['proposal_id']), 'rejected',
                          'proposal_consumed')

    def test_reentrant_provider_cannot_consume_the_same_proposal_twice(self):
        nested = []
        identity = None
        view = make_view()

        def reentrant_provider():
            if identity is not None:
                nested.append(queue.consume_for_native_decision(
                    identity, authority_epoch=EPOCH, revision=REVISION))
            return view

        queue = proposals.ProposalQueue(reentrant_provider, clock_ns=self.clock)
        identity = queue.propose(request())['proposal_id']
        outer = queue.consume_for_native_decision(
            identity, authority_epoch=EPOCH, revision=REVISION)
        self.assertIsNone(outer)
        self.assertEqual(nested, [None])
        self.assert_reply(queue.inspect(identity), 'blocked',
                          'confirmation_required')
        self.assertIsNone(queue.consume_for_native_decision(
            identity, authority_epoch=EPOCH, revision=REVISION))

    def test_reentrant_reject_during_provider_call_cannot_be_overwritten(self):
        nested = []
        identity = None
        view = make_view()

        def rejecting_provider():
            if identity is not None:
                nested.append(queue.reject(identity))
            return view

        queue = proposals.ProposalQueue(rejecting_provider, clock_ns=self.clock)
        identity = queue.propose(request())['proposal_id']
        self.assertIsNone(queue.consume_for_native_decision(
            identity, authority_epoch=EPOCH, revision=REVISION))
        self.assertEqual(len(nested), 1)
        self.assert_reply(nested[0], 'blocked', 'confirmation_required')
        self.assert_reply(queue.inspect(identity), 'blocked',
                          'confirmation_required')

    def test_bad_consumption_clocks_cannot_be_retried_with_fresh_values(self):
        for field in ('authority_epoch', 'revision'):
            for value in (True, 1, '01', '18446744073709551616'):
                with self.subTest(field=field, value_type=type(value).__name__):
                    queue = proposals.ProposalQueue(self.provider,
                                                    clock_ns=self.clock)
                    reply = queue.propose(request())
                    kwargs = {'authority_epoch': EPOCH, 'revision': REVISION}
                    kwargs[field] = value
                    self.assertIsNone(queue.consume_for_native_decision(
                        reply['proposal_id'], **kwargs))
                    self.assert_reply(queue.inspect(reply['proposal_id']),
                                      'blocked', 'invalid_proposal')
                    self.assertIsNone(queue.consume_for_native_decision(
                        reply['proposal_id'], authority_epoch=EPOCH,
                        revision=REVISION))

    def test_stale_native_decision_clocks_are_terminal(self):
        for field, value in (('authority_epoch', '8'), ('revision', '12')):
            with self.subTest(field=field):
                queue = proposals.ProposalQueue(self.provider, clock_ns=self.clock)
                reply = queue.propose(request())
                kwargs = {'authority_epoch': EPOCH, 'revision': REVISION}
                kwargs[field] = value
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], **kwargs))
                self.assert_reply(queue.inspect(reply['proposal_id']), 'blocked',
                                  'stale_revision')
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))

    def test_any_bound_identity_change_invalidates_consumption(self):
        mutations = [
            lambda v: v.update(authority_epoch='8'),
            lambda v: v.update(revision='12'),
            lambda v: v['capsules'][CAPSULE].update(generation='4'),
            lambda v: v['capsules'][CAPSULE]['feeds']['game'].update(generation='6'),
            lambda v: v['capsules'][CAPSULE]['feeds']['game'].update(
                feed_id='synthetic-replaced-game'),
            lambda v: v['capsules'][CAPSULE]['feeds']['camera'].update(generation='10'),
            lambda v: v['capsules'][CAPSULE]['feeds'].pop('camera'),
            lambda v: v['windows'][0].update(generation='14'),
            lambda v: v['windows'][0].update(layout_revision='18'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(change=index):
                provider = FakeProvider()
                queue = proposals.ProposalQueue(provider, clock_ns=self.clock)
                reply = queue.propose(request('bind_window'))
                mutate(provider.view)
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
                self.assert_reply(queue.inspect(reply['proposal_id']), 'blocked',
                                  'stale_revision')
                provider.view = make_view()
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))

    def test_changed_feed_role_is_not_silently_rebound(self):
        reply = self.propose(request('bind_window'))
        feeds = self.provider.view['capsules'][CAPSULE]['feeds']
        game, camera = feeds['game'], feeds['camera']
        game['role'], camera['role'] = 'camera', 'game'
        feeds['game'], feeds['camera'] = camera, game
        self.assertIsNone(self.consume(reply['proposal_id']))
        self.assert_reply(self.queue.inspect(reply['proposal_id']), 'blocked',
                          'stale_revision')

    def test_added_feed_invalidates_previously_empty_role(self):
        del self.provider.view['capsules'][CAPSULE]['feeds']['camera']
        reply = self.propose()
        self.provider.view['capsules'][CAPSULE]['feeds']['camera'] = {
            'feed_id': 'synthetic-added-feed', 'role': 'camera', 'generation': '1'}
        self.assertIsNone(self.consume(reply['proposal_id']))
        self.assert_reply(self.queue.inspect(reply['proposal_id']), 'blocked',
                          'stale_revision')

    def test_input_and_reply_mutation_cannot_modify_stored_body(self):
        body = request('bind_window')
        original = copy.deepcopy(body)
        reply = self.propose(body)
        identity = reply['proposal_id']
        body['window']['role'] = 'camera'
        body['capsule_id'] = 'synthetic-other'
        reply['state'] = 'committed'
        reply['proposal_id'] = 'synthetic-forged'
        stored = self.assert_reply(self.queue.inspect(identity), 'pending', None)
        self.assertEqual(stored['proposal_id'], identity)
        self.assertEqual(self.propose(original), stored)
        self.assert_reply(self.propose(body), 'rejected', 'request_id_conflict')
        self.assertIsNotNone(self.consume(identity))

    def test_provider_loss_or_malformed_view_at_consumption_is_terminal(self):
        for unavailable in (None, {}, {'authority_epoch': EPOCH}):
            with self.subTest(provider_type=type(unavailable).__name__):
                provider = FakeProvider()
                queue = proposals.ProposalQueue(provider, clock_ns=self.clock)
                reply = queue.propose(request())
                provider.view = unavailable
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))
                self.assert_reply(queue.inspect(reply['proposal_id']), 'blocked',
                                  'provider_unavailable')
                provider.view = make_view()
                self.assertIsNone(queue.consume_for_native_decision(
                    reply['proposal_id'], authority_epoch=EPOCH, revision=REVISION))

    def test_partial_window_page_does_not_allow_a_disappeared_binding(self):
        reply = self.propose(request('bind_window'))
        self.provider.view['windows'] = [{'token': 'synthetic-other-window',
                                          'generation': '13',
                                          'layout_revision': '17'}]
        self.assertIsNone(self.consume(reply['proposal_id']))
        self.assert_reply(self.queue.inspect(reply['proposal_id']), 'blocked',
                          'confirmation_required')

    def test_no_lifecycle_path_performs_effects_or_reads_a_wall_clock(self):
        traps = ('builtins.open', 'builtins.print', 'os.open', 'os.write',
                 'os.system', 'os.urandom', 'socket.socket',
                 'socket.create_connection', 'subprocess.Popen',
                 'threading.Thread', 'time.time', 'time.time_ns',
                 'time.monotonic', 'time.monotonic_ns', 'time.perf_counter',
                 'time.perf_counter_ns')
        with ExitStack() as stack:
            mocks = [stack.enter_context(mock.patch(
                name, side_effect=AssertionError('forbidden_external_effect')))
                     for name in traps]
            for op in OPERATIONS:
                body = request(op, 'synthetic-effect-' + op)
                reply = self.assert_reply(self.propose(body), 'pending', None,
                                          request_id=body['request_id'])
                self.assertEqual(self.propose(body), reply)
                self.queue.inspect(reply['proposal_id'])
                self.queue.reject(reply['proposal_id'])
                self.assertIsNone(self.consume(reply['proposal_id']))
            missing = proposals.ProposalQueue(clock_ns=self.clock)
            self.assert_reply(missing.propose(request()), 'blocked',
                              'provider_unavailable')
            self.assert_reply(self.queue.propose(None), 'rejected',
                              'invalid_proposal', request_id=None)
            limited = proposals.ProposalQueue(self.provider, clock_ns=self.clock,
                                              max_pending=1)
            limited.propose(request())
            self.assert_reply(limited.propose(request(
                request_id='synthetic-capacity')), 'rejected', 'proposal_capacity',
                request_id=None)
            stale = request(request_id='synthetic-no-effect-stale')
            stale['expected_revision'] = '12'
            self.assert_reply(self.propose(stale), 'blocked', 'stale_revision',
                              request_id='synthetic-no-effect-stale')
            pending = self.propose()
            changed = request('stop_recording')
            self.assert_reply(self.propose(changed), 'rejected',
                              'request_id_conflict')
            self.assertIsNotNone(self.consume(pending['proposal_id']))
            expiring = self.propose(request(request_id='synthetic-expiring'))
            self.clock.now += TTL_NS
            self.assert_reply(self.queue.inspect(expiring['proposal_id']),
                              'expired', 'proposal_expired',
                              request_id='synthetic-expiring')
            for called in mocks:
                called.assert_not_called()

    def test_module_cannot_import_recording_obs_or_broker_effect_hosts(self):
        # Together with the runtime syscall traps, this gate keeps the pure
        # queue detached from effect hosts, including lazy imports in branches.
        syntax = ast.parse(Path(proposals.__file__).read_text(encoding='utf-8'))
        pure_dependencies = {'collections', 'dataclasses', 'enum', 'functools',
                             'hashlib', 'json', 're', 'types', 'typing'}
        for node in ast.walk(syntax):
            if isinstance(node, ast.Import):
                for name in node.names:
                    self.assertIn(name.name.split('.')[0], pure_dependencies)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn((node.module or '').split('.')[0], pure_dependencies)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {'__import__', 'eval', 'exec',
                                               'compile', 'open'})


if __name__ == '__main__':
    unittest.main()
