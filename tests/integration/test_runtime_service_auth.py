"""Handwritten private service contract, before any real side effect."""
import copy
import unittest

from connectors.cafe.runtime_service_auth import validate_service_request, validate_service_result


class ServiceContract(unittest.TestCase):
    def reject(self, value):
        calls = []
        with self.assertRaisesRegex(ValueError, "^service_request_rejected$"):
            validate_service_request(value)
            calls.append("effect")
        self.assertEqual(calls, [])

    def test_only_named_closed_operations(self):
        known = [{"op": "status"}, {"op": "close"},
                 {"op": "configure_obs", "port": 4455, "password": {"mode": "absent"}},
                 {"op": "configure_obs", "port": 4455, "password": {"mode": "provided", "value": "SYNTHETIC_CANARY"}},
                 {"op": "start_chat", "channel": "synthetic_channel"},
                 {"op": "send_capsule", "capsule_id": "guest-one", "feeds": {"game": "Game:Fixture:game.exe", "camera": "Call:Fixture:chat.exe"}, "crops": {"game": {"x": 0, "y": 0, "width": 1280, "height": 720}, "camera": {"x": 10, "y": 20, "width": 320, "height": 240}}}]
        for body in known:
            with self.subTest(op=body["op"]):
                self.assertEqual(validate_service_request(body), body)
                self.reject({**body, "unknown": True})
        for body in [None, [], {}, {"op": "eval"}, {"op": []}, {"op": "token_lease"}]:
            self.reject(body)

    def test_caller_cannot_choose_endpoint_program_or_path(self):
        base = {"op": "configure_obs", "port": 4455, "password": {"mode": "absent"}}
        for extra in [{"host": "127.0.0.1"}, {"host": "0.0.0.0"}, {"url": "wss://synthetic.invalid"}, {"program": "synthetic"}]:
            self.reject({**base, **extra})
        for extra in [{"destination": "relative.jsonl"}, {"url": "wss://synthetic.invalid"}]:
            self.reject({"op": "start_chat", "channel": "synthetic", **extra})

    def test_obs_password_variants_are_closed_and_nonreflecting(self):
        for password in [None, "SYNTHETIC_CANARY", {"mode": "absent", "value": "SYNTHETIC_CANARY"},
                         {"mode": "provided"}, {"mode": "provided", "value": ""},
                         {"mode": "provided", "value": "x" * 1025},
                         {"mode": "provided", "value": "\ud800"},
                         {"mode": "provided", "value": "SYNTHETIC_CANARY", "extra": True}]:
            self.reject({"op": "configure_obs", "port": 4455, "password": password})
        for port in [False, 0, -1, 65536, "4455", 4455.0]:
            self.reject({"op": "configure_obs", "port": port, "password": {"mode": "absent"}})

    def test_channel_not_arbitrary_irc_or_url(self):
        for value in ["", "#guest", "Uppercase", "guest\r\nJOIN #other", "a" * 26, "wss://synthetic.invalid", 7]:
            self.reject({"op": "start_chat", "channel": value})

    def test_capsule_feeds_cannot_inject_unknown_requests_or_ambiguous_descriptors(self):
        valid = {"op": "send_capsule", "capsule_id": "guest", "feeds": {"game": "Game:Fixture:game.exe"}, "crops": {"game": {"x": 0, "y": 0, "width": 1280, "height": 720}}}
        for feeds in [{}, [], {"audio": "Audio:Fixture:app.exe"}, {"game": "bad"},
                      {"game": "Title:with:colon:app.exe"}, {"game": "Call:Fixture:app.exe", "camera": "Call:Fixture:app.exe"},
                      {"game": "Call\n:Fixture:app.exe"}, {"game": "Call:Fixture:"}, {"game": "\ud800"}]:
            self.reject({**valid, "feeds": feeds})
        self.reject({**valid, "capsule_id": "guest:other"})

    def test_result_has_no_secret_reflection_and_uses_decimal_counter_strings(self):
        base = {"accepted": True, "closed": False, "obs": "configured", "obs_code": None,
                "chat": "running", "chat_code": None, "chat_connected": None, "messages": None, "malformed": None,
                "reconnects": None, "connections": None}
        self.assertEqual(validate_service_result(base), base)
        self.assertEqual(validate_service_result({**base, "messages": "9007199254740993"})["messages"], "9007199254740993")
        invalid = [{**base, "password": "SYNTHETIC_CANARY"}, {**base, "obs_code": "SYNTHETIC_CANARY"},
                   {**base, "chat": "connected"}, {**base, "obs": []}, {**base, "accepted": 1}]
        invalid += [{**base, "messages": value} for value in [0, True, "01", "-1", "18446744073709551616", "SYNTHETIC_CANARY"]]
        for value in invalid:
            with self.assertRaisesRegex(ValueError, "^service_result_rejected$"):
                validate_service_result(copy.deepcopy(value))
