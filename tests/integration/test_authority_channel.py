"""Independent real-process authority/channel tests; no generated snapshots."""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import queue
import threading
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import channel_auth as auth


def assert_canary_absent(root, outputs, canary):
    """Independent detector; never prints a discovered synthetic secret."""
    needle = canary.encode()
    for output in outputs:
        if needle in output:
            raise AssertionError('private canary leaked to process output')
    for path in root.rglob('*'):
        if path.is_file() and needle in path.read_bytes():
            raise AssertionError('private canary leaked to artifact')


SIDECAR = '''import asyncio,json,sys
from cafe.authority_channel import serve_connector
async def effect(body):
    action=body['action']
    if action=='exit':
        raise SystemExit(0)
    if action=='giant':
        sys.stdout.buffer.write(b'x'*20000+b'\\n');sys.stdout.buffer.flush()
        return True
    if action=='stderr':
        sys.stderr.buffer.write(b'diagnostic '*20000);sys.stderr.buffer.flush()
        return True
    if action=='old_epoch':
        frame={'schema':'cafe.connector.v1','kind':'observation','request_id':'channel','authority_epoch':'1','expected_revision':'0','body':{'ready':True}}
        sys.stdout.write(json.dumps(frame)+'\\n');sys.stdout.flush()
        return True
    if action=='effect':
        with open('effect.marker','a') as f:f.write('effect\\n')
        return True
    return False
try:asyncio.run(serve_connector(effect))
except BaseException:sys.exit(0)
'''
BROKER = '''import asyncio,json,sys
from cafe.channel_auth import receive_ticket,connect_broker
async def run():
    try:
        ticket=await receive_ticket(sys.argv[1])
        result=await connect_broker(ticket,sys.argv[2])
        print(json.dumps(result))
    except Exception:
        print('broker_rejected')
        raise SystemExit(3)
asyncio.run(run())
'''


