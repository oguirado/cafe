"""Independent read-only IRC and durable JSONL negatives using a real local peer."""
import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import stat
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import twitch_storage as storage


def event(text='literal', day='2000-01-01'):
    return {'schema': 'cafe.chat_event.v1', 'channel': 'test_channel',
            'received_at': {'monotonic_ns': '7', 'utc': day + 'T00:00:00Z'},
            'sent_at': None, 'message_id': None, 'user_login': None,
            'display_name': None, 'badges': [], 'text': text,
            'kind': 'notice', 'session_id': 'session-a'}


class StorageSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-chat-')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)

    def writer(self, *args, **kwargs):
        value = storage.JsonlWriter(self.root, *args, **kwargs)
        def cleanup():
            try:
                value.close()
            except storage.ChatError:
                pass  # The test body checks explicit storage failures.
        self.addCleanup(cleanup)
        return value

    def test_channel_canonical_and_invalid_rejected(self):
        self.assertEqual(storage.validate_channel('Test_Channel'), 'test_channel')
        for value in ['ab', '../escape', 'x' * 26, 'space here', True, 'ümlaut']:
            with self.subTest(value=value), self.assertRaises(storage.ChatError):
                storage.validate_channel(value)

    def test_url_allowlist_is_exact_with_verified_tls_default(self):
        self.assertEqual(storage.validate_url(storage.OFFICIAL_URL), storage.OFFICIAL_URL)
        self.assertEqual(storage.validate_url('ws://127.0.0.1:1234'), 'ws://127.0.0.1:1234')
        for url in ['wss://example.invalid:443', 'ws://irc-ws.chat.twitch.tv:443',
                    'wss://irc-ws.chat.twitch.tv.evil.invalid:443',
                    'wss://irc-ws.chat.twitch.tv:443' + '@' + 'elsewhere.invalid',
                    'ws://localhost:1234', 'ws://127.0.0.1:1234/other',
                    'wss://irc-ws.chat.twitch.tv:443?token=synthetic']:
            with self.subTest(url=url), self.assertRaises(storage.ChatError):
                storage.validate_url(url)

    def test_flush_count_time_and_close_are_durable(self):
        now = [0.0]
        with mock.patch.object(storage.os, 'fsync', wraps=storage.os.fsync) as sync:
            writer = self.writer('test_channel', sync_events=2,
                                         sync_seconds=2, clock=lambda: now[0])
            writer.append(event()); self.assertEqual(sync.call_count, 0)
            writer.append(event()); self.assertEqual(sync.call_count, 1)
            writer.append(event()); now[0] = 3
            writer.flush_due(); self.assertEqual(sync.call_count, 2)
            writer.append(event()); writer.close(); self.assertEqual(sync.call_count, 3)
        self.assertTrue(writer.closed)

    def test_fsync_failure_is_error_never_durable_success(self):
        writer = self.writer('test_channel', sync_events=1)
        with mock.patch.object(storage.os, 'fsync', side_effect=OSError('synthetic_failure')):
            with self.assertRaises(storage.ChatError):
                writer.append(event())
            with self.assertRaises(storage.ChatError):
                writer.close()
        self.assertTrue(writer.closed)
        self.assertEqual(writer.syncs, 0)

    def test_partial_write_is_completed_and_zero_write_fails(self):
        original = os.write
        def partial(fd, data):
            return original(fd, data[:3])
        writer = self.writer('test_channel', sync_events=1)
        with mock.patch.object(storage.os, 'write', side_effect=partial):
            writer.append(event())
        with mock.patch.object(storage.os, 'write', return_value=0):
            with self.assertRaises(storage.ChatError):
                writer.append(event())
        with self.assertRaises(storage.ChatError):
            writer.close()
        self.assertTrue(writer.poisoned)
        records = list(storage.read_jsonl_file(self.root / 'twitch/test_channel/2000-01-01.jsonl'))
        self.assertEqual(records, [event()])

    def test_day_rotation_preserves_all_records(self):
        with self.writer('test_channel') as writer:
            writer.append(event(day='2000-01-01')); writer.append(event(day='2000-01-02'))
        self.assertEqual(sorted(p.name for p in (self.root / 'twitch/test_channel').iterdir()),
                         ['2000-01-01.jsonl', '2000-01-02.jsonl'])

    def test_path_escape_rejected_without_external_write(self):
        # Ancestor aliases must fail even when the leaf has a regular stat.
        fake = SimpleNamespace(is_symlink=lambda: False, is_dir=lambda: True,
                               resolve=lambda: 'outside', absolute=lambda: 'selected',
                               lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_ino=7, st_dev=1, st_file_attributes=0))
        with self.assertRaises(storage.ChatError):
            storage._directory(fake)
        with self.assertRaises(storage.ChatError):
            self.writer('../outside')
        if os.name != 'nt':
            with tempfile.TemporaryDirectory(prefix='cafe-outside-') as outside:
                (self.root / 'twitch').symlink_to(outside, target_is_directory=True)
                with self.assertRaises(storage.ChatError):
                    self.writer('test_channel')
                self.assertEqual(list(Path(outside).iterdir()), [])

    def test_torn_tail_is_ignored_and_not_repaired(self):
        with self.writer('test_channel') as writer:
            writer.append(event())
        path = self.root / 'twitch/test_channel/2000-01-01.jsonl'
        with path.open('ab') as stream:
            stream.write(b'{"torn":')
        before = path.read_bytes(); stats = {'invalid': 0, 'truncated': 0}
        self.assertEqual(list(storage.read_jsonl_file(path, stats)), [event()])
        self.assertEqual(stats['truncated'], 1)
        self.assertEqual(path.read_bytes(), before)
        writer = self.writer('test_channel')
        try:
            with self.assertRaises(storage.ChatError):
                writer.append(event())
        finally:
            writer.close()
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_event_keys_and_invalid_clock_are_rejected(self):
        for change in [lambda x: x.update(access_token='synthetic'),
                       lambda x: x['received_at'].update(monotonic_ns=7),
                       lambda x: x.update(kind=['message'])]:
            value = event(); change(value)
            with self.assertRaises(storage.ChatError):
                storage.validate_event(value)

    def test_source_module_caller_rejects_invalid_channel_before_network(self):
        env = dict(os.environ); env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-B', '-m', 'connectors.cafe.twitch_storage',
                                 '--channel', '../escape', '--data', str(self.root)],
                                cwd=ROOT, env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b''); self.assertEqual(result.stderr, b'')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_write_error_poisoned_writer_never_acknowledges_later_record(self):
        writer = self.writer('test_channel')
        original = os.write; calls = [0]
        def interrupted(fd, data):
            calls[0] += 1
            if calls[0] == 1:
                return original(fd, data[:3])
            raise OSError('synthetic_interrupted_write')
        with mock.patch.object(storage.os, 'write', side_effect=interrupted):
            with self.assertRaises(storage.ChatError):
                writer.append(event('first'))
        path = self.root / 'twitch/test_channel/2000-01-01.jsonl'
        partial_bytes = path.read_bytes()
        self.assertEqual(len(partial_bytes), 3)
        with self.assertRaises(storage.ChatError):
            writer.append(event('must never be acknowledged'))
        with self.assertRaises(storage.ChatError):
            writer.close()
        self.assertTrue(writer.poisoned); self.assertTrue(writer.closed)
        self.assertIsNone(writer._fd)
        self.assertEqual(path.read_bytes(), partial_bytes)
        reopened = self.writer('test_channel')
        try:
            with self.assertRaises(storage.ChatError):
                reopened.append(event('new instance must refuse torn day file'))
        finally:
            reopened.close()
        self.assertIsNone(reopened._fd)
        self.assertEqual(path.read_bytes(), partial_bytes)
        stats = {'invalid': 0, 'truncated': 0}
        self.assertEqual(list(storage.read_jsonl_file(path, stats)), [])
        self.assertEqual(stats['truncated'], 1)

    def test_elapsed_and_close_fsync_failures_poison_and_release_descriptor(self):
        for failure in ['elapsed', 'close']:
            with self.subTest(failure=failure):
                now = [0.0]
                writer = self.writer('test_channel', clock=lambda: now[0])
                writer.append(event(failure))
                path = self.root / 'twitch/test_channel/2000-01-01.jsonl'
                before = path.read_bytes(); now[0] = 3.0
                with mock.patch.object(storage.os, 'fsync', side_effect=OSError('synthetic_sync_failure')):
                    with self.assertRaises(storage.ChatError):
                        writer.flush_due() if failure == 'elapsed' else writer.close()
                self.assertTrue(writer.poisoned); self.assertTrue(writer.closed)
                self.assertIsNone(writer._fd)
                with self.assertRaises(storage.ChatError):
                    writer.append(event('must refuse after failed durability'))
                with self.assertRaises(storage.ChatError):
                    writer.close()
                self.assertEqual(path.read_bytes(), before)

    def test_special_file_open_is_nonblocking_and_regular_on_every_platform(self):
        writer = self.writer('test_channel')
        original = os.open; flags = []
        def observed(path, value, *args, **kwargs):
            flags.append(value)
            return original(path, value, *args, **kwargs)
        with mock.patch.object(storage.os, 'open', side_effect=observed):
            writer.append(event())
        writer.close()
        if hasattr(os, 'O_NONBLOCK'):
            self.assertTrue(all(value & os.O_NONBLOCK for value in flags))
        path = self.root / 'twitch/test_channel/2000-01-01.jsonl'
        special = SimpleNamespace(st_mode=stat.S_IFIFO, st_ino=7, st_dev=1)
        with mock.patch.object(storage.os, 'fstat', return_value=special):
            with self.assertRaises(storage.ChatError):
                storage._open_file(path, os.O_RDONLY)
        windows_stat = SimpleNamespace(S_ISREG=stat.S_ISREG, S_ISDIR=stat.S_ISDIR,
                                       FILE_ATTRIBUTE_REPARSE_POINT=1024)
        with mock.patch.object(storage, 'os', SimpleNamespace(name='nt')), \
             mock.patch.object(storage, 'stat', windows_stat):
            info = SimpleNamespace(st_mode=stat.S_IFREG, st_ino=7, st_dev=1, st_file_attributes=1024)
            with self.assertRaises(storage.ChatError):
                storage._safe_stat(info)
            info.st_file_attributes = 0; storage._safe_stat(info)
        if hasattr(os, 'mkfifo'):
            path.unlink(); os.mkfifo(path)
            program = ("from cafe.twitch_storage import JsonlWriter,ChatError; import sys; "
                       "w=JsonlWriter(sys.argv[1],'test_channel'); "
                       "e={'schema':'cafe.chat_event.v1','channel':'test_channel',"
                       "'received_at':{'monotonic_ns':'7','utc':'2000-01-01T00:00:00Z'},"
                       "'sent_at':None,'message_id':None,'user_login':None,'display_name':None,"
                       "'badges':[],'text':'synthetic','kind':'notice','session_id':'session-a'}; "
                       "\ntry: w.append(e)\nexcept ChatError: print('special file rejected')\nelse: raise AssertionError('FIFO accepted')\nfinally: w.close()")
            env = dict(os.environ); env['PYTHONPATH'] = str(ROOT / 'connectors')
            result = subprocess.run([sys.executable, '-B', '-c', program, str(self.root)],
                                    cwd=ROOT, env=env, capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'special file rejected\n')

    def test_source_module_caller_closes_canonical_domain_error(self):
        program = ("import runpy,sys,warnings; from connectors.cafe import twitch_chat; "
                   "warnings.filterwarnings('ignore', category=RuntimeWarning, "
                   "message=\".*connectors.cafe.twitch_storage.*found in sys.modules.*\"); "
                   "\nasync def rejected(*args, **kwargs): raise twitch_chat.ChatError('network_failed')"
                   "\ntwitch_chat.run_chat=rejected"
                   "\nsys.argv=['cafe-chat','--channel','test_channel','--data',sys.argv[1]]"
                   "\nrunpy.run_module('connectors.cafe.twitch_storage', run_name='__main__')")
        env = dict(os.environ); env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.root)],
                                cwd=ROOT, env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b''); self.assertEqual(result.stderr, b'')
        self.assertEqual(list(self.root.iterdir()), [])


