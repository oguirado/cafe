"""Closed private-pipe service requests; never a public broker capability.

The Rust parent must validate the same body before writing its owned pipe.
No endpoint, executable, filesystem path or arbitrary OBS request is accepted.
Validation errors contain fixed codes only, never rejected values.
"""
import re

_CHANNEL = re.compile(r"[a-z0-9_]{1,25}\Z")
_CAPSULE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_STATES = {"absent", "configured", "closed", "failed"}
_CHAT = {"idle", "running", "succeeded", "failed", "cancelled"}
_CODES = {"closed", "not_configured", "already_running", "operation_uncertain", "unavailable"}


def _keys(value, names):
    return type(value) is dict and set(value) == set(names)


def _text(value, maximum):
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= maximum and "\0" not in value
    except UnicodeError:
        return False


def validate_crops(feeds, crops):
    if (type(feeds) is not dict or not feeds or not set(feeds) <= {"game", "camera"}
            or type(crops) is not dict or set(crops) != set(feeds)):
        raise ValueError("service_request_rejected")
    for crop in crops.values():
        if (not _keys(crop, ("x", "y", "width", "height"))
                or any(type(v) is not int or not 0 <= v <= 4294967295 for v in crop.values())
                or crop["width"] == 0 or crop["height"] == 0
                or crop["x"] + crop["width"] > 4294967295
                or crop["y"] + crop["height"] > 4294967295):
            raise ValueError("service_request_rejected")


def validate_service_request(body):
    """Reject before a handler, network call, task or file operation can run."""
    good = False
    if type(body) is dict:
        op = body.get("op")
        if op in ("status", "close"):
            good = _keys(body, ("op",))
        elif op == "configure_obs" and _keys(body, ("op", "port", "password")):
            choice = body["password"]
            good = type(body["port"]) is int and 1 <= body["port"] <= 65535
            if _keys(choice, ("mode",)) and choice["mode"] == "absent":
                pass
            elif _keys(choice, ("mode", "value")) and choice["mode"] == "provided" and _text(choice["value"], 1024):
                pass
            else:
                good = False
        elif op == "start_chat" and _keys(body, ("op", "channel")):
            good = type(body["channel"]) is str and _CHANNEL.fullmatch(body["channel"]) is not None
        elif op == "send_capsule" and _keys(body, ("op", "capsule_id", "feeds", "crops")):
            feeds = body["feeds"]
            good = (type(body["capsule_id"]) is str and _CAPSULE.fullmatch(body["capsule_id"]) is not None
                    and type(feeds) is dict and bool(feeds) and set(feeds) <= {"game", "camera"})
            if good:
                descriptors = list(feeds.values())
                good = all(_text(v, 4096) and not any(c in v for c in "\r\n")
                           and len(v.split(":")) == 3 and all(v.split(":")) for v in descriptors)
                if good:
                    good = len(set(descriptors)) == len(descriptors)
            if good:
                validate_crops(feeds, body["crops"])
    if not good:
        raise ValueError("service_request_rejected")
    return body


def validate_service_result(body):
    """Only bounded nonsecret state crosses back to the authority/UI."""
    names = ("accepted", "closed", "obs", "obs_code", "chat", "chat_code", "chat_connected",
             "messages", "malformed", "reconnects", "connections")
    good = (_keys(body, names) and type(body["accepted"]) is bool and type(body["closed"]) is bool
            and type(body["obs"]) is str and body["obs"] in _STATES
            and type(body["chat"]) is str and body["chat"] in _CHAT)
    if good and body['chat_connected'] is not None and type(body['chat_connected']) is not bool:
        good = False
    if good:
        for name in ("obs_code", "chat_code"):
            value = body[name]
            if value is not None and (type(value) is not str or value not in _CODES):
                good = False
        for name in ("messages", "malformed", "reconnects", "connections"):
            value = body[name]
            if value is not None and not (type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,19}", value)
                                          and int(value) <= 18446744073709551615):
                good = False
    if not good:
        raise ValueError("service_result_rejected")
    return body
