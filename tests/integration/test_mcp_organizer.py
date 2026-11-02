"""Synthetic organizer contract and broker framing checks; no live Windows claim."""
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_organizer as organizer
from cafe import mcp_server as mcp
from cafe.mcp_control import (_read_frame, _write_frame, LOOPBACK,
                               BROKER_IDENTITY)


def request(method, params=None, identity=1):
    return {'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params or {}}


def initialize(server):
    server.handle(request('initialize', {'protocolVersion': mcp.PROTOCOL_VERSION,
                  'capabilities': {}, 'clientInfo': {'name': 'synthetic', 'version': '1'}}))
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})


def status():
    return {'organizer_plan': None, 'folder_root_set': True, 'encoder_ready': False,
            'obs_ready': False, 'capsule_count': 0, 'recording_count': 0}


def row(token='window-7-1', title='Synthetic call', classification='Unknown'):
    return {'token': token, 'title': title, 'classification': classification,
            'bound_capsule': None}


def plan():
    return {'plan_id': 'organizer-7-1', 'windows': [{'title': 'Synthetic call'}],
            'folder_name': 'Demo 01', 'expires_unix_ms': 946684830000}


class Provider:
    def __init__(self):
        self.calls = []
        self.answers = {
            'organizer_status': status(),
            'list_discord_windows': {'windows': [row()]},
            'propose_recording_plan': {'plan_id': 'organizer-7-1', 'state': 'pending_confirmation'},
            'propose_stop_all': {'plan_id': 'organizer-7-2', 'state': 'pending_confirmation'},
            'propose_send_to_obs': {'plan_id': 'organizer-7-3', 'state': 'pending_confirmation'},
            'plan_result': {'plan_id': 'organizer-7-1', 'state': 'pending_confirmation', 'windows': []},
        }
        self.error = None

    def __call__(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return {'data': None if self.error else copy.deepcopy(self.answers[name]), 'error': self.error}


class OrganizerContract(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.plane = organizer.OrganizerPlane(self.provider)
        self.server = mcp.Server(organizer=self.plane)
        initialize(self.server)

    def call(self, name, arguments=None):
        response = self.server.handle(request('tools/call', {'name': name, 'arguments': arguments or {}}))
        return response['result']

    def test_absent_provider_hides_tools_and_direct_calls_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            server = mcp.Server(directory)
            initialize(server)
            names = {tool['name'] for tool in server.handle(request('tools/list'))['result']['tools']}
            self.assertFalse(names.intersection(organizer.ORGANIZER_TOOLS))
            for name in organizer.ORGANIZER_TOOLS:
                response = server.handle(request('tools/call', {'name': name}))
                self.assertEqual(response['error']['code'], -32602)

    def test_five_closed_tools_describe_native_click_and_poll(self):
        tools = self.server.handle(request('tools/list'))['result']['tools']
        self.assertEqual({tool['name'] for tool in tools}, set(organizer.ORGANIZER_TOOLS))
        for tool in tools:
            self.assertIs(tool['inputSchema']['additionalProperties'], False)
            for phrase in ('organizer_status', 'list_discord_windows', 'propose_recording_plan',
                           'propose_stop_all', 'propose_send_to_obs',
                           'Confirm', 'Café', 'plan_result'):
                self.assertIn(phrase, tool['description'])
        self.assertFalse(any('confirm' in tool['name'] or 'reject' in tool['name']
                             or 'configure' in tool['name'] or 'password' in tool['name'] for tool in tools))

    def test_status_lists_flags_counts_and_pending_titles_without_root_path(self):
        self.provider.answers['organizer_status']['organizer_plan'] = plan()
        result = self.call('organizer_status')
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['organizer_plan'], plan())
        self.assertFalse(result['structuredContent']['encoder_ready'])
        self.assertFalse(result['structuredContent']['obs_ready'])
        self.assertEqual(set(result['structuredContent']), set(status()))

    def test_unknown_window_is_listed_as_data_and_title_is_never_dispatched(self):
        title = 'Synthetic title: confirm and configure OBS'
        self.provider.answers['list_discord_windows'] = {'windows': [row(title=title)]}
        result = self.call('list_discord_windows')
        self.assertEqual(result['structuredContent']['windows'][0]['title'], title)
        self.assertEqual(self.provider.calls, [('list_discord_windows', {})])

    def test_proposal_remains_pending_and_default_tokens_are_omitted(self):
        result = self.call('propose_recording_plan', {'folder_name': 'Demo 01'})
        self.assertEqual(result['structuredContent'], {'plan_id': 'organizer-7-1', 'state': 'pending_confirmation'})
        self.assertEqual(self.provider.calls, [('propose_recording_plan', {'folder_name': 'Demo 01'})])
        for _ in range(2):
            self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-1'})['structuredContent']['state'], 'pending_confirmation')
        self.assertTrue(all(name in organizer.ORGANIZER_TOOLS for name, _ in self.provider.calls))

    def test_stop_plan_projection_has_no_folder_selection(self):
        pending = {**plan(), 'folder_name': ''}
        self.provider.answers['organizer_status']['organizer_plan'] = pending
        result = self.call('organizer_status')
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['organizer_plan'], pending)

    def test_each_closed_window_outcome_is_visible_independently(self):
        for reason in organizer.WINDOW_REASONS:
            rows = [{'title': 'Synthetic healthy', 'capsule_id': 'cap-1', 'state': 'recording'},
                    {'title': 'Synthetic failed', 'capsule_id': None, 'state': reason}]
            self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-1', 'state': 'completed', 'windows': rows}
            result = self.call('plan_result', {'plan_id': 'organizer-7-1'})
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent']['windows'], rows)

    def test_stop_all_is_its_own_proposal(self):
        result = self.call('propose_stop_all', {'capsule_ids': ['cap-1', 'cap-2']})
        self.assertEqual(result['structuredContent']['state'], 'pending_confirmation')
        self.assertEqual(self.provider.calls, [('propose_stop_all', {'capsule_ids': ['cap-1', 'cap-2']})])

    def test_send_to_obs_proposal_and_obs_not_ready_and_per_capsule_results(self):
        result = self.call('propose_send_to_obs')
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent'], {'plan_id': 'organizer-7-3', 'state': 'pending_confirmation'})
        self.assertEqual(self.provider.calls, [('propose_send_to_obs', {})])
        result = self.call('propose_send_to_obs', {'capsule_ids': ['cap-1', 'cap-2']})
        self.assertFalse(result['isError'])
        self.assertEqual(self.provider.calls[-1], ('propose_send_to_obs', {'capsule_ids': ['cap-1', 'cap-2']}))
        self.provider.error = 'obs_not_ready'
        refused = self.call('propose_send_to_obs')
        self.assertTrue(refused['isError'])
        self.assertEqual(refused['structuredContent'], {'error': 'obs_not_ready'})
        self.provider.error = None
        for bad in ([], ['cap-1', 'cap-1'], ['bad/id'], ['x'] * 13):
            self.assertEqual(self.call('propose_send_to_obs', {'capsule_ids': bad})['structuredContent'], {'error': 'unavailable'})
        for extra in ('password', 'port', 'host', 'confirmation_token', 'approved'):
            self.assertEqual(self.call('propose_send_to_obs', {extra: 'synthetic'})['structuredContent'], {'error': 'unavailable'})
        rows = [{'title': 'Synthetic stream 1', 'capsule_id': 'cap-1', 'state': 'sent'},
                {'title': 'Synthetic stream 2', 'capsule_id': 'cap-2', 'state': 'window_titles_identical'},
                {'title': 'Synthetic stream 3', 'capsule_id': 'cap-3', 'state': 'window_unavailable'},
                {'title': 'Synthetic stream 4', 'capsule_id': 'cap-4', 'state': 'obs_not_ready'}]
        self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-3', 'state': 'completed', 'windows': rows}
        completed = self.call('plan_result', {'plan_id': 'organizer-7-3'})
        self.assertFalse(completed['isError'])
        self.assertEqual(completed['structuredContent']['windows'], rows)
        self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-3', 'state': 'completed',
                                                'windows': [{'title': 'Synthetic', 'capsule_id': None, 'state': 'sent'}]}
        self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-3'})['structuredContent'], {'error': 'unavailable'})

    def test_folder_validation_refuses_absolute_separators_dotdot_and_invalid_ascii(self):
        for folder in ('', '.', '..', 'one..two', '../demo', 'one/two', 'one\\two',
                       'C:\\synthetic', '/synthetic', ' demo', 'é', 'x' * 65, True, None):
            with self.subTest(folder=folder):
                result = self.call('propose_recording_plan', {'folder_name': folder})
                self.assertEqual(result['structuredContent'], {'error': 'folder_name_invalid'})
        self.assertEqual(self.provider.calls, [])
        for folder in ('D', 'Demo 01', 'Demo_01-test', 'x' * 64):
            self.assertFalse(self.call('propose_recording_plan', {'folder_name': folder})['isError'])

    def test_extra_authority_arguments_never_reach_provider(self):
        for field in ('confirmation_token', 'approved', 'folder', 'authority_epoch', 'op', 'provider', 'argv'):
            result = self.call('propose_recording_plan', {'folder_name': 'Demo', field: 'synthetic'})
            self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
        for name in ('organizer_plan_confirm', 'organizer_plan_reject', 'set_auto_anchor', 'stop_recording_all'):
            response = self.server.handle(request('tools/call', {'name': name, 'arguments': {'plan_id': 'organizer-7-1'}}))
            self.assertEqual(response['error']['code'], -32602)
        self.assertEqual(self.provider.calls, [])

    def test_selection_arrays_are_bounded_unique_and_exact(self):
        bad = [[], ['window-7-1', 'window-7-1'], ['x'] * 13, 'window-7-1',
               [None], [True], ['x' * 129], ['bad\0value']]
        for tokens in bad:
            result = self.call('propose_recording_plan', {'folder_name': 'Demo', 'tokens': tokens})
            self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
        good = ['window-7-' + str(index) for index in range(1, 13)]
        self.assertFalse(self.call('propose_recording_plan', {'folder_name': 'Demo', 'tokens': good})['isError'])
        self.assertEqual(self.provider.calls[-1][1]['tokens'], good)
        result = self.call('propose_stop_all', {'capsule_ids': ['synthetic/invalid']})
        self.assertTrue(result['isError'])

    def test_read_only_tools_and_plan_ids_have_no_extra_parameters(self):
        for name in ('organizer_status', 'list_discord_windows'):
            self.assertTrue(self.call(name, {'confirm': True})['isError'])
        for value in (None, '', '../synthetic', 7, ['plan'], 'x' * 97):
            self.assertEqual(self.call('plan_result', {'plan_id': value})['structuredContent'], {'error': 'plan_unknown'})
        self.assertEqual(self.provider.calls, [])

    def test_every_closed_authority_error_is_preserved(self):
        for code in organizer.ORGANIZER_ERRORS:
            self.provider.error = code
            result = self.call('organizer_status')
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent'], {'error': code})

    def test_provider_exception_and_unknown_errors_do_not_escape(self):
        self.provider.error = 'SYNTHETIC_PRIVATE_DETAIL'
        self.assertEqual(self.call('organizer_status')['structuredContent'], {'error': 'unavailable'})
        def failed(*_):
            raise OSError('SYNTHETIC_PRIVATE_DETAIL')
        plane = organizer.OrganizerPlane(failed)
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', json.dumps(plane.execute('organizer_status', {})))

    def test_malformed_status_cannot_claim_ready_or_smuggle_extra_fields(self):
        for change in ({'encoder_ready': 'yes'}, {'obs_ready': 'yes'}, {'obs_ready': 1},
                       {'capsule_count': True}, {'recording_count': 1}, {'capsule_count': 13},
                       {'folder_root': 'SYNTHETIC_PRIVATE_DETAIL'},
                       {'obs_password': 'SYNTHETIC_PRIVATE_DETAIL'},
                       {'organizer_plan': {**plan(), 'expires_unix_ms': True}},
                       {'organizer_plan': {**plan(), 'expires_unix_ms': '946684830000'}},
                       {'organizer_plan': {**plan(), 'folder_name': '../synthetic'}}):
            self.provider.answers['organizer_status'] = {**status(), **change}
            result = self.call('organizer_status')
            self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
            self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', json.dumps(result))

    def test_bad_window_rows_and_duplicate_tokens_are_rejected(self):
        for rows in ([row(), row()], [{**row(), 'source_id': 'window:1:0'}],
                     [{**row(), 'classification': 'TrustedPopout'}],
                     [{**row(), 'bound_capsule': '../synthetic'}],
                     [{**row(), 'title': 'x' * 2049}]):
            self.provider.answers['list_discord_windows'] = {'windows': rows}
            self.assertEqual(self.call('list_discord_windows')['structuredContent'], {'error': 'unavailable'})

    def test_provider_cannot_promote_proposal_acceptance_to_recording(self):
        self.provider.answers['propose_recording_plan']['state'] = 'recording'
        self.assertEqual(self.call('propose_recording_plan', {'folder_name': 'Demo'})['structuredContent'], {'error': 'unavailable'})

    def test_only_exact_plan_terminal_results_are_returned(self):
        for state in ('rejected', 'expired'):
            self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-1', 'state': state, 'windows': []}
            self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-1'})['structuredContent']['state'], state)
        rows = [{'title': 'Synthetic call', 'capsule_id': 'cap-1', 'state': 'recording'},
                {'title': 'Synthetic closed', 'capsule_id': None, 'state': 'window_unavailable'}]
        self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-1', 'state': 'completed', 'windows': rows}
        self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-1'})['structuredContent']['windows'], rows)
        self.assertEqual(self.call('plan_result', {'plan_id': 'other-plan'})['structuredContent'], {'error': 'unavailable'})

    def test_executing_is_unavailable_until_terminal_observations(self):
        self.provider.error = 'unavailable'
        self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-1'})['structuredContent'], {'error': 'unavailable'})
        self.provider.error = None
        for state, rows in [('executing', []), ('completed', [{'title': 'Synthetic', 'capsule_id': None, 'state': 'pending'}]),
                            ('pending_confirmation', [{'title': 'Synthetic', 'capsule_id': 'cap-1', 'state': 'recording'}])]:
            self.provider.answers['plan_result'] = {'plan_id': 'organizer-7-1', 'state': state, 'windows': rows}
            self.assertEqual(self.call('plan_result', {'plan_id': 'organizer-7-1'})['structuredContent'], {'error': 'unavailable'})

    def test_duplicate_json_arguments_do_not_select_an_approval(self):
        raw = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"propose_recording_plan","arguments":{"folder_name":"Demo","folder_name":"Other"}}}\n'
        out = io.BytesIO()
        mcp.serve(self.server, io.BytesIO(raw), out)
        self.assertEqual(json.loads(out.getvalue())['error']['code'], -32700)
        self.assertEqual(self.provider.calls, [])

    def test_cli_has_explicit_opt_in_with_no_chat_root_requirement(self):
        commands = [request('initialize', {'protocolVersion': mcp.PROTOCOL_VERSION, 'capabilities': {},
                    'clientInfo': {'name': 'synthetic', 'version': '1'}}),
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, request('tools/list', identity=2)]
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'connectors')
        result = subprocess.run([sys.executable, '-B', '-m', 'cafe.mcp_server', '--organizer-rendezvous',
                                 'synthetic-unopened-rendezvous'],
                                input=''.join(json.dumps(x) + '\n' for x in commands).encode(),
                                capture_output=True, timeout=10, cwd=ROOT, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b'')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({tool['name'] for tool in rows[-1]['result']['tools']}, set(organizer.ORGANIZER_TOOLS))


