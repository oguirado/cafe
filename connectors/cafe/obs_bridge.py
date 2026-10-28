"""OBS WebSocket v5 semantic client for the Cafe sprint MVP.

I/O-bound orchestration glue only: opens a loopback websocket through the
dedicated closed helpers, runs Hello/Identify/Identified, then drives
correlated scene/input requests sequentially.

All validation, endpoint admission, authentication math and error closure
belong to ``cafe.obs_auth``; this module implements no copies of them,
stores no credentials, and never surfaces provider strings.
"""

import asyncio
import json

from cafe import obs_auth

from .obs_auth import ObsError

_SCENE = "Café"
_INPUT_KIND = "window_capture"
_BOUND = 5.0


async def _quiet_close(ws):
    try:
        await asyncio.wait_for(ws.close(), _BOUND)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def connect(host, port, password=None):
    """Open an OBS session on literal 127.0.0.1 and complete Identify."""
    ws = await obs_auth.open_local_connection(host, port)
    hello_failed = False
    try:
        raw_hello = await asyncio.wait_for(ws.recv(), _BOUND)
    except asyncio.CancelledError:
        raise
    except Exception:
        hello_failed = True
    if hello_failed:
        await _quiet_close(ws)
        raise ObsError("authentication_failed")
    try:
        hello = obs_auth.decode_message(raw_hello)
    except ObsError:
        await _quiet_close(ws)
        raise
    try:
        payload = obs_auth.identify_payload(hello, password)
    except ObsError:
        del password
        await _quiet_close(ws)
        raise
    del password
    identify = {"op": 1, "d": payload}
    del payload
    handshake_failed = False
    try:
        await asyncio.wait_for(ws.send(json.dumps(identify)), _BOUND)
        raw_identified = await asyncio.wait_for(ws.recv(), _BOUND)
    except asyncio.CancelledError:
        raise
    except Exception:
        handshake_failed = True
    del identify
    if handshake_failed:
        await _quiet_close(ws)
        raise ObsError("authentication_failed")
    try:
        identified = obs_auth.decode_message(raw_identified)
    except ObsError:
        await _quiet_close(ws)
        raise
    try:
        obs_auth.validate_identified(identified)
    except ObsError:
        await _quiet_close(ws)
        raise
    return ObsBridge(ws)


