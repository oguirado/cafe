"""OBS-observed dimensions and hidden-until-verified camera crops."""
import unittest
from websockets.asyncio.server import serve
from test_obs_bridge import FakeOBS, FEEDS, quiet_logger
from cafe import obs_auth, obs_bridge


class CropValidation(unittest.TestCase):
    def test_exact_plan_uses_observed_source_not_a_window_rectangle(self):
        crop = {"x": 10, "y": 20, "width": 320, "height": 240}
        plan = obs_auth.plan_crop(crop, {"sceneItemTransform": {"sourceWidth": 1280.0, "sourceHeight": 720}})
        self.assertEqual(plan, {"source_width": 1280, "source_height": 720,
                               "transform": {"cropLeft": 10, "cropTop": 20, "cropRight": 950, "cropBottom": 460}})
        obs_auth.verify_crop(plan, {"sceneItemTransform": {"sourceWidth": 1280, "sourceHeight": 720, **plan["transform"]}})

    def test_static_invalid_crops_reject_without_network(self):
        good = {"x": 0, "y": 0, "width": 320, "height": 240}
        for value in [None, {}, {"game": good}, {"camera": {**good, "extra": 1}},
                      {"camera": {**good, "x": True}}, {"camera": {**good, "width": 0}},
                      {"camera": {**good, "x": 4294967295}}, {"camera": {**good, "height": 1.5}}]:
            with self.assertRaises(obs_auth.ObsError):
                obs_auth.validate_crops({"camera": FEEDS["camera"]}, value)

    def test_unknown_or_changed_dimensions_and_wrong_applied_crop_fail(self):
        crop = {"x": 10, "y": 20, "width": 320, "height": 240}
        for size in [0, None, True, float("nan"), 100.5, -1, 4294967296]:
            with self.assertRaises(obs_auth.ObsError):
                obs_auth.plan_crop(crop, {"sceneItemTransform": {"sourceWidth": size, "sourceHeight": 720}})
        with self.assertRaises(obs_auth.ObsError):
            obs_auth.plan_crop(crop, {"sceneItemTransform": {"sourceWidth": 100, "sourceHeight": 720}})
        plan = obs_auth.plan_crop(crop, {"sceneItemTransform": {"sourceWidth": 1280, "sourceHeight": 720}})
        for changed in [{"sourceWidth": 1279}, {"sourceHeight": 721}, {"cropLeft": 11}, {"cropBottom": False}]:
            value = {"sourceWidth": 1280, "sourceHeight": 720, **plan["transform"], **changed}
            with self.assertRaises(obs_auth.ObsError):
                obs_auth.verify_crop(plan, {"sceneItemTransform": value})


class RealCropProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_crop_is_verified_before_enabling_and_repeat_does_not_duplicate(self):
        obs = FakeOBS()
        crops = {"game": {"x": 0, "y": 0, "width": 1280, "height": 720},
                 "camera": {"x": 10, "y": 20, "width": 320, "height": 240}}
        async with serve(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
            async with await obs_bridge.connect("127.0.0.1", server.sockets[0].getsockname()[1]) as client:
                for _ in range(2):
                    answer = await client.send_capsule("guest", FEEDS, crops=crops)
                    self.assertEqual(answer["outcome"], "configured")
        self.assertEqual(len(obs.inputs), 2)
        self.assertEqual(obs.errors, [])
        camera = next(item for item, row in obs.items.items() if row["sourceName"].endswith(".camera"))
        self.assertEqual(obs.transforms[camera], {"cropLeft": 10, "cropTop": 20, "cropRight": 950, "cropBottom": 460})
        self.assertTrue(all(row["sceneItemEnabled"] for row in obs.items.values()))
        for request in obs.requests:
            if request["requestType"] == "SetSceneItemTransform":
                self.assertEqual(set(request["requestData"]["sceneItemTransform"]), {"cropLeft", "cropTop", "cropRight", "cropBottom"})

    async def test_failed_observed_or_applied_crop_leaves_camera_disabled(self):
        for mismatch in (False, True):
            obs = FakeOBS()
            if mismatch:
                obs.crop_mode = "mismatch"
            else:
                obs.source_size = (100, 100)
            async with serve(obs.handle, "127.0.0.1", 0, logger=quiet_logger()) as server:
                async with await obs_bridge.connect("127.0.0.1", server.sockets[0].getsockname()[1]) as client:
                    with self.assertRaises(obs_auth.ObsError):
                        await client.send_capsule("guest", {"camera": FEEDS["camera"]},
                                                  crops={"camera": {"x": 10, "y": 20, "width": 320, "height": 240}})
            self.assertEqual(len(obs.items), 1)
            self.assertFalse(next(iter(obs.items.values()))["sceneItemEnabled"])
            self.assertFalse(any(r["requestType"] == "SetSceneItemEnabled" and r["requestData"]["sceneItemEnabled"] for r in obs.requests))
