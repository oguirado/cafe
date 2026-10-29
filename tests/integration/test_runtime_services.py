"""Independent ownership/shutdown contrasts; no live services or credentials."""
import asyncio
import unittest
from unittest.mock import patch
from connectors.cafe import runtime_services as module


class Obs:
    def __init__(self):
        self.closes = 0
        self.sends = 0
        self.error = None

    async def close(self):
        self.closes += 1

    async def send_capsule(self, capsule, feeds):
        self.sends += 1
        if self.error:
            raise RuntimeError(self.error)
        return {"accepted": True}


class RuntimeServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_is_none_and_close_idempotent(self):
        obs = Obs()
        service = module.RuntimeServices(obs)
        self.assertEqual(service.status()["obs"], "configured")
        self.assertIsNone(service.status()["messages"])
        await service.close()
        await service.close()
        self.assertEqual(obs.closes, 1)
        self.assertEqual(service.status()["obs"], "closed")

    async def test_duplicate_rejected_before_factory(self):
        service = module.RuntimeServices()
        calls = []
        async def producer(stop):
            await stop.wait()
            return {"messages": 3, "malformed": False}
        def factory(stop):
            calls.append("called")
            return producer(stop)
        await service.start_chat(factory)
        with self.assertRaisesRegex(module.ServiceError, "^already_running$"):
            await service.start_chat(factory)
        self.assertEqual(calls, ["called"])
        await service.close()
        self.assertEqual(service.status()["messages"], 3)
        self.assertIsNone(service.status()["malformed"])

    async def test_never_cancels_unrelated_task_returned_by_factory(self):
        stop = asyncio.Event()
        unrelated = asyncio.create_task(stop.wait())
        service = module.RuntimeServices()
        try:
            with self.assertRaisesRegex(module.ServiceError, "^unavailable$"):
                await service.start_chat(lambda _event: unrelated)
            await service.close()
            self.assertFalse(unrelated.done())
        finally:
            stop.set()
            await unrelated

    async def test_owned_producer_self_cancel_is_not_closer_cancel(self):
        service = module.RuntimeServices(Obs())
        async def producer(stop):
            await stop.wait()
            raise asyncio.CancelledError()
        await service.start_chat(producer)
        await service.close()
        self.assertEqual(service.status()["chat"], "cancelled")
        self.assertEqual(service.status()["obs"], "closed")

    async def test_closer_cancel_leaves_bounded_owned_cleanup_running(self):
        obs = Obs()
        service = module.RuntimeServices(obs)
        await service.start_chat(lambda stop: stop.wait())
        close = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await close
        await service.close()
        self.assertEqual(obs.closes, 1)
        self.assertEqual(service.status()["obs"], "closed")

    async def test_uncertain_publish_never_retried_or_reflected(self):
        obs = Obs()
        obs.error = "operation_uncertain"
        service = module.RuntimeServices(obs)
        with self.assertRaisesRegex(module.ServiceError, "^operation_uncertain$"):
            await service.publish_capsule("guest", {"game": "Synthetic:Fixture:app.exe"})
        self.assertEqual(obs.sends, 1)
        obs.error = "SYNTHETIC_SECRET_CANARY"
        with self.assertRaisesRegex(module.ServiceError, "^unavailable$"):
            await service.publish_capsule("guest", {})
        self.assertNotIn(obs.error, str(service.status()))
        await service.close()

    async def test_uncooperative_owned_task_is_failure_with_bounded_close(self):
        service = module.RuntimeServices(Obs())
        release = asyncio.Event()
        async def producer(_stop):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        await service.start_chat(producer)
        started = asyncio.get_running_loop().time()
        try:
            with patch.object(module, "_CLOSE_GRACE", 0.03):
                with self.assertRaisesRegex(module.ServiceError, "^unavailable$"):
                    await service.close()
            self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
            self.assertEqual(service.status()["obs"], "failed")
        finally:
            release.set()
            await service._chat

    async def test_obs_close_deadline_and_secret_error_are_explicit_failures(self):
        release = asyncio.Event()
        class Stuck(Obs):
            async def close(self):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
        service = module.RuntimeServices(Stuck())
        try:
            with patch.object(module, "_CLOSE_GRACE", 0.03):
                with self.assertRaisesRegex(module.ServiceError, "^unavailable$"):
                    await service.close()
        finally:
            release.set()
            await asyncio.sleep(0.01)
        class Bad(Obs):
            def close(self):
                raise RuntimeError("SYNTHETIC_SECRET_CANARY")
        with self.assertRaisesRegex(module.ServiceError, "^unavailable$"):
            await module.RuntimeServices(Bad()).close()

    async def test_hung_publish_does_not_hold_shutdown_lock(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        class Hanging(Obs):
            async def send_capsule(self, _capsule, _feeds):
                entered.set()
                await release.wait()
            async def close(self):
                await super().close()
                release.set()
        service = module.RuntimeServices(Hanging())
        request = asyncio.create_task(service.publish_capsule("guest", {}))
        await entered.wait()
        await service.close()
        with self.assertRaisesRegex(module.ServiceError, "^operation_uncertain$"):
            await request

    async def test_replacement_during_shutdown_never_installs_new_handle(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Closing(Obs):
            async def close(self):
                entered.set()
                await release.wait()
        old, new = Closing(), Obs()
        service = module.RuntimeServices(old)
        replacing = asyncio.create_task(service.replace_obs(new))
        await entered.wait()
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        with self.assertRaisesRegex(module.ServiceError, "^closed$"):
            await replacing
        self.assertEqual(new.closes, 1)