class ObsBridge:
    """One handshake-completed OBS session; no secrets retained."""

    def __init__(self, ws):
        self._ws = ws
        self._lock = asyncio.Lock()
        self._seq = 0
        self._closed = False
        self._broken = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False

    async def close(self):
        if self._closed:
            return
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            await _quiet_close(ws)

    async def send_capsule(self, capsule_id, feeds):
        obs_auth.validate_capsule(capsule_id, feeds)
        async with self._lock:
            if self._closed or self._broken or self._ws is None:
                raise ObsError("not_connected")
            names = {}
            for role in sorted(feeds):
                names[role] = "%s.%s.%s" % (_SCENE, capsule_id, role)
            await self._ensure_scene()
            kinds = await self._input_kinds()
            for role in sorted(names):
                kind = kinds.get(names[role])
                if kind is not None:
                    if kind != _INPUT_KIND:
                        raise ObsError("source_conflict")
                    await self._verify_settings(names[role], feeds[role])
            items = await self._scene_items()
            result_feeds = {}
            for role in sorted(names):
                item_id = await self._ensure_source(
                    names[role], feeds[role], kinds.get(names[role]),
                    items.get(names[role]),
                )
                result_feeds[role] = {
                    "input_name": names[role],
                    "scene_item_id": item_id,
                }
            return {
                "scene": _SCENE,
                "outcome": "configured",
                "feeds": result_feeds,
            }

    async def _exchange(self, request_type, request_data):
        self._seq += 1
        request_id = "cafe-%d" % self._seq
        payload = json.dumps({"op": 6, "d": {
            "requestType": request_type,
            "requestId": request_id,
            "requestData": request_data,
        }})
        transfer_failed = False
        try:
            await asyncio.wait_for(self._ws.send(payload), _BOUND)
            raw = await asyncio.wait_for(self._ws.recv(), _BOUND)
        except asyncio.CancelledError:
            raise
        except Exception:
            transfer_failed = True
        if transfer_failed:
            self._broken = True
            await self.close()
            raise ObsError("operation_uncertain")
        message = obs_auth.decode_message(raw)
        return obs_auth.response_data(message, request_type, request_id)

    async def _ensure_scene(self):
        data = await self._exchange("GetSceneList", {})
        scenes = data.get("scenes")
        if type(scenes) is not list:
            raise ObsError("malformed_response")
        found = False
        for entry in scenes:
            if type(entry) is not dict or type(entry.get("sceneName")) is not str:
                raise ObsError("malformed_response")
            if entry["sceneName"] == _SCENE:
                found = True
        if not found:
            await self._exchange("CreateScene", {"sceneName": _SCENE})

    async def _input_kinds(self):
        data = await self._exchange("GetInputList", {})
        inputs = data.get("inputs")
        if type(inputs) is not list:
            raise ObsError("malformed_response")
        kinds = {}
        for entry in inputs:
            if (type(entry) is not dict
                    or type(entry.get("inputName")) is not str
                    or type(entry.get("inputKind")) is not str):
                raise ObsError("malformed_response")
            if entry["inputName"] in kinds:
                raise ObsError("malformed_response")
            kinds[entry["inputName"]] = entry["inputKind"]
        return kinds

    async def _scene_items(self):
        data = await self._exchange("GetSceneItemList", {"sceneName": _SCENE})
        entries = data.get("sceneItems")
        if type(entries) is not list:
            raise ObsError("malformed_response")
        items = {}
        for entry in entries:
            if (type(entry) is not dict
                    or type(entry.get("sourceName")) is not str
                    or type(entry.get("sceneItemId")) is not int):
                raise ObsError("malformed_response")
            if entry["sourceName"] in items:
                raise ObsError("ambiguous_window")
            items[entry["sourceName"]] = entry["sceneItemId"]
        return items

    async def _verify_settings(self, name, descriptor):
        data = await self._exchange("GetInputSettings", {"inputName": name})
        if data.get("inputKind") != _INPUT_KIND:
            raise ObsError("source_conflict")
        settings = data.get("inputSettings")
        if type(settings) is not dict or settings.get("window") != descriptor:
            raise ObsError("source_conflict")

    async def _ensure_source(self, name, descriptor, kind, item_id):
        if kind is None:
            data = await self._exchange("CreateInput", {
                "sceneName": _SCENE,
                "inputName": name,
                "inputKind": _INPUT_KIND,
                "inputSettings": {
                    "window": descriptor,
                    "method": 2,
                    "priority": 0,
                    "cursor": False,
                    "client_area": True,
                    "compatibility": False,
                    "capture_audio": False,
                },
                "sceneItemEnabled": False,
            })
            item_id = data.get("sceneItemId")
            if type(item_id) is not int:
                raise ObsError("malformed_response")
        elif item_id is None:
            data = await self._exchange("CreateSceneItem", {
                "sceneName": _SCENE,
                "sourceName": name,
                "sceneItemEnabled": False,
            })
            item_id = data.get("sceneItemId")
            if type(item_id) is not int:
                raise ObsError("malformed_response")
        await self._verify_settings(name, descriptor)
        await self._exchange("SetSceneItemEnabled", {
            "sceneName": _SCENE,
            "sceneItemId": item_id,
            "sceneItemEnabled": True,
        })
        state = await self._exchange("GetSceneItemEnabled", {
            "sceneName": _SCENE,
            "sceneItemId": item_id,
        })
        enabled = state.get("sceneItemEnabled")
        if type(enabled) is not bool:
            raise ObsError("malformed_response")
        if enabled is not True:
            raise ObsError("request_failed")
        return item_id
