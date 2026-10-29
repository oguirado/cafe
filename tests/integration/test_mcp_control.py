"""Independent stdlib checks for the MCP control plane; socketpair fixture only.

No MCP registration, no network syscalls: the broker double plugs into the
ControlPlane ticket_fetcher/connector seams over socketpair while the
production code path (handshake, framing, HMAC, validation) runs untouched.
Golden wires and transitions are handwritten below.
"""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_control as ctl
from cafe import mcp_server as mcp

NOW = 1_700_000_000_000_000_000
EPOCH = '7'
REVISION = '4'
COMMIT_REVISION = '5'
CAPSULE = 'cap-main'
TOKEN = 't0K3n-canary_' + 'z' * 19
CANARY_TOKEN = 'CANARY9_' + 'q' * 24


def make_wire(request_id='req-1', op='start_recording', capsule=CAPSULE,
              token=TOKEN, epoch=EPOCH, revision=REVISION):
    return {'schema': 'cafe.control_intent.v1', 'kind': 'control_intent',
            'request_id': request_id, 'authority_epoch': epoch,
            'expected_revision': revision,
            'body': {'op': op, 'confirmation_token': token,
                     'capsule_id': capsule}}


def make_snapshot(epoch=EPOCH, revision=REVISION, capsules=(CAPSULE,)):
    return {'authority_epoch': epoch, 'revision': revision,
            'capsules': {name: {'feeds': {}} for name in capsules}}


def approval_for(wire, issued=NOW, calls=None):
    def provide(request_id, op, capsule_id):
        if calls is not None:
            calls.append((request_id, op, capsule_id))
        return (wire, issued)

    return provide


def request(method, params=None, identity=1):
    result = {'jsonrpc': '2.0', 'id': identity, 'method': method}
    if params is not None:
        result['params'] = params
    return result


def initialize(server):
    response = server.handle(request('initialize', {
        'protocolVersion': mcp.PROTOCOL_VERSION, 'capabilities': {},
        'clientInfo': {'name': 'synthetic', 'version': '1'}}))
    assert 'result' in response
    assert server.handle({'jsonrpc': '2.0',
                          'method': 'notifications/initialized'}) is None


def _recv_line(conn):
    data = bytearray()
    while True:
        if len(data) > 16384:
            raise ValueError('frame_too_large')
        chunk = conn.recv(4096)
        if not chunk:
            raise ValueError('frame_eof')
        data += chunk
        index = data.find(b'\n')
        if index != -1:
            return bytes(data[:index + 1])


