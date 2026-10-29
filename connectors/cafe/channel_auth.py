"""Closed bounded framing and one-use broker HMAC, using only the standard library.

The installation rendezvous is private, never an environment variable or a log.
Windows uses a local named pipe with checked account SID and owner-only ACL.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import time

MAX_LINE = 16384
SCHEMA = 'cafe.connector.v1'


def _clock(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == '0'):
        raise ValueError('invalid_clock')
    number = int(value)
    if not 0 <= number <= (1 << 64) - 1:
        raise ValueError('invalid_clock')
    return number


def _keys(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def make_frame(kind, epoch, body):
    return dict(schema=SCHEMA, kind=kind, request_id='channel', authority_epoch=str(epoch), expected_revision='0', body=body)


def validate_lease(body):
    if not _keys(body, ('token', 'expires_unix_ms')) or not isinstance(body['token'], str) or not 1 <= len(body['token'].encode()) <= 2048:
        raise ValueError('invalid_lease')
    expiry = _clock(body['expires_unix_ms'])
    remaining = expiry - time.time_ns() // 1_000_000
    if not 0 < remaining <= 5000:
        raise ValueError('expired_lease')


def validate_frame(frame, epoch, from_authority=True):
    if not _keys(frame, ('schema', 'kind', 'request_id', 'authority_epoch', 'expected_revision', 'body')):
        raise ValueError('invalid_frame')
    actual = _clock(frame['authority_epoch'])
    if actual == 0 or frame['schema'] != SCHEMA or frame['request_id'] != 'channel' or frame['expected_revision'] != '0':
        raise ValueError('invalid_frame')
    if epoch is None:
        if not from_authority or frame['kind'] != 'connector_start':
            raise ValueError('startup_required')
    elif actual != epoch:
        raise ValueError('stale_epoch')
    kind, body = frame['kind'], frame['body']
    if from_authority and kind == 'connector_start' and _keys(body, ()):
        return actual
    if from_authority and kind == 'token_lease':
        validate_lease(body)
        return actual
    if from_authority and kind == 'approved_effect' and _keys(body, ('action',)) and isinstance(body['action'], str) and 1 <= len(body['action'].encode()) <= 64:
        return actual
    if from_authority and kind == 'service_request':
        from .runtime_service_auth import validate_service_request
        validate_service_request(body)
        return actual
    if not from_authority and kind == 'observation' and _keys(body, ('ready',)) and type(body['ready']) is bool:
        return actual
    if not from_authority and kind == 'effect_result' and _keys(body, ('accepted',)) and type(body['accepted']) is bool:
        return actual
    if not from_authority and kind == 'service_result':
        from .runtime_service_auth import validate_service_result
        validate_service_result(body)
        return actual
    raise ValueError('capability_denied')


async def read_frame(reader):
    try:
        line = await asyncio.wait_for(reader.readline(), 5)
    except (ValueError, asyncio.LimitOverrunError, asyncio.TimeoutError):
        raise ValueError('invalid_frame') from None
    if not line or len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ValueError('invalid_frame')
    try:
        return json.loads(line)
    except (ValueError, UnicodeError):
        raise ValueError('invalid_frame') from None


async def write_frame(writer, value):
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(data) > MAX_LINE:
        raise ValueError('frame_too_large')
    writer.write(data)
    await asyncio.wait_for(writer.drain(), 5)


async def receive_ticket(directory):
    """Consume one private platform bootstrap; metadata never contains a secret."""
    directory = Path(directory)
    if os.name == 'nt':
        from .windows_auth import _receive_ticket
        ticket = await asyncio.to_thread(_receive_ticket, directory, MAX_LINE)
    elif os.name == 'posix':
        d = directory.lstat()
        socket = directory / 'broker.sock'
        s = socket.lstat()
        if not stat.S_ISDIR(d.st_mode) or d.st_uid != os.getuid() or stat.S_IMODE(d.st_mode) != 0o700 or not stat.S_ISSOCK(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
            raise ValueError('insecure_rendezvous')
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (d.st_dev, d.st_ino):
                raise ValueError('rendezvous_changed')
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection('/proc/self/fd/' + str(fd) + '/broker.sock', limit=MAX_LINE), 5)
        finally:
            os.close(fd)
        try:
            ticket = await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()
    else:
        raise ValueError('rendezvous_unsupported')
    if not _keys(ticket, ('schema', 'host', 'port', 'epoch', 'identity', 'secret')) or ticket['schema'] != 'cafe.rendezvous.v1' or ticket['host'] != '127.0.0.1' or ticket['identity'] != 'broker' or type(ticket['port']) is not int or not 0 < ticket['port'] <= 65535 or _clock(ticket['epoch']) == 0:
        raise ValueError('invalid_ticket')
    secret = ticket['secret']
    if not isinstance(secret, str) or len(secret) != 64 or any(c not in '0123456789abcdef' for c in secret):
        raise ValueError('invalid_ticket')
    return ticket


async def connect_broker(ticket, capability='snapshot', identity='broker', epoch=None):
    """One authenticated connection and one bounded identity-scoped request."""
    if ticket.get('host') != '127.0.0.1':
        raise ValueError('loopback_only')
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', ticket['port'], limit=MAX_LINE), 5)
    try:
        challenge = await read_frame(reader)
        if not _keys(challenge, ('schema', 'identity', 'epoch', 'nonce')) or challenge['schema'] != 'cafe.broker.auth.v1' or challenge['identity'] != 'broker' or challenge['epoch'] != ticket['epoch'] or not isinstance(challenge['nonce'], str) or len(challenge['nonce']) != 64 or any(c not in '0123456789abcdef' for c in challenge['nonce']):
            raise ValueError('invalid_challenge')
        message = ('cafe.broker.auth.v1\n' + identity + '\n' + ticket['epoch'] + '\n' + challenge['nonce']).encode()
        proof = hmac.new(bytes.fromhex(ticket['secret']), message, hashlib.sha256).hexdigest()
        await write_frame(writer, dict(identity=identity, epoch=ticket['epoch'], proof=proof))
        response = await read_frame(reader)
        if response != {'authenticated': True}:
            raise ValueError('authentication_failed')
        await write_frame(writer, dict(epoch=ticket['epoch'] if epoch is None else epoch, capability=capability, body={}))
        return await read_frame(reader)
    except (OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
        raise ValueError('broker_rejected') from None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
