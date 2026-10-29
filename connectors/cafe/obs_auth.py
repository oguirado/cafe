"""Closed OBS authentication and admission; secrets never enter diagnostics.

The password is consumed only by Identify construction. No API persists it;
connection logs are disabled and provider errors are never exposed verbatim.
"""
import asyncio
import base64
import hashlib
import json
import logging
import math
import re
from websockets import __version__ as _websockets_version
from websockets.asyncio.client import connect as _connect

_CODES = frozenset({'loopback_only', 'invalid_endpoint', 'invalid_password',
                    'authentication_failed', 'unsupported_rpc', 'malformed_response',
                    'connection_failed', 'connection_lost', 'request_failed',
                    'invalid_capsule', 'ambiguous_window', 'source_conflict',
                    'dependency_unavailable', 'not_connected', 'operation_uncertain'})
_LIMIT = 65536


class ObsError(RuntimeError):
    """Only fixed nonsecret codes are allowed across this boundary."""
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in _CODES else 'request_failed')


def safe_logger():
    """No ancestor/root handler can observe websocket frames or close reasons."""
    logger = logging.Logger('cafe.obs.private', level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def validate_endpoint(host, port):
    if type(host) is not str or host != '127.0.0.1':
        raise ObsError('loopback_only')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ObsError('invalid_endpoint')


def password_presence(password):
    if password is not None:
        if type(password) is not str:
            raise ObsError('invalid_password')
        invalid = False
        try:
            invalid = len(password.encode('utf-8')) > 2048
        except UnicodeError:
            invalid = True
        if invalid:
            raise ObsError('invalid_password')
    return {'obs_password_present': bool(password)}


class _LocalConnect(_connect):
    def process_redirect(self, exc):
        # websockets17.2 explicitly allows an Exception return to reject redirects.
        # Never permit the library to reinterpret an untrusted Location URI.
        return ObsError('connection_failed')


async def open_local_connection(host, port):
    validate_endpoint(host, port)
    if _websockets_version != '17.2':
        raise ObsError('dependency_unavailable')
    failed = False
    try:
        return await _LocalConnect('ws://127.0.0.1:' + str(port), host='127.0.0.1', port=port,
                                   proxy=None, compression=None, open_timeout=5,
                                   close_timeout=1, ping_interval=None, max_size=_LIMIT,
                                   max_queue=4, write_limit=16384, logger=safe_logger())
    except asyncio.CancelledError:
        raise
    except Exception:
        failed = True
    # Outside the except suite: no provider exception context/close reason escapes.
    if failed:
        raise ObsError('connection_failed')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = item
    return value


def _invalid_number(value):
    raise ValueError('non_json_number')


def decode_message(raw):
    if type(raw) is not str or len(raw.encode('utf-8')) > _LIMIT:
        raise ObsError('malformed_response')
    invalid = False
    try:
        message = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
    except (ValueError, UnicodeError, RecursionError):
        invalid = True
    if invalid:
        raise ObsError('malformed_response')
    if (type(message) is not dict or set(message) != {'op', 'd'} or
            type(message['op']) is not int or type(message['d']) is not dict):
        raise ObsError('malformed_response')
    return message


def identify_payload(hello, password):
    password_presence(password)
    if type(hello) is not dict or type(hello.get('op')) is not int or hello['op'] != 0 or type(hello.get('d')) is not dict:
        raise ObsError('malformed_response')
    data = hello['d']
    if type(data.get('rpcVersion')) is not int:
        raise ObsError('malformed_response')
    if data['rpcVersion'] != 1:
        raise ObsError('unsupported_rpc')
    if (type(data.get('obsWebSocketVersion')) is not str or
            not data['obsWebSocketVersion'].startswith('5.') or
            ('obsStudioVersion' in data and type(data['obsStudioVersion']) is not str)):
        raise ObsError('malformed_response')
    response = {'rpcVersion': 1, 'eventSubscriptions': 0}
    if 'authentication' in data:
        auth = data['authentication']
        if type(auth) is not dict or set(auth) != {'salt', 'challenge'}:
            raise ObsError('malformed_response')
        for value in auth.values():
            if type(value) is not str or not 1 <= len(value) <= 1024:
                raise ObsError('malformed_response')
            invalid = False
            try:
                base64.b64decode(value, validate=True)
            except (ValueError, UnicodeError):
                invalid = True
            if invalid:
                raise ObsError('malformed_response')
        if not password:
            raise ObsError('authentication_failed')
        secret = base64.b64encode(hashlib.sha256((password + auth['salt']).encode('utf-8')).digest()).decode('ascii')
        response['authentication'] = base64.b64encode(hashlib.sha256((secret + auth['challenge']).encode('utf-8')).digest()).decode('ascii')
    return response


def validate_identified(message):
    if (message.get('op') != 2 or type(message.get('d')) is not dict or
            type(message['d'].get('negotiatedRpcVersion')) is not int):
        raise ObsError('malformed_response')
    if message['d']['negotiatedRpcVersion'] != 1:
        raise ObsError('unsupported_rpc')


def response_data(message, request_type, request_id):
    if message.get('op') != 7:
        raise ObsError('malformed_response')
    data = message['d']
    status = data.get('requestStatus')
    if (data.get('requestType') != request_type or data.get('requestId') != request_id or
            type(status) is not dict or type(status.get('result')) is not bool or
            type(status.get('code')) is not int):
        raise ObsError('malformed_response')
    if status['result'] is not True:
        raise ObsError('request_failed')
    if status['code'] != 100:
        raise ObsError('malformed_response')
    result = data.get('responseData', {})
    if type(result) is not dict:
        raise ObsError('malformed_response')
    return result


def validate_capsule(capsule_id, feeds):
    if type(capsule_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', capsule_id):
        raise ObsError('invalid_capsule')
    if type(feeds) is not dict or not feeds or not set(feeds) <= {'game', 'camera'}:
        raise ObsError('invalid_capsule')
    seen = set()
    for role, descriptor in feeds.items():
        if (type(descriptor) is not str or not 1 <= len(descriptor.encode('utf-8')) <= 4096 or
                any(c in descriptor for c in '\r\n\x00') or
                len(descriptor.split(':')) != 3 or not all(descriptor.split(':'))):
            raise ObsError('invalid_capsule')
        if descriptor in seen:
            raise ObsError('ambiguous_window')
        seen.add(descriptor)


def validate_crops(feeds, crops):
    from .runtime_service_auth import validate_crops as check
    try:
        check(feeds, crops)
    except ValueError:
        raise ObsError('invalid_capsule') from None


def _observed_integer(value, minimum=0, maximum=4294967295):
    if type(value) is int and minimum <= value <= maximum:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer() and minimum <= value <= maximum:
        return int(value)
    raise ObsError('malformed_response')


def plan_crop(crop, response):
    # OBS owns the source dimensions. A window rectangle is not substituted for
    # them; only the four reviewed crop fields are sent back to the server.
    validate_crops({'camera': 'unused'}, {'camera': crop})
    if type(response) is not dict or type(response.get('sceneItemTransform')) is not dict:
        raise ObsError('malformed_response')
    observed = response['sceneItemTransform']
    width = _observed_integer(observed.get('sourceWidth'), 1)
    height = _observed_integer(observed.get('sourceHeight'), 1)
    if crop['x'] + crop['width'] > width or crop['y'] + crop['height'] > height:
        raise ObsError('invalid_capsule')
    transform = {'cropLeft': crop['x'], 'cropTop': crop['y'],
                 'cropRight': width - crop['x'] - crop['width'],
                 'cropBottom': height - crop['y'] - crop['height']}
    if any(value > 100000 for value in transform.values()):
        raise ObsError('invalid_capsule')
    return {'transform': transform, 'source_width': width, 'source_height': height}


def verify_crop(plan, response):
    if (type(plan) is not dict or set(plan) != {'transform', 'source_width', 'source_height'}
            or type(plan['transform']) is not dict
            or set(plan['transform']) != {'cropLeft', 'cropTop', 'cropRight', 'cropBottom'}
            or type(response) is not dict or type(response.get('sceneItemTransform')) is not dict):
        raise ObsError('malformed_response')
    transform = response['sceneItemTransform']
    if (_observed_integer(transform.get('sourceWidth'), 1) != plan['source_width']
            or _observed_integer(transform.get('sourceHeight'), 1) != plan['source_height']):
        raise ObsError('request_failed')
    for name, value in plan['transform'].items():
        if type(value) is not int or not 0 <= value <= 100000:
            raise ObsError('malformed_response')
        if _observed_integer(transform.get(name), 0, 100000) != value:
            raise ObsError('request_failed')