class Authority:
    def __init__(self, root):
        self.root = root
        self.output = []
        (root / 'sidecar.py').write_text(SIDECAR)
        (root / 'broker.py').write_text(BROKER)
        (root / 'journal.jsonl').write_bytes(b'')
        (root / 'authority.log').write_bytes(b'')
        binary = os.environ.get('CAFE_AUTHORITY_FIXTURE')
        if not binary or not Path(binary).is_file():
            raise AssertionError('test authority binary absent; build approved fixture first')
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / 'connectors'), PYTHONDONTWRITEBYTECODE='1')
        self.process = subprocess.Popen([binary, sys.executable, str(root / 'sidecar.py'), str(root)], cwd=root, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._owned_child = self.process
        self._owned_pid = self.process.pid
        try:
            self.ready = self.read()
            if self.ready.get('ready') is not True:
                raise AssertionError('test authority startup failed')
        except BaseException:
            if self.process.poll() is None:
                self._signal_owned(False)
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_owned(True)
                self.process.communicate()
            finally:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            raise

    def _signal_owned(self, hard):
        process = self.process
        pid = process.pid
        if (process is not self._owned_child or not isinstance(pid, int) or
                pid <= 1 or pid == os.getpid() or pid != self._owned_pid):
            raise AssertionError('signal_target_not_owned_child')
        if process.poll() is None:
            if hard:
                process.kill()
            else:
                process.terminate()

    def read(self):
        # Windows select cannot wait on a subprocess pipe. This bounded worker
        # reads only this owned child's response, and close() reaps before join.
        responses = queue.Queue(maxsize=1)
        def receive():
            try:
                responses.put(self.process.stdout.readline(auth.MAX_LINE + 1))
            except (OSError, ValueError):
                responses.put(b'')
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            line = responses.get(timeout=10)
        except queue.Empty:
            raise AssertionError('authority response deadline') from None
        reader.join(timeout=1)
        self.output.append(line)
        with (self.root / 'authority.log').open('ab') as log:
            log.write(line)
        if not line or len(line) > auth.MAX_LINE:
            raise AssertionError('authority response invalid')
        return json.loads(line)

    def call(self, **message):
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        self.process.stdin.flush()
        return self.read()

    def broker_start(self, bind='127.0.0.1'):
        response = self.call(op='broker_start', bind=bind)
        return response

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.call(op='shutdown')
                finally:
                    self.process.stdin.close()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._signal_owned(True)
                        self.process.wait(timeout=5)
            self.output.extend([self.process.stdout.read(), self.process.stderr.read()])
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


class TestAuthorityChannel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-channel-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.canary = 'oauth:' + secrets.token_urlsafe(36)
        self.a = Authority(self.root)
        self.addCleanup(self._close_and_scan)

    def _close_and_scan(self):
        try:
            self.a.close()
        finally:
            assert_canary_absent(self.root, self.a.output, self.canary)

    def ticket(self):
        state = self.a.broker_start()['ok']
        self.assertEqual(state['host'], '127.0.0.1')
        ticket = asyncio.run(auth.receive_ticket(self.root / 'rendezvous-1'))
        return ticket

    def test_rendezvous_platform_permissions_are_explicit_before_ticket(self):
        self.a.broker_start()
        directory = self.root / 'rendezvous-1'
        if os.name == 'nt':
            from cafe.windows_auth import _api, _owner, _check_acl
            kernel, security = _api()
            storage, sid = _owner(kernel, security)
            _check_acl(directory, True, kernel, security, sid)
            _check_acl(directory / 'broker.pipe', False, kernel, security, sid)
            self.assertGreater(len(storage), 0)
        elif os.name == 'posix':
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / 'broker.sock').stat().st_mode), 0o600)
        else:
            self.fail('unsupported private transport platform')

    def test_real_child_and_independent_broker_positive(self):
        initial = self.a.ready['connector']
        self.assertTrue(initial['available'])
        self.assertEqual(initial['authority_epoch'], '1')
        self.assertGreater(initial['pid'], 0)
        response = self.a.call(op='effect', action='effect')
        self.assertEqual(response['ok']['body'], {'accepted': True})
        self.assertEqual((self.root / 'effect.marker').read_text(), 'effect\n')
        self.a.broker_start()
        result = subprocess.run([sys.executable, str(self.root / 'broker.py'), str(self.root / 'rendezvous-1'), 'snapshot'], cwd=self.root, env=self.a.env, capture_output=True, timeout=10)
        self.a.output += [result.stdout, result.stderr]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'accepted': True, 'effects': 1})
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 1)

    def test_wrong_capability_before_any_effect(self):
        frame = auth.make_frame('approved_effect', 1, {'action': 'effect'})
        self.assertIn('error', self.a.call(op='accept_connector', frame=frame))
        self.assertEqual(self.a.call(op='status')['ok']['admitted'], 0)
        self.assertFalse((self.root / 'effect.marker').exists())
        ticket = self.ticket()
        response = asyncio.run(auth.connect_broker(ticket, 'confirm'))
        self.assertEqual(response, {'error': 'capability_denied'})
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 0)

    def test_wildcard_bind_before_listener_or_rendezvous(self):
        self.assertIn('error', self.a.broker_start('0.0.0.0'))
        self.assertFalse((self.root / 'rendezvous-1').exists())
        self.assertIsNone(self.a.call(op='status')['ok']['broker'])

    def test_eof_restart_revokes_previous_connector_epoch(self):
        first = self.a.ready['connector']
        self.assertIn('error', self.a.call(op='effect', action='exit'))
        self.assertFalse(self.a.call(op='status')['ok']['connector']['available'])
        second = self.a.call(op='restart')['ok']
        self.assertTrue(second['available'])
        self.assertEqual(second['authority_epoch'], '2')
        self.assertNotEqual(first['pid'], second['pid'])
        old = auth.make_frame('observation', 1, {'ready': True})
        self.assertIn('error', self.a.call(op='accept_connector', frame=old))
        self.assertEqual(self.a.call(op='status')['ok']['admitted'], 0)
        self.assertIn('error', self.a.call(op='effect', action='old_epoch'))
        self.assertFalse((self.root / 'effect.marker').exists())

    def test_failed_restart_reserves_epoch_before_child_launch(self):
        # A child that started and failed its handshake already consumed an epoch.
        script = self.root / 'sidecar.py'
        script.write_text('raise SystemExit(0)\n')
        self.assertIn('error', self.a.call(op='restart'))
        failed = self.a.call(op='status')['ok']['connector']
        self.assertFalse(failed['available'])
        self.assertEqual(failed['authority_epoch'], '2')
        script.write_text(SIDECAR)
        recovered = self.a.call(op='restart')['ok']
        self.assertTrue(recovered['available'])
        self.assertEqual(recovered['authority_epoch'], '3')

    def test_private_token_lease_never_journal_log_or_broker(self):
        journal = (self.root / 'journal.jsonl').read_bytes()
        for sink in ('journal', 'log', 'broker'):
            self.assertIn('error', self.a.call(op='lease', token=self.canary, ttl_ms=1000, sink=sink))
        self.assertEqual((self.root / 'journal.jsonl').read_bytes(), journal)
        self.assertFalse((self.root / 'effect.marker').exists())
        good = self.a.call(op='lease', token=self.canary, ttl_ms=1000, sink='private')
        self.assertEqual(good['ok']['body'], {'accepted': True})
        assert_canary_absent(self.root, self.a.output, self.canary)
        self.assertIn('error', self.a.call(op='lease', token=self.canary, ttl_ms=5001, sink='private'))
        contaminated = self.root / 'copied_bad_log'
        contaminated.write_bytes(self.canary.encode())
        try:
            with self.assertRaisesRegex(AssertionError, 'private canary leaked'):
                assert_canary_absent(self.root, [], self.canary)
        finally:
            contaminated.unlink()

    def test_unauthenticated_local_client_before_effect(self):
        state = self.a.broker_start()['ok']
        with socket.create_connection(('127.0.0.1', state['port']), timeout=5) as client:
            stream = client.makefile('rwb')
            challenge = json.loads(stream.readline(auth.MAX_LINE + 1))
            self.assertEqual(challenge['identity'], 'broker')
            stream.write(b'{"epoch":"1","capability":"propose","body":{}}\n')
            stream.flush()
            self.assertEqual(stream.readline(auth.MAX_LINE + 1), b'')
            stream.close()
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 0)

    def test_hmac_connection_replay_and_wrong_identity(self):
        ticket = self.ticket()
        with self.assertRaises(ValueError):
            asyncio.run(auth.connect_broker(ticket, identity='connector'))
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 0)
        # Malformed identity did not consume a valid authenticated ticket.
        self.assertEqual(asyncio.run(auth.connect_broker(ticket)), {'accepted': True, 'effects': 1})
        with self.assertRaises(ValueError):
            asyncio.run(auth.connect_broker(ticket))
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 1)

    def test_rendezvous_owner_acl_and_one_time_bootstrap(self):
        self.a.broker_start()
        directory = self.root / 'rendezvous-1'
        if os.name == 'nt':
            from cafe.windows_auth import _api, _owner, _check_acl
            kernel, security = _api()
            storage, sid = _owner(kernel, security)
            _check_acl(directory, True, kernel, security, sid)
            _check_acl(directory / 'broker.pipe', False, kernel, security, sid)
            self.assertNotIn(b'secret', (directory / 'broker.pipe').read_bytes())
        else:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / 'broker.sock').stat().st_mode), 0o600)
        asyncio.run(auth.receive_ticket(directory))
        with self.assertRaises((OSError, ValueError, asyncio.TimeoutError)):
            asyncio.run(auth.receive_ticket(directory))
        self.assertEqual(self.a.call(op='status')['ok']['broker']['effects'], 0)

    def test_bounded_pipes_and_separate_stderr_drain(self):
        self.assertEqual(self.a.call(op='effect', action='stderr')['ok']['body'], {'accepted': True})
        self.assertIn('error', self.a.call(op='effect', action='giant'))
        self.assertFalse(self.a.call(op='status')['ok']['connector']['available'])
        self.assertFalse((self.root / 'effect.marker').exists())

    def test_closed_python_decoder_and_expired_lease(self):
        frame = auth.make_frame('observation', 1, {'ready': True})
        self.assertEqual(auth.validate_frame(frame, 1, False), 1)
        for change in ({'extra': True}, {'authority_epoch': 1}, {'authority_epoch': '01'}, {'kind': 'approved_effect'}):
            mutant = dict(frame, **change)
            with self.assertRaises(ValueError):
                auth.validate_frame(mutant, 1, False)
        with self.assertRaises(ValueError):
            auth.validate_lease({'token': self.canary, 'expires_unix_ms': '1'})


if __name__ == '__main__':
    unittest.main()
