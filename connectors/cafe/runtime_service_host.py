"""Rust-owned service sidecar. No public listener and no credentials in argv.

Only the parent supplies the canonical private chat directory. OBS is always
loopback; chat always uses its reviewed official endpoint. Stdout is exclusively
the closed private protocol, never chat, passwords, exception text or logging.
"""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys

from .channel_auth import MAX_LINE, make_frame, validate_frame
from .runtime_service_auth import validate_service_request, validate_service_result
from .runtime_services import RuntimeServices, ServiceError


def private_root(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ValueError("private_service_root_rejected")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("private_service_root_rejected")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("private_service_root_rejected")
    if os.name == "posix":
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private_service_root_rejected")
    elif os.name == "nt":
        from . import windows_auth
        kernel, security = windows_auth._api()
        owner_storage, sid = windows_auth._owner(kernel, security)
        windows_auth._check_acl(path, True, kernel, security, sid)
        del owner_storage
    else:
        raise ValueError("private_service_platform_unsupported")
    return path


async def _connect(port, password):
    from .obs_bridge import connect
    return await connect("127.0.0.1", port, password)


async def _produce(channel, directory, stop, progress):
    from .twitch_chat import run_chat
    return await run_chat(channel, directory, stop_event=stop, progress=progress)


class ServiceDispatcher:
    def __init__(self, directory, *, obs_connect=_connect, producer=_produce):
        self.root = private_root(directory)
        self.services = RuntimeServices()
        self._connect = obs_connect
        self._produce = producer

    def projection(self, accepted):
        body = self.services.status()
        for name in ("messages", "malformed", "reconnects", "connections"):
            value = body[name]
            body[name] = str(value) if type(value) is int and 0 <= value <= 18446744073709551615 else None
        return validate_service_result({"accepted": bool(accepted), **body})

    async def dispatch(self, body):
        validate_service_request(body)
        try:
            if body["op"] == "configure_obs":
                choice = body["password"]
                password = choice["value"] if choice["mode"] == "provided" else None
                client = await self._connect(body["port"], password)
                password = None
                try:
                    await self.services.replace_obs(client)
                except BaseException:
                    # Ownership transfers only after replace_obs succeeds.
                    close = asyncio.create_task(client.close())
                    close.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                    done, _ = await asyncio.wait({close}, timeout=8)
                    if not done:
                        close.cancel()
                    raise
            elif body["op"] == "start_chat":
                channel = body["channel"]
                await self.services.start_chat(lambda stop: self._produce(channel, self.root, stop, self.services.observe_chat))
            elif body["op"] == "send_capsule":
                await self.services.publish_capsule(body["capsule_id"], body["feeds"], body["crops"])
            elif body["op"] == "close":
                await self.services.close()
        except Exception:
            return self.projection(False)
        return self.projection(True)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("private_frame_rejected")
        result[key] = value
    return result


def _number_rejected(_value):
    raise ValueError("private_frame_rejected")


def decode_private(raw, epoch):
    if not raw or len(raw) > MAX_LINE or not raw.endswith(b"\n"):
        raise ValueError("private_frame_rejected")
    try:
        frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_number_rejected)
        actual = validate_frame(frame, epoch, True)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("private_frame_rejected") from None
    return frame, actual


def _write(frame):
    data = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("private_frame_rejected")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


async def serve(directory):
    dispatcher = ServiceDispatcher(directory)
    epoch = None
    try:
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
            if not raw:
                return 0 if epoch is not None else 2
            frame, actual = decode_private(raw, epoch)
            if epoch is None:
                epoch = actual
                await asyncio.to_thread(_write, make_frame("observation", epoch, {"ready": True}))
                continue
            if frame["kind"] != "service_request":
                return 2
            body = frame["body"]
            closed = body["op"] == "close"
            result = await dispatcher.dispatch(body)
            del body, frame
            reply = make_frame("service_result", epoch, result)
            validate_frame(reply, epoch, False)
            await asyncio.to_thread(_write, reply)
            if closed:
                return 0 if result["accepted"] else 2
    finally:
        await dispatcher.services.close()


def main():
    code = 2
    try:
        if len(sys.argv) == 2:
            code = asyncio.run(serve(sys.argv[1]))
    except BaseException:
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