class LoopbackBroker:
    """Socketpair broker double speaking the real handshake bytes.

    Plugs into ControlPlane via the ticket_fetcher/connector seams so the
    production handshake, framing, HMAC and reply validation run end to end
    without network syscalls. The default production path still dials real
    127.0.0.1 loopback TCP plus a UDS rendezvous fetch.
    """

    def __init__(self, *, epoch=EPOCH, behavior='committed',
                 commit_revision=COMMIT_REVISION, connect_fail=False):
        self.epoch = epoch
        self.behavior = behavior
        self.commit_revision = commit_revision
        self.connect_fail = connect_fail
        self.secret = os.urandom(32).hex()
        self.ticket = {'schema': 'cafe.rendezvous.v1', 'host': '127.0.0.1',
                       'port': 1, 'epoch': epoch, 'identity': 'broker',
                       'secret': self.secret}
        self.requests = []
        self.frames = []
        self.connects = []
        self._lock = threading.Lock()
        self._threads = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for worker in self._threads:
            worker.join(timeout=10)
        return False

    @property
    def request_count(self):
        with self._lock:
            return len(self.requests)

    def fetch_ticket(self, rendezvous_dir):
        return dict(self.ticket)

    def connect(self, ticket):
        with self._lock:
            self.connects.append(dict(ticket))
        if self.connect_fail:
            raise OSError('connection_refused')
        client, server = socket.socketpair()
        worker = threading.Thread(target=self._handle, args=(server,),
                                  daemon=True)
        worker.start()
        with self._lock:
            self._threads.append(worker)
        return client

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            nonce = os.urandom(32).hex()
            conn.sendall(json.dumps(
                {'schema': 'cafe.broker.auth.v1', 'identity': 'broker',
                 'epoch': self.epoch, 'nonce': nonce},
                separators=(',', ':')).encode() + b'\n')
            proof = json.loads(_recv_line(conn))
            expected = hmac.new(
                bytes.fromhex(self.secret),
                ('cafe.broker.auth.v1\nbroker\n' + self.epoch + '\n'
                 + nonce).encode(), hashlib.sha256).hexdigest()
            if proof != {'identity': 'broker', 'epoch': self.epoch,
                         'proof': expected}:
                return
            conn.sendall(b'{"authenticated":true}\n')
            raw = _recv_line(conn)
            seen = json.loads(raw)
            with self._lock:
                self.requests.append(seen)
                self.frames.append(raw)
            wire = seen.get('body', {})
            inner = wire.get('body', {}) if type(wire) is dict else {}
            request_id = wire.get('request_id', 'invalid') \
                if type(wire) is dict else 'invalid'
            op = inner.get('op', 'start_recording') \
                if type(inner) is dict else 'start_recording'
            if self.behavior == 'drop':
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            if self.behavior == 'garbage':
                conn.sendall(b'not-json\n')
                return
            if self.behavior == 'wrong_id':
                reply = {'request_id': 'someone-else', 'state': 'committed',
                         'revision': self.commit_revision, 'error': None,
                         'receipt': {'schema': 'cafe.control_receipt.v1',
                                     'op': op,
                                     'effect_id': 'ctl.' + hashlib.sha256(
                                         b'someone-else').hexdigest()}}
            elif self.behavior == 'rejected':
                reply = {'request_id': request_id, 'state': 'rejected',
                         'revision': REVISION,
                         'error': 'confirmation_binding_or_expiry',
                         'receipt': None}
            else:
                reply = {'request_id': request_id, 'state': 'committed',
                         'revision': self.commit_revision, 'error': None,
                         'receipt': {'schema': 'cafe.control_receipt.v1',
                                     'op': op,
                                     'effect_id': 'ctl.' + hashlib.sha256(
                                         str(request_id).encode())
                                     .hexdigest()}}
            conn.sendall(json.dumps(reply, separators=(',', ':')).encode()
                         + b'\n')
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-mcp-control-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.audit = self.root / 'audit.jsonl'

    def plane(self, **kwargs):
        kwargs.setdefault('clock', lambda: NOW)
        return ctl.ControlPlane(self.audit, **kwargs)

    def audit_lines(self, path=None):
        target = self.audit if path is None else path
        if not target.exists():
            return []
        return [json.loads(line) for line in
                target.read_text(encoding='utf-8').splitlines()]

    def reply_of(self, envelope):
        self.assertEqual(set(envelope),
                         {'content', 'structuredContent', 'isError'})
        self.assertEqual(json.loads(envelope['content'][0]['text']),
                         envelope['structuredContent'])
        return envelope['structuredContent']

    def test_select_accepts_snapshot_member(self):
        plane = self.plane(snapshot_provider=lambda: make_snapshot())
        envelope = plane.execute('select_capsule', {'capsule_id': CAPSULE},
                                 NOW)
        reply = self.reply_of(envelope)
        self.assertEqual(reply, {'capsule_id': CAPSULE, 'state': 'selected',
                                 'error': None})
        self.assertFalse(envelope['isError'])
        self.assertEqual(plane.selected_capsule, CAPSULE)
        (line,) = self.audit_lines()
        self.assertEqual(line, {
            'ts_ns': NOW, 'op': 'select_capsule', 'request_id_sha256': None,
            'capsule_id_sha256': hashlib.sha256(CAPSULE.encode()).hexdigest(),
            'authority_epoch': EPOCH, 'expected_revision': REVISION,
            'state': 'selected', 'error': None, 'effect_id': None})

    def test_select_rejects_unknown_capsule(self):
        plane = self.plane(snapshot_provider=lambda: make_snapshot())
        envelope = plane.execute('select_capsule',
                                 {'capsule_id': 'cap-ghost'}, NOW)
        reply = self.reply_of(envelope)
        self.assertEqual(reply, {'capsule_id': 'cap-ghost',
                                 'state': 'rejected',
                                 'error': 'capsule_unavailable'})
        self.assertTrue(envelope['isError'])
        self.assertIsNone(plane.selected_capsule)

    def test_default_deny_without_providers(self):
        plane = self.plane()
        cases = [
            ('select_capsule', {'capsule_id': CAPSULE}, 'blocked',
             'mcp_control_unavailable'),
            ('start_recording',
             {'request_id': 'req-1', 'capsule_id': CAPSULE}, 'blocked',
             'mcp_control_unavailable'),
            ('stop_recording', {'request_id': 'req-2'}, 'rejected',
             'capsule_unavailable'),
            ('send_to_obs', {'request_id': 'req-3'}, 'rejected',
             'capsule_unavailable'),
        ]
        for tool, args, state, error in cases:
            with self.subTest(tool=tool):
                reply = self.reply_of(plane.execute(tool, args, NOW))
                self.assertEqual(reply['state'], state)
                self.assertEqual(reply['error'], error)
                self.assertIsNone(reply.get('revision'))

    def test_tools_hidden_by_default(self):
        server = mcp.Server(self.root)
        initialize(server)
        tools = server.handle(request('tools/list'))['result']['tools']
        self.assertEqual(len(tools), 6)
        self.assertEqual({item['name'] for item in tools},
                         set(mcp._TOOL_ARGUMENTS))
        good = {'select_capsule': {'capsule_id': CAPSULE},
                'start_recording': {'request_id': 'req-1'}}
        for name, args in good.items():
            with self.subTest(name=name):
                response = server.handle(request(
                    'tools/call', {'name': name, 'arguments': args}))
                self.assertEqual(response['error']['code'], -32602)

    def test_tools_listed_with_control_plane(self):
        server = mcp.Server(self.root, control=self.plane())
        initialize(server)
        tools = server.handle(request('tools/list'))['result']['tools']
        self.assertEqual(len(tools), 10)
        by_name = {item['name']: item for item in tools}
        for name in ctl.CONTROL_TOOLS:
            with self.subTest(name=name):
                schema = by_name[name]
                self.assertIs(
                    schema['inputSchema']['additionalProperties'], False)
                self.assertIs(schema['annotations']['readOnlyHint'], False)
                self.assertIs(schema['annotations']['idempotentHint'], False)
                self.assertIs(schema['annotations']['openWorldHint'], False)

    def test_validate_arguments_rejects_secrets_and_junk(self):
        bad = [
            ('start_recording',
             {'request_id': 'req-1', 'confirmation_token': TOKEN}),
            ('start_recording', {'request_id': 'req-1', 'approve': True}),
            ('start_recording', {'request_id': 'req-1', 'extra': 'x'}),
            ('start_recording', {}),
            ('start_recording', {'request_id': '-bad'}),
            ('start_recording', {'request_id': 'x' * 97}),
            ('start_recording', {'request_id': 'req-1',
                                 'capsule_id': 'no spaces'}),
            ('select_capsule', {}),
            ('select_capsule', {'capsule_id': ''}),
            ('select_capsule',
             {'capsule_id': CAPSULE, 'request_id': 'req-1'}),
        ]
        for name, args in bad:
            with self.subTest(name=name, args=args):
                with self.assertRaises(ValueError):
                    mcp.validate_arguments(name, args)
        plane = mock.Mock()
        server = mcp.Server(self.root, control=plane)
        initialize(server)
        response = server.handle(request('tools/call', {
            'name': 'start_recording',
            'arguments': {'request_id': 'req-1',
                          'confirmation_token': TOKEN}}))
        self.assertEqual(response['error']['code'], -32602)
        plane.execute.assert_not_called()

    def test_start_committed_golden(self):
        wire = make_wire()
        calls = []
        with LoopbackBroker() as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(wire, calls=calls),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            envelope = plane.execute(
                'start_recording',
                {'request_id': 'req-1', 'capsule_id': CAPSULE}, NOW)
        reply = self.reply_of(envelope)
        effect = 'ctl.' + hashlib.sha256(b'req-1').hexdigest()
        self.assertEqual(reply, {
            'request_id': 'req-1', 'state': 'committed',
            'revision': COMMIT_REVISION, 'error': None,
            'receipt': {'schema': 'cafe.control_receipt.v1',
                        'op': 'start_recording', 'effect_id': effect}})
        self.assertFalse(envelope['isError'])
        self.assertEqual(calls, [('req-1', 'start_recording', CAPSULE)])
        (seen,) = broker.requests
        self.assertEqual(seen, {'epoch': EPOCH, 'capability': 'control',
                                'body': wire})
        self.assertLessEqual(len(broker.frames[0]), 16384)
        req_hash = hashlib.sha256(b'req-1').hexdigest()
        cap_hash = hashlib.sha256(CAPSULE.encode()).hexdigest()
        admission, outcome = self.audit_lines()
        self.assertEqual(admission, {
            'ts_ns': NOW, 'op': 'start_recording',
            'request_id_sha256': req_hash, 'capsule_id_sha256': cap_hash,
            'authority_epoch': EPOCH, 'expected_revision': REVISION,
            'state': 'pending', 'error': None, 'effect_id': None})
        self.assertEqual(outcome, {
            'ts_ns': NOW, 'op': 'start_recording',
            'request_id_sha256': req_hash, 'capsule_id_sha256': cap_hash,
            'authority_epoch': EPOCH, 'expected_revision': COMMIT_REVISION,
            'state': 'committed', 'error': None, 'effect_id': effect})

    def test_stop_and_send_commit(self):
        for op in ('stop_recording', 'send_to_obs'):
            with self.subTest(op=op):
                audit = self.root / (op + '.jsonl')
                wire = make_wire(request_id='req-' + op, op=op)
                with LoopbackBroker() as broker:
                    plane = ctl.ControlPlane(
                        audit, snapshot_provider=lambda: make_snapshot(),
                        approval_provider=approval_for(wire),
                        rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
                    reply = self.reply_of(plane.execute(
                        op, {'request_id': 'req-' + op,
                             'capsule_id': CAPSULE}, NOW))
                self.assertEqual(reply['state'], 'committed')
                self.assertEqual(reply['receipt']['op'], op)
                self.assertEqual(broker.request_count, 1)

    def test_selected_capsule_used_when_omitted(self):
        wire = make_wire()
        with LoopbackBroker() as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(wire),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            selected = self.reply_of(plane.execute(
                'select_capsule', {'capsule_id': CAPSULE}, NOW))
            self.assertEqual(selected['state'], 'selected')
            reply = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1'}, NOW))
            self.assertEqual(reply['state'], 'committed')
            (seen,) = broker.requests
            self.assertEqual(seen['body']['body']['capsule_id'], CAPSULE)

    def test_authority_rejection_passes_through(self):
        with LoopbackBroker(behavior='rejected') as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(make_wire()),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            envelope = plane.execute(
                'start_recording',
                {'request_id': 'req-1', 'capsule_id': CAPSULE}, NOW)
        reply = self.reply_of(envelope)
        self.assertEqual(reply, {'request_id': 'req-1', 'state': 'rejected',
                                 'revision': REVISION,
                                 'error': 'confirmation_binding_or_expiry',
                                 'receipt': None})
        self.assertTrue(envelope['isError'])
        admission, outcome = self.audit_lines()
        self.assertEqual(outcome['state'], 'rejected')
        self.assertEqual(outcome['error'], 'confirmation_binding_or_expiry')
        self.assertIsNone(outcome['effect_id'])

    def test_missing_approval_blocked(self):
        plane = self.plane(
            snapshot_provider=lambda: make_snapshot(),
            approval_provider=lambda request_id, op, capsule_id: None)
        reply = self.reply_of(plane.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error'], reply['revision']),
                         ('blocked', 'confirmation_required', REVISION))
        unconfigured = self.plane(
            snapshot_provider=lambda: make_snapshot())
        reply = self.reply_of(unconfigured.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))

    def test_expired_and_future_approval_rejected(self):
        cases = [(NOW - 30_000_000_001, 'expired'),
                 (NOW - 30_000_000_000, 'boundary'),
                 (NOW + 1, 'future')]
        for issued, label in cases:
            with self.subTest(label=label):
                wire = make_wire(request_id='req-' + label)
                plane = self.plane(
                    snapshot_provider=lambda: make_snapshot(),
                    approval_provider=approval_for(wire, issued=issued))
                reply = self.reply_of(plane.execute(
                    'start_recording', {'request_id': 'req-' + label,
                                        'capsule_id': CAPSULE}, NOW))
                self.assertEqual(reply['state'], 'rejected')
                self.assertEqual(reply['error'],
                                 'confirmation_binding_or_expiry')
        fresh = make_wire(request_id='req-fresh')
        plane = self.plane(
            snapshot_provider=lambda: make_snapshot(),
            approval_provider=approval_for(fresh,
                                           issued=NOW - 30_000_000_000 + 1))
        reply = self.reply_of(plane.execute(
            'start_recording', {'request_id': 'req-fresh',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))

    def test_replay_rejected_without_resend(self):
        wire = make_wire()
        calls = []
        with LoopbackBroker() as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(wire, calls=calls),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            first = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual(first['state'], 'committed')
            second = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual((second['state'], second['error'],
                              second['revision']),
                             ('rejected', 'request_id_conflict', None))
            self.assertEqual(broker.request_count, 1)
            self.assertEqual(len(calls), 1)

    def test_token_reuse_rejected(self):
        wire_a = make_wire(request_id='req-a')
        wire_b = make_wire(request_id='req-b')

        def provide(request_id, op, capsule_id):
            return (wire_a if request_id == 'req-a' else wire_b, NOW)

        with LoopbackBroker() as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=provide, rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            first = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-a',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual(first['state'], 'committed')
            second = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-b',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual((second['state'], second['error']),
                             ('rejected', 'confirmation_binding_or_expiry'))
            self.assertEqual(broker.request_count, 1)

    def test_malformed_wires_rejected_before_effect(self):
        reflected = 'req-' + TOKEN
        base = make_wire()
        cases = [
            ('extra_key', {**base, 'extra': 1}, 'invalid_control_intent'),
            ('missing_key',
             {key: base[key] for key in base if key != 'kind'},
             'invalid_control_intent'),
            ('bad_schema', {**base, 'schema': 'cafe.evil.v1'},
             'invalid_control_intent'),
            ('short_token', make_wire(token='abc'), 'invalid_control_intent'),
            ('bad_token_chars', make_wire(token='!' * 32),
             'invalid_control_intent'),
            ('bad_epoch', make_wire(epoch='01'), 'invalid_control_intent'),
            ('negative_epoch', make_wire(epoch='-1'),
             'invalid_control_intent'),
            ('huge_epoch', make_wire(epoch=str(1 << 64)),
             'invalid_control_intent'),
            ('bad_revision', make_wire(revision='x'),
             'invalid_control_intent'),
            ('set_folder_body', make_wire(op='start_recording') | {
                'body': {'op': 'set_folder', 'confirmation_token': TOKEN,
                         'folder': '/tmp'}},
             'confirmation_binding_or_expiry'),
            ('non_dict_body', {**base, 'body': 'x'},
             'invalid_control_intent'),
            ('non_dict_wire', 'x', 'invalid_control_intent'),
            ('bad_issued', (base, 'now'), 'invalid_control_intent'),
            ('token_reflected', make_wire(request_id=reflected),
             'invalid_control_intent'),
        ]
        with LoopbackBroker() as broker:
            for label, approval, error in cases:
                with self.subTest(label=label):
                    args = {'request_id': 'req-1', 'capsule_id': CAPSULE}
                    if label == 'token_reflected':
                        args = {'request_id': reflected,
                                'capsule_id': CAPSULE}
                    plane = ctl.ControlPlane(
                        self.root / (label + '.jsonl'),
                        snapshot_provider=lambda: make_snapshot(),
                        approval_provider=lambda *a, _v=approval: (_v, NOW) if type(_v) is dict else _v,
                        rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
                    reply = self.reply_of(plane.execute(
                        'start_recording', args, NOW))
                    self.assertEqual(reply['state'], 'rejected')
                    self.assertEqual(reply['error'], error)
            self.assertEqual(broker.request_count, 0)

    def test_wrong_bindings_rejected(self):
        cases = [
            ('request', make_wire(request_id='req-other'),
             'confirmation_binding_or_expiry'),
            ('op', make_wire(op='stop_recording'),
             'confirmation_binding_or_expiry'),
            ('capsule', make_wire(capsule='cap-other'),
             'confirmation_binding_or_expiry'),
            ('epoch', make_wire(epoch='8'),
             'control_identity_or_epoch_denied'),
            ('revision', make_wire(revision='9'), 'stale_revision'),
        ]
        with LoopbackBroker() as broker:
            for label, wire, error in cases:
                with self.subTest(label=label):
                    plane = ctl.ControlPlane(
                        self.root / ('bind-' + label + '.jsonl'),
                        snapshot_provider=lambda: make_snapshot(),
                        approval_provider=approval_for(wire),
                        rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
                    reply = self.reply_of(plane.execute(
                        'start_recording', {'request_id': 'req-1',
                                            'capsule_id': CAPSULE}, NOW))
                    self.assertEqual(reply['state'], 'rejected')
                    self.assertEqual(reply['error'], error)
            self.assertEqual(broker.request_count, 0)

    def test_unknown_revision_is_null(self):
        plane = self.plane(
            snapshot_provider=lambda: make_snapshot(revision=None),
            approval_provider=approval_for(make_wire()))
        reply = self.reply_of(plane.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('rejected', 'stale_revision'))
        self.assertIsNone(reply['revision'])
        for line in self.audit_lines():
            self.assertIsNone(line['expected_revision'])
        selected = self.reply_of(plane.execute(
            'select_capsule', {'capsule_id': CAPSULE}, NOW))
        self.assertEqual(selected['state'], 'selected')

    def test_missing_bridge_blocked(self):
        wire = make_wire()
        plane = self.plane(snapshot_provider=lambda: make_snapshot(),
                           approval_provider=approval_for(wire))
        reply = self.reply_of(plane.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))
        empty = self.root / 'empty'
        empty.mkdir()
        brokerless = self.plane(snapshot_provider=lambda: make_snapshot(),
                                approval_provider=approval_for(wire),
                                rendezvous_dir=empty)
        reply = self.reply_of(brokerless.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))
        with LoopbackBroker(connect_fail=True) as broker:
            refused = ctl.ControlPlane(
                self.root / 'refused.jsonl',
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(wire),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            reply = self.reply_of(refused.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual((reply['state'], reply['error']),
                             ('blocked', 'control_transport_error'))
            admission, outcome = self.audit_lines(self.root / 'refused.jsonl')
            self.assertEqual(admission['state'], 'pending')
            self.assertEqual(outcome['error'], 'control_transport_error')

    def test_stale_ticket_epoch_rejected_without_dial(self):
        with LoopbackBroker(epoch='8') as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(make_wire()),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket,
                connector=broker.connect)
            reply = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual((reply['state'], reply['error']),
                             ('rejected', 'control_identity_or_epoch_denied'))
            self.assertEqual(broker.connects, [])
            self.assertEqual(broker.request_count, 0)

    def test_insecure_rendezvous_fails_closed(self):
        exposed = self.root / 'exposed'
        exposed.mkdir()
        (exposed / 'broker.sock').write_text('not-a-socket')
        os.chmod(exposed, 0o755)
        try:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(make_wire()),
                rendezvous_dir=exposed)
            reply = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
        finally:
            os.chmod(exposed, 0o700)
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))

    def test_uncertain_dispatch_never_retried(self):
        for behavior in ('drop', 'garbage', 'wrong_id'):
            with self.subTest(behavior=behavior):
                audit = self.root / (behavior + '.jsonl')
                request_id = 'req-' + behavior
                with LoopbackBroker(behavior=behavior) as broker:
                    plane = ctl.ControlPlane(
                        audit, snapshot_provider=lambda: make_snapshot(),
                        approval_provider=approval_for(
                            make_wire(request_id=request_id)),
                        rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
                    first = self.reply_of(plane.execute(
                        'start_recording', {'request_id': request_id,
                                            'capsule_id': CAPSULE}, NOW))
                    self.assertEqual((first['state'], first['error'],
                                      first['revision']),
                                     ('interrupted', 'uncertain_dispatch',
                                      None))
                    self.assertIn(request_id, plane.uncertain_requests)
                    second = self.reply_of(plane.execute(
                        'start_recording', {'request_id': request_id,
                                            'capsule_id': CAPSULE}, NOW))
                    self.assertEqual((second['state'], second['error']),
                                     ('rejected', 'request_id_conflict'))
                    self.assertEqual(broker.request_count, 1)
                admission, outcome, replay = self.audit_lines(audit)
                self.assertEqual(admission['state'], 'pending')
                self.assertEqual(outcome['error'], 'uncertain_dispatch')
                self.assertEqual(replay['error'], 'request_id_conflict')

    def test_audit_and_responses_scrubbed(self):
        wire = make_wire(token=CANARY_TOKEN)
        with LoopbackBroker() as broker:
            plane = self.plane(
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(wire),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            self.reply_of(plane.execute(
                'select_capsule', {'capsule_id': CAPSULE}, NOW))
            self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.reply_of(plane.execute(
                'select_capsule', {'capsule_id': '..bad..'}, NOW))
            secret = broker.secret

            def explode(request_id, op, capsule_id):
                raise RuntimeError('BOOM_MARKER_XYZ')

            failing = ctl.ControlPlane(
                self.root / 'explode.jsonl',
                snapshot_provider=lambda: make_snapshot(),
                approval_provider=explode)
            failing.execute('start_recording', {'request_id': 'req-9',
                                                'capsule_id': CAPSULE}, NOW)
        raw = self.audit.read_text(encoding='utf-8')
        raw += (self.root / 'explode.jsonl').read_text(encoding='utf-8')
        for forbidden in (CANARY_TOKEN, TOKEN, secret, 'confirmation_token',
                          'secret', 'proof', 'nonce', 'Traceback',
                          'BOOM_MARKER_XYZ', str(self.root), CAPSULE, 'req-1',
                          'req-9', '..bad..'):
            self.assertNotIn(forbidden, raw)
        for line in self.audit_lines():
            self.assertEqual(
                set(line),
                {'ts_ns', 'op', 'request_id_sha256', 'capsule_id_sha256',
                 'authority_epoch', 'expected_revision', 'state', 'error',
                 'effect_id'})

    def test_responses_scrub_malformed_ids(self):
        plane = self.plane(snapshot_provider=lambda: make_snapshot())
        reply = self.reply_of(plane.execute(
            'start_recording', {'request_id': '..bad',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual(reply['request_id'], 'invalid')
        self.assertNotIn('..bad', json.dumps(reply))
        selected = self.reply_of(plane.execute(
            'select_capsule', {'capsule_id': '..bad'}, NOW))
        self.assertEqual(selected['capsule_id'], 'invalid')

    def test_audit_failure_blocks_effect(self):
        missing = self.root / 'no-such-dir' / 'audit.jsonl'
        with LoopbackBroker() as broker:
            plane = ctl.ControlPlane(
                missing, snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(make_wire()),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect)
            reply = self.reply_of(plane.execute(
                'start_recording', {'request_id': 'req-1',
                                    'capsule_id': CAPSULE}, NOW))
            self.assertEqual((reply['state'], reply['error']),
                             ('blocked', 'audit_unavailable'))
            selected = self.reply_of(plane.execute(
                'select_capsule', {'capsule_id': CAPSULE}, NOW))
            self.assertEqual(selected['error'], 'audit_unavailable')
            self.assertIsNone(plane.selected_capsule)
            self.assertEqual(broker.request_count, 0)

    def test_strict_frame_size(self):
        client, server = socket.socketpair()
        try:
            client.sendall(b'x' * 16385)
            with self.assertRaises(ValueError):
                ctl._read_frame(server)
        finally:
            client.close()
            server.close()
        client, server = socket.socketpair()
        try:
            with self.assertRaises(ValueError):
                ctl._write_frame(client, {'blob': 'y' * 20000})
        finally:
            client.close()
            server.close()
        wire = make_wire(request_id='r' * 96, capsule='c' * 96,
                         token='t' * 256)
        frame = {'epoch': '9' * 20, 'capability': 'control', 'body': wire}
        self.assertLessEqual(len(ctl._frame_bytes(frame)), 16384)

    def test_approval_container_forms(self):
        wire = make_wire()
        forms = [(wire, NOW), {'wire': wire, 'issued_ns': NOW}]
        for index, form in enumerate(forms):
            with self.subTest(index=index):
                plane = ctl.ControlPlane(
                    self.root / ('form-%d.jsonl' % index),
                    snapshot_provider=lambda: make_snapshot(),
                    approval_provider=lambda *a, _v=form: _v)
                reply = self.reply_of(plane.execute(
                    'start_recording', {'request_id': 'req-1',
                                        'capsule_id': CAPSULE}, NOW))
                self.assertEqual((reply['state'], reply['error']),
                                 ('blocked', 'mcp_control_unavailable'))
        queue = {('req-1', 'start_recording', CAPSULE): (wire, NOW)}
        queued = self.plane(snapshot_provider=lambda: make_snapshot(),
                            approval_queue=queue)
        reply = self.reply_of(queued.execute(
            'start_recording', {'request_id': 'req-1',
                                'capsule_id': CAPSULE}, NOW))
        self.assertEqual((reply['state'], reply['error']),
                         ('blocked', 'mcp_control_unavailable'))
        with self.assertRaises(ValueError):
            self.plane(snapshot_provider=lambda: make_snapshot(),
                       approval_provider=approval_for(wire),
                       approval_queue=queue)

    def test_execute_validates_trusted_params(self):
        plane = self.plane()
        with self.assertRaises(ValueError):
            plane.execute('nope', {}, NOW)
        with self.assertRaises(ValueError):
            plane.execute('start_recording', [], NOW)
        with self.assertRaises(ValueError):
            plane.execute('start_recording', {}, -1)
        with self.assertRaises(ValueError):
            plane.execute('start_recording', {}, True)

    def test_server_control_roundtrip(self):
        with LoopbackBroker() as broker:
            plane = ctl.ControlPlane(
                self.audit, snapshot_provider=lambda: make_snapshot(),
                approval_provider=approval_for(make_wire()),
                rendezvous_dir=self.root, ticket_fetcher=broker.fetch_ticket, connector=broker.connect, clock=lambda: NOW)
            server = mcp.Server(self.root, control=plane,
                                clock=lambda: NOW // 1_000_000_000)
            initialize(server)
            response = server.handle(request('tools/call', {
                'name': 'start_recording',
                'arguments': {'request_id': 'req-1',
                              'capsule_id': CAPSULE}}))
        structured = response['result']['structuredContent']
        self.assertEqual(structured['state'], 'committed')
        self.assertFalse(response['result']['isError'])

    def test_cli_defaults_blocked(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'connectors')
        cases = [
            (['start', '--request-id', 'req-cli-1', '--capsule-id', CAPSULE],
             'blocked', 'mcp_control_unavailable'),
            (['start', '--request-id', 'req-cli-2'],
             'rejected', 'capsule_unavailable'),
            (['select', '--capsule-id', CAPSULE],
             'blocked', 'mcp_control_unavailable'),
        ]
        for argv, state, error in cases:
            with self.subTest(argv=argv):
                command = [sys.executable, '-B', '-m', 'cafe.mcp_control',
                           '--audit', str(self.audit)] + argv
                result = subprocess.run(command, capture_output=True,
                                        timeout=30, env=env, cwd=ROOT)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, b'')
                reply = json.loads(result.stdout)
                self.assertEqual(reply['state'], state)
                self.assertEqual(reply['error'], error)
        self.assertEqual(len(self.audit_lines()), 3)


if __name__ == '__main__':
    unittest.main()

