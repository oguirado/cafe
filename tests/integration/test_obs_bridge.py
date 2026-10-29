"""Real loopback OBS v5 server, independent state and secret-canary oracles."""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
import traceback
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from cafe import obs_auth, obs_bridge

GAME = 'Synthetic game:Chrome_WidgetWin_1:Discord.exe'
CAMERA = 'Synthetic call:Chrome_WidgetWin_1:Discord.exe'
FEEDS = {'game': GAME, 'camera': CAMERA}


def assert_canary_absent(root, logs, values, canary):
    candidates = [logs] + [repr(value) for value in values]
    for file in root.rglob('*'):
        if file.is_file():
            candidates.append(file.read_bytes().decode('utf-8', errors='replace'))
    if any(canary in value for value in candidates):
        raise AssertionError('synthetic secret leaked')


def quiet_logger():
    logger = logging.Logger('fake.obs.private', level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class FakeOBS:
    def __init__(self, password=None):
        self.password = password
        self.salt = base64.b64encode(b's' * 32).decode()
        self.challenge = base64.b64encode(b'c' * 32).decode()
        self.scenes = set()
        self.inputs = {}
        self.items = {}
        self.requests = []
        self.identifies = []
        self.effects = []
        self.transforms = {}
        self.source_size = (1280, 720)
        self.crop_mode = None
        self.accepted = 0
        self.rpc = 1
        self.omit_studio_version = False
        self.mode = None
        self.errors = []
        self.echo = ''

    async def handle(self, websocket):
        self.accepted += 1
        hello = {'obsStudioVersion': '32.2.2', 'obsWebSocketVersion': '5.6.0', 'rpcVersion': self.rpc}
        if self.omit_studio_version:
            hello.pop('obsStudioVersion')
        if self.password:
            hello['authentication'] = {'salt': self.salt, 'challenge': self.challenge}
        try:
            await websocket.send(json.dumps({'op': 0, 'd': hello}))
            identify = json.loads(await websocket.recv())
            self.identifies.append(identify)
            if self.password:
                # Server's independent protocol implementation, not helper output.
                first = hashlib.sha256((self.password + self.salt).encode()).digest()
                secret = base64.b64encode(first).decode()
                expected = base64.b64encode(hashlib.sha256((secret + self.challenge).encode()).digest()).decode()
                if identify.get('d', {}).get('authentication') != expected:
                    await websocket.close(code=4009, reason=self.echo)
                    return
            elif 'authentication' in identify.get('d', {}):
                self.errors.append('unexpected_authentication')
                return
            if identify.get('op') != 1 or identify.get('d', {}).get('rpcVersion') != 1:
                self.errors.append('invalid_identify')
                return
            await websocket.send(json.dumps({'op': 2, 'd': {'negotiatedRpcVersion': 1}}))
            async for raw in websocket:
                frame = json.loads(raw)
                if frame.get('op') != 6:
                    self.errors.append('request_before_identified_or_wrong_opcode')
                    return
                data = frame['d']
                self.requests.append(data)
                kind, body = data['requestType'], data.get('requestData', {})
                result, code, response = True, 100, {}
                if self.mode == 'malformed':
                    self.mode = None
                    await websocket.send(json.dumps({'op': 7, 'd': {'requestType': kind, 'requestId': 'wrong', 'requestStatus': {'result': True, 'code': 100}, 'responseData': {'scenes': []}}}))
                    continue
                if self.mode == 'bad_json':
                    await websocket.send('not-json-' + self.echo)
                    return
                if self.mode == 'error_comment':
                    await websocket.send(json.dumps({'op': 7, 'd': {'requestType': kind, 'requestId': data['requestId'], 'requestStatus': {'result': False, 'code': 600, 'comment': self.echo}}}))
                    return
                if self.mode == 'close_echo':
                    await websocket.close(code=1011, reason=self.echo)
                    return
                if kind == 'GetSceneList':
                    response = {'scenes': [{'sceneName': name} for name in sorted(self.scenes)]}
                elif kind == 'CreateScene':
                    self.scenes.add(body['sceneName'])
                    self.effects.append(kind)
                    response = {'sceneUuid': 'synthetic-scene'}
                elif kind == 'GetInputList':
                    response = {'inputs': [{'inputName': name, 'inputKind': row['kind']} for name, row in self.inputs.items()]}
                elif kind == 'GetInputKindList':
                    response = {'inputKinds': ['window_capture']}
                elif kind == 'CreateInput':
                    name = body['inputName']
                    if name in self.inputs:
                        result, code = False, 601
                    else:
                        self.inputs[name] = {'kind': body['inputKind'], 'settings': dict(body['inputSettings'])}
                        item = len(self.items) + 1
                        self.items[item] = {'sourceName': name, 'sceneItemId': item, 'sceneItemEnabled': body.get('sceneItemEnabled', True)}
                        self.effects.append(kind)
                        response = {'sceneItemId': item, 'inputUuid': 'synthetic-input-' + str(item)}
                        if self.mode == 'disconnect_after_create':
                            self.mode = None
                            await websocket.close(code=1011, reason='interrupted')
                            return
                elif kind == 'GetInputSettings':
                    row = self.inputs[body['inputName']]
                    response = {'inputKind': row['kind'], 'inputSettings': dict(row['settings'])}
                elif kind == 'SetInputSettings':
                    self.inputs[body['inputName']]['settings'].update(body['inputSettings'])
                    self.effects.append(kind)
                elif kind == 'GetSceneItemList':
                    response = {'sceneItems': [dict(row) for row in self.items.values()]}
                elif kind == 'GetSceneItemId':
                    matches = [i for i, row in self.items.items() if row['sourceName'] == body['sourceName']]
                    if len(matches) == 1:
                        response = {'sceneItemId': matches[0]}
                    else:
                        result, code = False, 600
                elif kind == 'CreateSceneItem':
                    item = len(self.items) + 1
                    self.items[item] = {'sourceName': body['sourceName'], 'sceneItemId': item, 'sceneItemEnabled': body.get('sceneItemEnabled', True)}
                    self.effects.append(kind)
                    response = {'sceneItemId': item}
                elif kind == 'SetSceneItemEnabled':
                    self.items[body['sceneItemId']]['sceneItemEnabled'] = body['sceneItemEnabled']
                    self.effects.append(kind)
                elif kind == 'GetSceneItemTransform':
                    item = body['sceneItemId']
                    response = {'sceneItemTransform': {'sourceWidth': self.source_size[0], 'sourceHeight': self.source_size[1], **self.transforms.get(item, {'cropLeft': 0, 'cropTop': 0, 'cropRight': 0, 'cropBottom': 0})}}
                    if self.crop_mode == 'mismatch' and item in self.transforms:
                        response['sceneItemTransform']['cropLeft'] += 1
                elif kind == 'SetSceneItemTransform':
                    if self.items[body['sceneItemId']]['sceneItemEnabled']:
                        self.errors.append('crop_applied_while_visible')
                    self.transforms[body['sceneItemId']] = dict(body['sceneItemTransform'])
                    self.effects.append(kind)
                elif kind == 'GetSceneItemEnabled':
                    response = {'sceneItemEnabled': self.items[body['sceneItemId']]['sceneItemEnabled']}
                else:
                    self.errors.append('unexpected_request_' + kind)
                    result, code = False, 204
                await websocket.send(json.dumps({'op': 7, 'd': {'requestType': kind, 'requestId': data['requestId'], 'requestStatus': {'result': result, 'code': code}, 'responseData': response}}))
        except ConnectionClosed:
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)


class TestObsBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cafe-obs-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.password = 'synthetic-obs-' + secrets.token_urlsafe(24)
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger().addHandler(self.handler)
        self.addCleanup(logging.getLogger().removeHandler, self.handler)
        self.fake = FakeOBS()
        self.server = await serve(self.fake.handle, '127.0.0.1', 0, logger=quiet_logger(), compression=None)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []
        self.observed = []

    async def asyncTearDown(self):
        try:
            for client in self.clients:
                await client.close()
        finally:
            self.server.close()
            await self.server.wait_closed()
        self.assertEqual(self.fake.errors, [])
        assert_canary_absent(self.root, self.log.getvalue(), self.observed, self.password)

    async def connect(self, password=None):
        client = await obs_bridge.connect('127.0.0.1', self.port, password=password)
        self.clients.append(client)
        self.observed.append(repr(client))
        return client

    def observe_error(self, error):
        self.observed.extend([str(error), repr(error), ''.join(traceback.format_exception(error))])
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_dependency_is_exact_operator_version(self):
        self.assertEqual(websockets.__version__, '17.2')

    async def test_no_password_identify_and_scene_create(self):
        self.fake.omit_studio_version = True
        client = await self.connect()
        result = await client.send_capsule('guest_a', {'game': GAME})
        self.assertEqual(result['scene'], 'Café')
        self.assertEqual(result['outcome'], 'configured')
        self.assertEqual(self.fake.identifies[0], {'op': 1, 'd': {'rpcVersion': 1, 'eventSubscriptions': 0}})
        self.assertEqual(self.fake.effects.count('CreateScene'), 1)
        self.assertEqual(self.fake.effects.count('CreateInput'), 1)
        self.observed.append(result)

    async def test_password_identify_two_windows_and_presence_only_journal(self):
        self.fake.password = self.password
        client = await self.connect(self.password)
        result = await client.send_capsule('guest_a', FEEDS)
        self.assertEqual(set(result['feeds']), {'game', 'camera'})
        self.assertEqual(set(self.fake.inputs), {'Café.guest_a.game', 'Café.guest_a.camera'})
        self.assertEqual(self.fake.inputs['Café.guest_a.game']['settings']['window'], GAME)
        self.assertEqual(self.fake.inputs['Café.guest_a.camera']['settings']['window'], CAMERA)
        self.assertTrue(all(row['sceneItemEnabled'] for row in self.fake.items.values()))
        self.assertTrue(all(row['kind'] == 'window_capture' for row in self.fake.inputs.values()))
        self.assertNotIn('password', vars(client))
        self.observed.append(result)
        record = obs_auth.password_presence(self.password)
        self.assertTrue(record == {'obs_password_present': True}, 'password_presence_contract')
        (self.root / 'journal.jsonl').write_text(json.dumps(record) + '\n')
        self.assertEqual(obs_auth.password_presence(None), {'obs_password_present': False})

    async def test_repeated_and_reconnected_operation_is_idempotent(self):
        client = await self.connect()
        first = await client.send_capsule('guest_a', FEEDS)
        second = await client.send_capsule('guest_a', FEEDS)
        await client.close()
        replacement = await self.connect()
        third = await replacement.send_capsule('guest_a', FEEDS)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(self.fake.effects.count('CreateScene'), 1)
        self.assertEqual(self.fake.effects.count('CreateInput'), 2)
        self.assertEqual(len(self.fake.items), 2)

    async def test_wrong_password_rejected_before_effect(self):
        self.fake.password = 'different-synthetic-password'
        self.fake.echo = self.password
        with self.assertRaises(obs_auth.ObsError) as caught:
            await self.connect(self.password)
        self.assertEqual(str(caught.exception), 'authentication_failed')
        self.observe_error(caught.exception)
        self.assertEqual(self.fake.effects, [])
        self.assertEqual(self.fake.requests, [])

    async def test_nonloopback_hosts_rejected_before_socket(self):
        for host in ['localhost', '0.0.0.0', '::1', '127.0.0.2', '127.0.0.1.example', '127.0.0.1@remote']:
            with self.subTest(endpoint=host), self.assertRaises(obs_auth.ObsError) as caught:
                await obs_bridge.connect(host, self.port, password=self.password)
            self.assertEqual(str(caught.exception), 'loopback_only')
            self.observe_error(caught.exception)
        self.assertEqual(self.fake.accepted, 0)

    async def test_nonloopback_alias_rejected_before_socket(self):
        # Mutation contrast uses only this local alias, never a remote DNS target.
        with self.assertRaises(obs_auth.ObsError) as caught:
            client = await obs_bridge.connect('localhost', self.port)
            self.clients.append(client)
        self.assertEqual(str(caught.exception), 'loopback_only')
        self.assertEqual(self.fake.accepted, 0)
        self.observe_error(caught.exception)

    async def test_malformed_response_rejected_before_mutation(self):
        self.fake.mode = 'malformed'
        client = await self.connect()
        with self.assertRaises(obs_auth.ObsError) as caught:
            await client.send_capsule('guest_a', FEEDS)
        self.assertEqual(str(caught.exception), 'malformed_response')
        self.observe_error(caught.exception)
        self.assertEqual(self.fake.effects, [])

    async def test_unsupported_rpc_rejected_before_identify(self):
        self.fake.rpc = 2
        with self.assertRaises(obs_auth.ObsError) as caught:
            await self.connect()
        self.assertEqual(str(caught.exception), 'unsupported_rpc')
        self.observe_error(caught.exception)
        self.assertEqual(self.fake.identifies, [])
        self.assertEqual(self.fake.effects, [])

    async def test_provider_secret_echo_never_reaches_log_error_or_journal(self):
        for mode in ['bad_json', 'error_comment', 'close_echo']:
            self.fake.mode = mode
            self.fake.echo = self.password
            client = await self.connect()
            with self.subTest(mode=mode), self.assertRaises(obs_auth.ObsError) as caught:
                await client.send_capsule('guest_a', FEEDS)
            self.observe_error(caught.exception)
            self.assertNotIn(self.password, self.log.getvalue())
            await client.close()
        (self.root / 'errors.jsonl').write_text(json.dumps(self.observed) + '\n')
        assert_canary_absent(self.root, self.log.getvalue(), self.observed, self.password)

    async def test_secret_canary_oracle_rejects_contaminated_log(self):
        bad = self.root / 'contaminated.log'
        bad.write_text(self.password)
        try:
            with self.assertRaisesRegex(AssertionError, 'synthetic secret leaked'):
                assert_canary_absent(self.root, '', [], self.password)
        finally:
            bad.unlink()

    async def test_disconnect_after_effect_is_uncertain_and_retry_reconciles(self):
        self.fake.mode = 'disconnect_after_create'
        client = await self.connect()
        with self.assertRaises(obs_auth.ObsError) as caught:
            await client.send_capsule('guest_a', FEEDS)
        self.assertEqual(str(caught.exception), 'operation_uncertain')
        self.observe_error(caught.exception)
        replacement = await self.connect()
        result = await replacement.send_capsule('guest_a', FEEDS)
        self.assertEqual(result['outcome'], 'configured')
        self.assertEqual(self.fake.effects.count('CreateScene'), 1)
        self.assertEqual(self.fake.effects.count('CreateInput'), 2)
        self.assertEqual(len(self.fake.items), 2)

    async def test_conflicting_existing_window_is_not_silently_rebound(self):
        client = await self.connect()
        await client.send_capsule('guest_a', {'game': GAME})
        before = list(self.fake.effects)
        with self.assertRaises(obs_auth.ObsError) as caught:
            await client.send_capsule('guest_a', {'game': CAMERA})
        self.assertEqual(str(caught.exception), 'source_conflict')
        self.assertEqual(self.fake.effects, before)
        self.observe_error(caught.exception)

    async def test_ambiguous_duplicate_descriptors_rejected_before_effect(self):
        client = await self.connect()
        with self.assertRaises(obs_auth.ObsError) as caught:
            await client.send_capsule('guest_a', {'game': GAME, 'camera': GAME})
        self.assertEqual(str(caught.exception), 'ambiguous_window')
        self.assertEqual(self.fake.effects, [])
        self.observe_error(caught.exception)

    async def test_http_redirect_is_rejected_without_visiting_target(self):
        visits = []
        async def target(socket):
            visits.append(True)
        async with serve(target, '127.0.0.1', 0, logger=quiet_logger()) as destination:
            port = destination.sockets[0].getsockname()[1]
            def redirect(connection, request):
                response = connection.respond(302, 'redirect')
                response.headers['Location'] = 'ws://127.0.0.1:' + str(port) + '/' + self.password
                return response
            async with serve(target, '127.0.0.1', 0, process_request=redirect, logger=quiet_logger()) as origin:
                source_port = origin.sockets[0].getsockname()[1]
                with self.assertRaises(obs_auth.ObsError) as caught:
                    await obs_bridge.connect('127.0.0.1', source_port, password=self.password)
                self.assertEqual(str(caught.exception), 'connection_failed')
                self.observe_error(caught.exception)
            self.assertEqual(visits, [])

    async def test_fixed_independent_authentication_vector(self):
        # Expected bytes transcribed from independent OpenSSL SHA256 calls.
        hello = {'op': 0, 'd': {'rpcVersion': 1, 'obsStudioVersion': '30.2.2', 'obsWebSocketVersion': '5.5.2', 'authentication': {'challenge': 'Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=', 'salt': 'c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M='}}}
        result = obs_auth.identify_payload(hello, 'public-synthetic-password')
        self.assertEqual(result['authentication'], 'ySTAe1UCPepVR9ho1Z7oPasFVD+BsGD+7X+TVJPlqYs=')


if __name__ == '__main__':
    unittest.main()