class IrcPeer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cafe import twitch_chat
        self.chat = twitch_chat
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-chat-peer-')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)

    async def test_privmsg_and_injection_sends_blocked_before_peer(self):
        peer = mock.AsyncMock()
        for command in ['PRIVMSG #test_channel :forbidden', 'PASS synthetic',
                        'JOIN #test_channel\r\nPRIVMSG #test_channel :bad']:
            with self.assertRaises(storage.ChatError):
                await storage.readonly_send(peer, command)
        peer.send.assert_not_awaited()

    async def test_tls_and_proxy_defaults_cannot_be_overridden(self):
        with mock.patch.object(storage, '_ExactConnect', new=mock.AsyncMock()) as connect:
            await storage.open_connection(storage.OFFICIAL_URL)
        args, kwargs = connect.call_args
        self.assertEqual(args, (storage.OFFICIAL_URL,))
        self.assertNotIn('ssl', kwargs)
        self.assertIsNone(kwargs['proxy'])
        self.assertIsInstance(storage._ExactConnect.process_redirect(None, None), storage.ChatError)

    async def test_real_peer_login_ping_tags_and_jsonl(self):
        from websockets.asyncio.server import serve
        received = []
        async def peer(ws):
            for _ in range(3):
                received.append(await ws.recv())
            await ws.send('PING :synthetic\r\n')
            received.append(await ws.recv())
            await ws.send('@id=message-a;tmi-sent-ts=946684800000;display-name=Alice\\sA;badges=subscriber/1 :alice!alice@synthetic PRIVMSG #test_channel :hello literal\r\n')
            await ws.close()
        async with serve(peer, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await self.chat.run_chat('test_channel', self.root,
                      url='ws://127.0.0.1:' + str(port), max_connections=1)
        self.assertRegex(received[0], r'^NICK justinfan[0-9]+\r\n$')
        self.assertEqual(received[1], 'CAP REQ :twitch.tv/tags twitch.tv/commands\r\n')
        self.assertEqual(received[2], 'JOIN #test_channel\r\n')
        self.assertEqual(received[3], 'PONG :synthetic\r\n')
        self.assertFalse(any('PASS' in x or 'PRIVMSG' in x for x in received))
        records = [e for p in self.root.glob('twitch/test_channel/*.jsonl') for e in storage.read_jsonl_file(p)]
        messages = [e for e in records if e['kind'] == 'message']
        self.assertEqual(len(messages), 1); self.assertEqual(messages[0]['text'], 'hello literal')
        self.assertEqual(messages[0]['display_name'], 'Alice A')
        self.assertEqual(messages[0]['badges'], [{'name': 'subscriber', 'version': '1'}])
        self.assertEqual(messages[0]['sent_at'], '946684800000')
        self.assertEqual(result['messages'], 1)

    async def test_malformed_tags_counted_without_crash(self):
        from websockets.asyncio.server import serve
        async def peer(ws):
            for _ in range(3):
                await ws.recv()
            await ws.send('@=bad :alice!alice@synthetic PRIVMSG #test_channel :bad\r\n')
            await ws.send(':alice!alice@synthetic PRIVMSG #test_channel :good\r\n')
            await ws.close()
        async with serve(peer, '127.0.0.1', 0) as server:
            result = await self.chat.run_chat('test_channel', self.root,
                     url='ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1]), max_connections=1)
        self.assertEqual(result['malformed'], 1); self.assertEqual(result['messages'], 1)

    async def test_reconnect_uses_bounded_backoff_and_new_connection(self):
        from websockets.asyncio.server import serve
        count = [0]; delays = []
        async def sleep(delay):
            delays.append(delay)
        async def peer(ws):
            for _ in range(3):
                await ws.recv()
            count[0] += 1
            if count[0] == 1:
                await ws.send('RECONNECT\r\n')
            else:
                await ws.send(':alice!alice@synthetic PRIVMSG #test_channel :after reconnect\r\n')
            await ws.close()
        async with serve(peer, '127.0.0.1', 0) as server:
            result = await self.chat.run_chat('test_channel', self.root,
                     url='ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1]),
                     max_connections=2, sleep=sleep, backoff_initial=0.01, backoff_max=0.02)
        self.assertEqual(count[0], 2); self.assertEqual(result['connections'], 2)
        self.assertTrue(delays); self.assertTrue(all(0 <= d <= 0.02 for d in delays))
        self.assertEqual(result['messages'], 1)

    async def test_repeated_peer_disconnects_grow_exponential_backoff(self):
        from websockets.asyncio.server import serve
        delays = []
        async def sleep(delay):
            delays.append(delay)
        async def peer(ws):
            for _ in range(3):
                await ws.recv()
            await ws.close()
        async with serve(peer, '127.0.0.1', 0) as server:
            with mock.patch.object(self.chat.random, 'uniform', return_value=0):
                result = await self.chat.run_chat('test_channel', self.root,
                         url='ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1]),
                         max_connections=4, sleep=sleep, backoff_initial=0.01, backoff_max=0.08)
        self.assertEqual(result['connections'], 4)
        self.assertEqual(delays, [0.01, 0.02, 0.04])

    async def test_invalid_run_options_rejected_before_network(self):
        with mock.patch.object(self.chat, 'open_connection', new=mock.AsyncMock()) as connect:
            for kwargs in [{'max_connections': 0}, {'max_connections': -1},
                           {'backoff_initial': 0}, {'backoff_max': float('inf')},
                           {'backoff_initial': True}, {'session_id': '../invalid'}]:
                with self.subTest(kwargs=kwargs), self.assertRaises(storage.ChatError):
                    await self.chat.run_chat('test_channel', self.root, **kwargs)
            connect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
