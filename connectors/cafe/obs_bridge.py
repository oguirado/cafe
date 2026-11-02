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
import re
import unicodedata

from cafe import obs_auth

from .obs_auth import ObsError

_SCENE = "Café"
_SCENE_PREFIX = "Café · "
_MAX_LABEL_LEN = 48
_INPUT_KIND = "window_capture"
_MANAGED_INPUT = re.compile(re.escape(_SCENE) + r"\.([A-Za-z0-9_-]{1,64})\.(?:game|camera)")
_BOUND = 5.0


def sanitize_label(label):
    """Sanitize untrusted slot or person label for an OBS scene name."""
    if type(label) is not str:
        raise ObsError("invalid_capsule")
    chars = []
    for ch in label:
        code = ord(ch)
        if ch in "\t\n\r ":
            chars.append(" ")
        elif code < 32 or 127 <= code <= 159 or unicodedata.category(ch)[0] == "C":
            continue
        else:
            chars.append(ch)
    cleaned = " ".join("".join(chars).split())[:_MAX_LABEL_LEN].rstrip()
    if not cleaned:
        raise ObsError("invalid_capsule")
    return cleaned


def scene_name(label):
    """Return the per-capsule OBS scene name ``Café · <sanitized label>``."""
    scene = "%s%s" % (_SCENE_PREFIX, sanitize_label(label))
    if not scene.startswith(_SCENE_PREFIX):
        raise ObsError("invalid_capsule")
    return scene


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

    async def send_capsule(self, capsule_id, feeds, crops=None, *, label=None):
        obs_auth.validate_capsule(capsule_id, feeds)
        if crops is not None:
            obs_auth.validate_crops(feeds, crops)
        scene = scene_name(capsule_id if label is None else label)
        async with self._lock:
            if self._closed or self._broken or self._ws is None:
                raise ObsError("not_connected")
            names = {}
            for role in sorted(feeds):
                names[role] = "%s.%s.%s" % (_SCENE, capsule_id, role)
            await self._ensure_scene(scene)
            canvas = await self._canvas_size()
            kinds = await self._input_kinds()
            for role in sorted(names):
                kind = kinds.get(names[role])
                if kind is not None:
                    if kind != _INPUT_KIND:
                        raise ObsError("source_conflict")
                    await self._verify_settings(names[role], feeds[role])
            items = await self._scene_items(scene)
            # Only the exact input namespace generated above identifies managed
            # capsule sources. Never mix another capsule into a colliding scene;
            # unrelated user overlays remain untouched.
            for name in items:
                managed = _MANAGED_INPUT.fullmatch(name)
                if managed is not None and managed.group(1) != capsule_id:
                    raise ObsError("source_conflict")
            result_feeds = {}
            for role in sorted(names):
                crop = None if crops is None else crops[role]
                item_id = await self._ensure_source(
                    scene, names[role], feeds[role], kinds.get(names[role]),
                    items.get(names[role]), crop, canvas,
                )
                result_feeds[role] = {
                    "input_name": names[role],
                    "scene_item_id": item_id,
                }
            await self._exchange("SetCurrentProgramScene", {"sceneName": scene})
            return {
                "scene": scene,
                "outcome": "configured",
                "feeds": result_feeds,
            }

    async def set_program_scene(self, label):
        scene = scene_name(label)
        async with self._lock:
            if self._closed or self._broken or self._ws is None:
                raise ObsError("not_connected")
            if not await self._scene_exists(scene):
                raise ObsError("request_failed")
            await self._exchange("SetCurrentProgramScene", {"sceneName": scene})
            return {"scene": scene, "outcome": "switched"}

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

    async def _scene_exists(self, scene):
        if not scene.startswith(_SCENE_PREFIX):
            raise ObsError("invalid_capsule")
        data = await self._exchange("GetSceneList", {})
        scenes = data.get("scenes")
        if type(scenes) is not list:
            raise ObsError("malformed_response")
        found = False
        for entry in scenes:
            if type(entry) is not dict or type(entry.get("sceneName")) is not str:
                raise ObsError("malformed_response")
            if entry["sceneName"] == scene:
                found = True
        return found

    async def _ensure_scene(self, scene):
        if not await self._scene_exists(scene):
            await self._exchange("CreateScene", {"sceneName": scene})

    async def _canvas_size(self):
        data = await self._exchange("GetVideoSettings", {})
        width = obs_auth._observed_integer(data.get("baseWidth"), 1, 100000)
        height = obs_auth._observed_integer(data.get("baseHeight"), 1, 100000)
        return (width, height)

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

    async def _scene_items(self, scene):
        if not scene.startswith(_SCENE_PREFIX):
            raise ObsError("invalid_capsule")
        data = await self._exchange("GetSceneItemList", {"sceneName": scene})
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

    async def _ensure_source(self, scene, name, descriptor, kind, item_id, crop=None, canvas=None):
        if not scene.startswith(_SCENE_PREFIX):
            raise ObsError("invalid_capsule")
        if kind is None:
            data = await self._exchange("CreateInput", {
                "sceneName": scene,
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
                "sceneName": scene,
                "sourceName": name,
                "sceneItemEnabled": False,
            })
            item_id = data.get("sceneItemId")
            if type(item_id) is not int:
                raise ObsError("malformed_response")
        await self._verify_settings(name, descriptor)
        await self._apply_transform(scene, item_id, crop, canvas)
        await self._exchange("SetSceneItemEnabled", {
            "sceneName": scene,
            "sceneItemId": item_id,
            "sceneItemEnabled": True,
        })
        state = await self._exchange("GetSceneItemEnabled", {
            "sceneName": scene,
            "sceneItemId": item_id,
        })
        enabled = state.get("sceneItemEnabled")
        if type(enabled) is not bool:
            raise ObsError("malformed_response")
        if enabled is not True:
            raise ObsError("request_failed")
        return item_id

    async def _apply_transform(self, scene, item_id, crop, canvas):
        await self._exchange("SetSceneItemEnabled", {
            "sceneName": scene,
            "sceneItemId": item_id,
            "sceneItemEnabled": False,
        })
        state = await self._exchange("GetSceneItemEnabled", {
            "sceneName": scene,
            "sceneItemId": item_id,
        })
        enabled = state.get("sceneItemEnabled")
        if type(enabled) is not bool:
            raise ObsError("malformed_response")
        if enabled is not False:
            raise ObsError("request_failed")
        plan = None
        transform = {}
        if crop is not None:
            observed = await self._exchange("GetSceneItemTransform", {
                "sceneName": scene,
                "sceneItemId": item_id,
            })
            plan = obs_auth.plan_crop(crop, observed)
            transform.update(plan["transform"])
        if canvas is not None:
            transform.update({
                "positionX": 0,
                "positionY": 0,
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": 0,
                "boundsWidth": canvas[0],
                "boundsHeight": canvas[1],
            })
        if transform:
            await self._exchange("SetSceneItemTransform", {
                "sceneName": scene,
                "sceneItemId": item_id,
                "sceneItemTransform": transform,
            })
            applied = await self._exchange("GetSceneItemTransform", {
                "sceneName": scene,
                "sceneItemId": item_id,
            })
            if plan is not None:
                obs_auth.verify_crop(plan, applied)
            if canvas is not None:
                applied_transform = applied.get("sceneItemTransform")
                if (type(applied_transform) is not dict
                        or applied_transform.get("boundsType") != "OBS_BOUNDS_SCALE_INNER"
                        or obs_auth._observed_integer(applied_transform.get("boundsWidth"), 1, 100000) != canvas[0]
                        or obs_auth._observed_integer(applied_transform.get("boundsHeight"), 1, 100000) != canvas[1]):
                    raise ObsError("request_failed")
