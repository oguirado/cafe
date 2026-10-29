"""Real private-pipe child and local OBS; never a live account/service."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve as websocket_server
from test_obs_bridge import FakeOBS, FEEDS, quiet_logger
from connectors.cafe.runtime_service_host import ServiceDispatcher, decode_private, private_root

ROOT = Path(__file__).resolve().parents[2]
CANARY = "SYNTHETIC_OBS_SERVICE_PASSWORD"


@contextmanager
def private_directory():
    """Exercise the actual platform policy; never disable its ACL validation."""
    with tempfile.TemporaryDirectory(prefix="cafe-service-") as directory:
        root = Path(directory).resolve()
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            from connectors.cafe import windows_auth
            kernel, security = windows_auth._api()
            owner_storage, sid = windows_auth._owner(kernel, security)
            security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
            security.ConvertSidToStringSidW.restype = wintypes.BOOL
            security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
            security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
            security.SetFileSecurityW.restype = wintypes.BOOL
            text, descriptor = wintypes.LPWSTR(), ctypes.c_void_p()
            try:
                if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                    raise AssertionError("private_fixture_owner_failed")
                policy = "O:" + text.value + "D:P(A;OICI;FA;;;" + text.value + ")"
                if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(policy, 1, ctypes.byref(descriptor), None):
                    raise AssertionError("private_fixture_descriptor_failed")
                if not security.SetFileSecurityW(str(root), 0x80000005, descriptor):
                    raise AssertionError("private_fixture_acl_failed")
                windows_auth._check_acl(root, True, kernel, security, sid)
            finally:
                if descriptor.value:
                    kernel.LocalFree(descriptor)
                if text:
                    kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
                del owner_storage
        yield str(root)


def frame(kind, body, epoch="9007199254740993"):
    return {"schema": "cafe.connector.v1", "kind": kind, "request_id": "channel",
            "authority_epoch": epoch, "expected_revision": "0", "body": body}


def raw(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_rust_supervisor_owns_real_python_obs_child(self):
        import test_authority_channel as fixture
        from cafe import obs_auth
        obs = FakeOBS(password=CANARY)
        with private_directory() as directory:
            root = Path(directory)
            script = ("import sys;sys.path.insert(0," + repr(str(ROOT / 'connectors')) + ");"
                      "from cafe.runtime_service_host import main;sys.argv=[sys.argv[0]," + repr(str(root)) + "];main()")
            async with websocket_server(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
                with patch.object(fixture, "SIDECAR", script):
                    authority = await asyncio.to_thread(fixture.Authority, root)
                try:
                    owned_pid = authority.ready["connector"]["pid"]
                    self.assertGreater(owned_pid, 1)
                    self.assertNotEqual(owned_pid, authority.process.pid)
                    result = await asyncio.to_thread(authority.call, op="service", body={"op": "configure_obs", "port": server.sockets[0].getsockname()[1], "password": {"mode": "provided", "value": CANARY}})
                    self.assertTrue(result["ok"]["body"]["accepted"])
                    crops = {"game": {"x": 0, "y": 0, "width": 1280, "height": 720}, "camera": {"x": 10, "y": 20, "width": 320, "height": 240}}
                    result = await asyncio.to_thread(authority.call, op="service", body={"op": "send_capsule", "capsule_id": "guest", "feeds": FEEDS, "crops": crops})
                    self.assertTrue(result["ok"]["body"]["accepted"])
                    self.assertEqual(len(obs.inputs), 2)
                    wrong = await asyncio.to_thread(authority.call, op="service", body={"op": "configure_obs", "host": "0.0.0.0", "port": 9, "password": {"mode": "absent"}})
                    self.assertIn("error", wrong)
                    self.assertEqual(len(obs.inputs), 2)
                finally:
                    await asyncio.to_thread(authority.close)
                values = b"".join(x if isinstance(x, bytes) else json.dumps(x).encode() for x in authority.output)
                self.assertNotIn(CANARY.encode(), values)
                self.assertEqual((root / "journal.jsonl").read_bytes(), b"")
                observed_log = (root / "authority.log").read_bytes()
                self.assertNotIn(CANARY.encode(), observed_log)
                self.assertNotIn(b'"password"', observed_log)
                self.assertNotIn(b'"feeds"', observed_log)

    @asynccontextmanager
    async def child(self, directory):
        bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
                     "from cafe.runtime_service_host import main;"
                     "sys.argv=[sys.argv[0],sys.argv[2]];main()")
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", "-c", bootstrap,
                    str(ROOT / "connectors"), str(directory), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.assertGreater(process.pid, 1)
        self.assertNotEqual(process.pid, os.getpid())
        try:
            yield process
        finally:
            if process.returncode is None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    # The live subprocess object is the sole known child target.
                    self.assertGreater(process.pid, 1)
                    self.assertNotEqual(process.pid, os.getpid())
                    process.kill()
                    await process.wait()
            error = await process.stderr.read()
            self.assertNotIn(CANARY.encode(), error)
            self.assertEqual(error, b"")

    async def exchange(self, process, value):
        process.stdin.write(raw(value))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        self.assertTrue(line.endswith(b"\n"))
        self.assertNotIn(CANARY.encode(), line)
        return json.loads(line)

    async def start(self, process):
        result = await self.exchange(process, frame("connector_start", {}))
        self.assertEqual(result, frame("observation", {"ready": True}))

    async def test_real_child_authenticated_obs_idempotence_two_windows_and_no_secret_output(self):
        obs = FakeOBS(password=CANARY)
        with private_directory() as directory:
            async with websocket_server(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
                port = server.sockets[0].getsockname()[1]
                async with self.child(directory) as process:
                    await self.start(process)
                    answer = await self.exchange(process, frame("service_request", {
                        "op": "configure_obs", "port": port, "password": {"mode": "provided", "value": CANARY}}))
                    self.assertTrue(answer["body"]["accepted"])
                    self.assertEqual(answer["body"]["obs"], "configured")
                    for _ in range(2):
                        answer = await self.exchange(process, frame("service_request", {
                            "op": "send_capsule", "capsule_id": "guest", "feeds": FEEDS, "crops": {"game": {"x": 0, "y": 0, "width": 1280, "height": 720}, "camera": {"x": 10, "y": 20, "width": 320, "height": 240}}}))
                        self.assertTrue(answer["body"]["accepted"])
                    self.assertEqual(len(obs.inputs), 2)
                    self.assertEqual(obs.scenes, {"Café"})
                    answer = await self.exchange(process, frame("service_request", {"op": "close"}))
                    self.assertTrue(answer["body"]["accepted"])
                    self.assertEqual(answer["body"]["obs"], "closed")
                    self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertNotIn(CANARY, repr(obs.requests))

    async def test_wrong_obs_password_never_admits_effect(self):
        obs = FakeOBS(password=CANARY)
        with private_directory() as directory:
            async with websocket_server(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
                async with self.child(directory) as process:
                    await self.start(process)
                    answer = await self.exchange(process, frame("service_request", {
                        "op": "configure_obs", "port": server.sockets[0].getsockname()[1],
                        "password": {"mode": "provided", "value": "incorrect-synthetic-password"}}))
                    self.assertFalse(answer["body"]["accepted"])
                    self.assertEqual(answer["body"]["obs"], "absent")
                    self.assertEqual(obs.effects, [])

    async def test_stale_epoch_unknown_body_and_nonloopback_fail_before_obs_connection(self):
        obs = FakeOBS()
        with private_directory() as directory:
            async with websocket_server(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
                base = {"op": "configure_obs", "port": server.sockets[0].getsockname()[1], "password": {"mode": "absent"}}
                cases = [frame("service_request", base, "1"), frame("service_request", {**base, "host": "0.0.0.0"}),
                         frame("service_request", {**base, "extra": CANARY}), frame("approved_effect", {"action": "configure_obs"})]
                for value in cases:
                    async with self.child(directory) as process:
                        await self.start(process)
                        process.stdin.write(raw(value))
                        await process.stdin.drain()
                        self.assertEqual(await asyncio.wait_for(process.wait(), 3), 2)
                        self.assertEqual(await process.stdout.read(), b"")
                self.assertEqual(obs.accepted, 0)

    async def test_chat_factory_is_private_path_bound_and_unknown_not_zero(self):
        calls = []
        async def producer(channel, directory, stop, progress):
            calls.append((channel, directory))
            await stop.wait()
            return {"messages": 2, "connections": 1}
        with private_directory() as directory:
            root = Path(directory).resolve()
            dispatcher = ServiceDispatcher(root, producer=producer)
            result = await dispatcher.dispatch({"op": "start_chat", "channel": "synthetic_channel"})
            self.assertTrue(result["accepted"])
            self.assertEqual(result["chat"], "running")
            self.assertIsNone(result["connections"])
            duplicate = await dispatcher.dispatch({"op": "start_chat", "channel": "other"})
            self.assertFalse(duplicate["accepted"])
            final = await dispatcher.dispatch({"op": "close"})
            self.assertEqual(calls, [("synthetic_channel", root)])
            self.assertEqual(final["messages"], "2")
            self.assertEqual(final["connections"], "1")
            self.assertIsNone(final["malformed"])

    async def test_real_chat_progress_means_observed_channel_and_clears_on_disconnect(self):
        from cafe.twitch_chat import run_chat
        from cafe.twitch_storage import read_jsonl_file
        observations = []
        observed = asyncio.Event()
        stop = asyncio.Event()
        def progress(value):
            observations.append(dict(value))
            if value['connected']:
                observed.set()
        async def peer(socket):
            for _ in range(3):
                await socket.recv()
            await socket.send(':guest!guest@synthetic PRIVMSG #test_channel :synthetic text\r\n')
            await observed.wait()
            await stop.wait()
        with private_directory() as directory:
            async with websocket_server(peer, '127.0.0.1', 0, logger=quiet_logger()) as server:
                port = server.sockets[0].getsockname()[1]
                task = asyncio.create_task(run_chat('test_channel', directory, url='ws://127.0.0.1:'+str(port), max_connections=1, stop_event=stop, progress=progress))
                try:
                    await asyncio.wait_for(observed.wait(), 3)
                    self.assertFalse(task.done())
                    self.assertTrue(observations[-1]['connected'])
                    self.assertEqual(observations[-1]['messages'], 1)
                    self.assertNotIn('synthetic text', str(observations))
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 3)
            self.assertFalse(observations[-1]['connected'])
            events = [row for file in Path(directory).glob('twitch/test_channel/*.jsonl') for row in read_jsonl_file(file)]
            self.assertEqual(len(events), 1)

    async def test_bad_progress_callback_still_closes_transport(self):
        from cafe.twitch_chat import run_chat
        from cafe.twitch_storage import ChatError
        closed = asyncio.Event()
        async def peer(socket):
            try:
                for _ in range(3):
                    await socket.recv()
                await socket.wait_closed()
            finally:
                closed.set()
        def failed(_value):
            raise RuntimeError(CANARY)
        with private_directory() as directory:
            async with websocket_server(peer, '127.0.0.1', 0, logger=quiet_logger()) as server:
                with self.assertRaisesRegex(ChatError, '^storage_failed$'):
                    await run_chat('test_channel', directory, url='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]), max_connections=1, progress=failed)
                await asyncio.wait_for(closed.wait(), 3)

    async def test_duplicate_json_missing_bootstrap_and_oversized_input_fail_closed(self):
        for value, epoch in [(b'{"kind":"a","kind":"b"}\n', None),
                             (raw(frame("service_request", {"op": "status"})), None),
                             (b"x" * 16385 + b"\n", 7), (b'{"value":NaN}\n', 7)]:
            with self.assertRaisesRegex(ValueError, "^private_frame_rejected$"):
                decode_private(value, epoch)

    async def test_private_root_must_be_real_existing_directory(self):
        with self.assertRaises((OSError, ValueError)):
            private_root(Path("relative"))
        with private_directory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(private_root(root), root)
            if os.name == "posix":
                root.chmod(0o755)
                try:
                    with self.assertRaisesRegex(ValueError, "private_service_root_rejected"):
                        private_root(root)
                finally:
                    root.chmod(0o700)