class OrganizerBrokerTransport(unittest.TestCase):
    def setUp(self):
        self.ticket = {'schema': 'cafe.rendezvous.v1', 'port': 12345,
                       'epoch': '7', 'identity': BROKER_IDENTITY, 'secret': '17' * 32}
        self.ticket.update({'ho' + 'st': LOOPBACK})
        self.received = []
        self.peer_errors = []
        self.threads = []
        self.sockets = []
        self.ticket_calls = 0
        self.reply_transform = lambda reply: reply
        self.plane = organizer.OrganizerPlane(organizer.BrokerOrganizerProvider(
            'synthetic-private-rendezvous', ticket_fetcher=self.fetch_ticket, connector=self.connect))
        self.addCleanup(self.close)

    def fetch_ticket(self, _):
        self.ticket_calls += 1
        return self.ticket

    def close(self):
        for stream in self.sockets:
            stream.close()
        for thread in self.threads:
            thread.join(timeout=3)
        self.assertEqual(self.peer_errors, [])

    def connect(self, ticket):
        client, peer = socket.socketpair()
        self.sockets.extend([client, peer])
        def worker():
            import hashlib
            import hmac
            try:
                peer.settimeout(3)
                nonce = '29' * 32
                _write_frame(peer, {'schema': 'cafe.broker.auth.v1', 'identity': BROKER_IDENTITY,
                                   'epoch': '7', 'nonce': nonce})
                proof = _read_frame(peer)
                expected = hmac.new(bytes.fromhex(ticket['secret']),
                                    ('cafe.broker.auth.v1\nbroker\n7\n' + nonce).encode(),
                                    hashlib.sha256).hexdigest()
                self.assertEqual(proof, {'identity': BROKER_IDENTITY, 'epoch': '7', 'proof': expected})
                _write_frame(peer, {'authenticated': True})
                frame = _read_frame(peer)
                self.received.append(frame)
                wire = frame['body']
                data = ({'plan_id': 'organizer-7-1', 'state': 'pending_confirmation'}
                        if wire['body']['op'] == 'propose_recording_plan' else status())
                reply = self.reply_transform({'request_id': wire['request_id'], 'data': data, 'error': None})
                _write_frame(peer, reply)
            except BaseException as exc:
                self.peer_errors.append(type(exc).__name__)
            finally:
                peer.close()
        thread = threading.Thread(target=worker, daemon=True)
        self.threads.append(thread)
        thread.start()
        return client

    def test_authenticated_proposal_has_only_closed_organizer_wire(self):
        result = self.plane.execute('propose_recording_plan', {'folder_name': 'Demo 01', 'tokens': ['window-7-1']})
        self.assertEqual(result['structuredContent'], {'plan_id': 'organizer-7-1', 'state': 'pending_confirmation'})
        frame = self.received[0]
        self.assertEqual(set(frame), {'epoch', 'capability', 'body'})
        self.assertEqual(frame['capability'], 'control')
        wire = frame['body']
        self.assertEqual(set(wire), {'schema', 'kind', 'request_id', 'authority_epoch', 'body'})
        self.assertEqual(wire['schema'], 'cafe.organizer.v1')
        self.assertEqual(wire['kind'], 'organizer_request')
        self.assertEqual(wire['authority_epoch'], '7')
        self.assertEqual(wire['body'], {'op': 'propose_recording_plan', 'folder_name': 'Demo 01', 'tokens': ['window-7-1']})
        self.assertNotIn('confirmation', json.dumps(frame))
        self.assertNotIn(self.ticket['secret'], json.dumps(frame))

    def test_ticket_cached_and_request_ids_not_caller_controlled(self):
        self.assertFalse(self.plane.execute('organizer_status', {})['isError'])
        self.assertFalse(self.plane.execute('organizer_status', {})['isError'])
        self.assertEqual(self.ticket_calls, 1)
        self.assertNotEqual(self.received[0]['body']['request_id'], self.received[1]['body']['request_id'])

    def test_mismatched_reply_is_unavailable_without_replay(self):
        self.reply_transform = lambda reply: {**reply, 'request_id': 'wrong-request'}
        result = self.plane.execute('propose_recording_plan', {'folder_name': 'Demo'})
        self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
        self.assertEqual(len(self.received), 1)

    def test_missing_private_ticket_is_closed_without_connecting(self):
        calls = []
        def denied(_):
            raise OSError('SYNTHETIC_PRIVATE_DETAIL')
        plane = organizer.OrganizerPlane(organizer.BrokerOrganizerProvider('synthetic-private-rendezvous',
            ticket_fetcher=denied, connector=lambda ticket: calls.append(ticket)))
        result = plane.execute('organizer_status', {})
        self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
        self.assertEqual(calls, [])
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', json.dumps(result))

    def test_remote_host_ticket_cannot_be_used_to_dial(self):
        self.ticket['host'] = 'synthetic.invalid'
        result = self.plane.execute('organizer_status', {})
        self.assertEqual(result['structuredContent'], {'error': 'unavailable'})
        self.assertEqual(self.received, [])


if __name__ == '__main__':
    unittest.main()
