"""Mechanical OBS/chat ownership, with parent-authored bounded shutdown.

The parent owns authentication, framing, filesystem grants and tests. Factories
are trusted parent callbacks; this module cannot choose endpoints or paths.
"""
import asyncio
import inspect

__all__ = ["ServiceError", "RuntimeServices"]
_CLOSE_GRACE = 8.0
_CODES = {"closed", "not_configured", "already_running", "operation_uncertain", "unavailable"}


class ServiceError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in _CODES else "unavailable")


def _code(error):
    value = error.args[0] if isinstance(error, Exception) and error.args else None
    return value if type(value) is str and value in _CODES else "unavailable"


def _consume(task):
    if not task.cancelled():
        task.exception()


async def _close_client(client):
    try:
        await client.close()
    except Exception as error:
        raise ServiceError(_code(error)) from None


class RuntimeServices:
    def __init__(self, obs_client=None):
        self._lock = asyncio.Lock()
        self._obs = obs_client
        self._obs_state = "absent" if obs_client is None else "configured"
        self._obs_code = None
        self._closed = False
        self._close_task = None
        self._chat = None
        self._stop = None
        self._chat_state = "idle"
        self._chat_code = None
        self._connected = None
        self._counts = dict.fromkeys(("messages", "malformed", "reconnects", "connections"))

    async def replace_obs(self, client):
        if not callable(getattr(client, "send_capsule", None)) or not callable(getattr(client, "close", None)):
            raise ServiceError("unavailable")
        async with self._lock:
            if self._closed:
                raise ServiceError("closed")
            old = self._obs
            try:
                if old is not None:
                    await old.close()
            except Exception as error:
                self._obs_code = _code(error)
                raise ServiceError(self._obs_code) from None
            if self._closed:
                # Shutdown can interrupt an admitted replacement while it awaits
                # the old client. Never install or leak the new client afterward.
                task = asyncio.create_task(_close_client(client))
                task.add_done_callback(_consume)
                done, _ = await asyncio.wait({task}, timeout=_CLOSE_GRACE)
                if not done:
                    task.cancel()
                    raise ServiceError("unavailable")
                if task.cancelled() or task.exception() is not None:
                    raise ServiceError("unavailable")
                raise ServiceError("closed")
            self._obs = client
            self._obs_state, self._obs_code = "configured", None

    async def publish_capsule(self, capsule_id, feeds, crops=None):
        async with self._lock:
            if self._closed:
                raise ServiceError("closed")
            if self._obs is None:
                raise ServiceError("not_configured")
            try:
                result = (await self._obs.send_capsule(capsule_id, feeds) if crops is None else
                          await self._obs.send_capsule(capsule_id, feeds, crops=crops))
            except Exception as error:
                self._obs_code = _code(error)
                raise ServiceError(self._obs_code) from None
            if self._closed:
                raise ServiceError("operation_uncertain")
            self._obs_code = None
            return result

    async def start_chat(self, producer_factory):
        async with self._lock:
            if self._closed:
                raise ServiceError("closed")
            if self._chat is not None and not self._chat.done():
                raise ServiceError("already_running")
            stop = asyncio.Event()
            try:
                coroutine = producer_factory(stop)
            except Exception:
                raise ServiceError("unavailable") from None
            if not inspect.iscoroutine(coroutine) or inspect.getcoroutinestate(coroutine) != inspect.CORO_CREATED:
                # A Task/Future can belong to somebody else. Never cancel it.
                raise ServiceError("unavailable")
            self._chat = asyncio.create_task(coroutine)
            self._chat.add_done_callback(self._chat_done)
            self._stop = stop
            self._chat_state, self._chat_code = "running", None
            self._connected = None
            self._counts = dict.fromkeys(self._counts)

    def observe_chat(self, value):
        if (type(value) is not dict or set(value) != {*self._counts, 'connected'}
                or type(value['connected']) is not bool
                or any(type(value[name]) is not int or not 0 <= value[name] <= 18446744073709551615 for name in self._counts)):
            raise ServiceError('unavailable')
        if self._closed or self._chat is None or self._chat.done():
            return
        self._counts = {name: value[name] for name in self._counts}
        self._connected = value['connected']

    def _chat_done(self, task):
        _consume(task)
        if task is not self._chat:
            return
        self._connected = False
        if task.cancelled():
            self._chat_state, self._chat_code = "cancelled", "unavailable"
        elif task.exception() is not None:
            self._chat_state, self._chat_code = "failed", _code(task.exception())
        else:
            self._chat_state, self._chat_code = "succeeded", None
            result = task.result()
            if type(result) is dict:
                for name in self._counts:
                    value = result.get(name)
                    self._counts[name] = value if type(value) is int and 0 <= value <= 18446744073709551615 else None

    def status(self):
        if self._chat is not None and self._chat.done():
            self._chat_done(self._chat)
        return {"closed": self._closed, "obs": self._obs_state, "obs_code": self._obs_code,
                "chat": self._chat_state, "chat_code": self._chat_code, "chat_connected": self._connected, **self._counts}

    async def _shutdown(self):
        # No operation-lock acquisition: an unresponsive publish must not prevent
        # closing its transport. All tasks in this set were created by this owner.
        if self._stop is not None:
            self._stop.set()
        tasks = set()
        if self._chat is not None and not self._chat.done():
            tasks.add(self._chat)
        obs_close = None
        if self._obs is not None:
            obs_close = asyncio.create_task(_close_client(self._obs))
            obs_close.add_done_callback(_consume)
            tasks.add(obs_close)
        pending = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_CLOSE_GRACE)
            for task in pending:
                task.cancel()
        failed = bool(pending)
        if obs_close is not None and obs_close.done():
            failed |= obs_close.cancelled() or obs_close.exception() is not None
        self._obs_state = "failed" if failed else "closed"
        self._obs_code = "unavailable" if failed else None
        if failed:
            # The Rust owner must reap the connector process. A Python task that
            # ignores cancellation cannot be claimed as cleanly terminated.
            raise ServiceError("unavailable")

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._shutdown())
            self._close_task.add_done_callback(_consume)
        try:
            # asyncio.wait never waits for cancellation acknowledgement by a task
            # that may suppress CancelledError (unlike wait_for).
            await asyncio.wait({self._close_task})
        except asyncio.CancelledError:
            # Shutdown remains owned and bounded even if its caller is cancelled.
            raise
        self._close_task.result()
