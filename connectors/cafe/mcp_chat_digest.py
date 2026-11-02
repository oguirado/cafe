"""Deterministic, read-only statistics over an already-loaded chat snapshot.

Word statistics: normalize each message `text` by Unicode case folding,
splitting on any sequence that is not a letter or decimal number, dropping
empty tokens and tokens longer than 64 characters, without stemming.
Count case-folded words exactly once per occurrence.

Integration belongs immediately after the existing DataReader path has been
fully materialized by the trusted caller. Pass its original stats dictionary;
never pass its lazy file-reading generator here. DataReader supplies ordinary
dicts without provenance markers, so this frozen API can validate their shape
but cannot attest their origin. The caller owns that trust boundary. In-memory
iterables must not perform I/O when advanced. No registration or server changes
are made by this module; the existing six tools keep their current behavior.

Snapshot identity is content-addressed: all supplied events, in order, and the
unchanged scan dictionary define it. Equal contents share an identity. Cursors
are opaque deterministic digest commitments to that identity, the exact query,
and a page offset; no chat, login, clock, or query is encoded as readable text.
DataReader cannot attribute damaged lines to a narrower interval, so any scan
damage conservatively makes the whole aggregate incomplete. The caller must
retain the existing final JSON-RPC frame cap (its request ID is not passed here).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from hashlib import sha256
import io
import json
import re
from types import GeneratorType
from typing import Literal, TypedDict

from .mcp_server import (
    DataReader, MAX_EVENT_LINE, MAX_FILES, MAX_REQUEST, MAX_RESPONSE, MAX_SCAN_BYTES,
)


__all__ = ['ReadResult', 'execute', 'tool_definitions']

ErrorCode = Literal[
    'invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
    'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large',
]


class _Page(TypedDict):
    next_cursor: str | None
    complete: bool


class ReadResult(TypedDict):
    schema: Literal['cafe.mcp_read.v1']
    availability: Literal['available', 'unavailable']
    data: dict | list | None
    page: _Page
    scan: dict | None
    error: ErrorCode | None


_NANO = 1_000_000_000
_MAX_CLOCK = '18446744073709551615'
_MAX_BUCKETS = 1000
_UTC_PATTERN = (
    r'([0-9]{4})-([0-9]{2})-([0-9]{2})T'
    r'([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z'
)
_UTC_RE = re.compile(_UTC_PATTERN)
_CHANNEL_RE = re.compile(r'[a-z0-9_]{3,25}')
_SCAN_KEYS = frozenset({
    'corrupt_lines', 'truncated_lines', 'bytes_scanned', 'files_scanned', 'scan_limited',
})
_EVENT_KEYS = frozenset({
    'schema', 'channel', 'received_at', 'sent_at', 'message_id', 'user_login',
    'display_name', 'badges', 'text', 'kind', 'session_id',
})
_KINDS = frozenset({'message', 'join_state', 'notice', 'clear', 'reconnect'})
_NAMES = ('chat_statistics', 'chat_activity')
_REQUIRED = frozenset({'channel', 'from', 'to'})
_COMMON = _REQUIRED | {'bucket_seconds', 'top_limit'}


def tool_definitions() -> list[dict]:
    """Return fresh, closed schemas for exactly the two read-only tools."""
    definitions = []
    for name in _NAMES:
        properties = {
            'channel': {'type': 'string', 'pattern': '^[a-z0-9_]{3,25}$',
                        'minLength': 3, 'maxLength': 25},
            'from': {'type': 'string', 'pattern': '^' + _UTC_PATTERN + '$',
                     'minLength': 20, 'maxLength': 30},
            'to': {'type': 'string', 'pattern': '^' + _UTC_PATTERN + '$',
                   'minLength': 20, 'maxLength': 30},
            'bucket_seconds': {'type': 'integer', 'minimum': 1,
                               'maximum': 3600, 'default': 60},
            'top_limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10},
        }
        if name == 'chat_activity':
            properties.update({
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
                'cursor': {'type': ['string', 'null'], 'maxLength': 512,
                           'description': 'Opaque cursor, at most 512 UTF-8 bytes.'},
            })
        definitions.append({
            'name': name,
            'description': 'Read-only loaded chat history: ' + name,
            'inputSchema': {'type': 'object', 'additionalProperties': False,
                            'required': ['channel', 'from', 'to'], 'properties': properties},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                            'idempotentHint': True, 'openWorldHint': False},
        })
    return definitions


def _utc_ns(value):
    if type(value) is not str or not 20 <= len(value) <= 30:
        raise ValueError
    match = _UTC_RE.fullmatch(value)
    if match is None:
        raise ValueError
    moment = datetime(*(int(match[index]) for index in range(1, 7)))
    seconds = ((moment.toordinal() * 24 + moment.hour) * 60 + moment.minute) * 60
    return (seconds + moment.second) * _NANO + int((match[7] or '0').ljust(9, '0'))


def _stamp(instant, precision):
    seconds, fraction = divmod(instant, _NANO)
    ordinal, seconds = divmod(seconds, 86400)
    day = date.fromordinal(ordinal)
    hour, seconds = divmod(seconds, 3600)
    minute, second = divmod(seconds, 60)
    suffix = '.' + f'{fraction:09d}'[:precision] if precision else ''
    return (f'{day.year:04d}-{day.month:02d}-{day.day:02d}T'
            f'{hour:02d}:{minute:02d}:{second:02d}{suffix}Z')


def _clock(value):
    # Clock strings are never converted, sorted numerically, or rewritten.
    return (type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,19}', value) is not None
            and (len(value) < 20 or value <= _MAX_CLOCK))


def _integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def _arguments(name, arguments):
    if type(name) is not str or name not in _NAMES or type(arguments) is not dict:
        raise ValueError
    allowed = _COMMON | {'limit', 'cursor'} if name == 'chat_activity' else _COMMON
    if not _REQUIRED <= arguments.keys() or arguments.keys() - allowed:
        raise ValueError
    channel = arguments['channel']
    if type(channel) is not str or _CHANNEL_RE.fullmatch(channel) is None:
        raise ValueError
    start, end = _utc_ns(arguments['from']), _utc_ns(arguments['to'])
    bucket = arguments.get('bucket_seconds', 60)
    top = arguments.get('top_limit', 10)
    limit = arguments.get('limit', 50)
    if (start >= end or not _integer(bucket, 3600) or not _integer(top, 50)
            or not _integer(limit, 200)):
        raise ValueError
    count = (end - start + bucket * _NANO - 1) // (bucket * _NANO)
    if count > _MAX_BUCKETS:
        raise ValueError
    cursor = arguments.get('cursor')
    if cursor is not None and (type(cursor) is not str or len(cursor.encode('utf-8')) > 512):
        raise ValueError
    query = {'channel': channel, 'from': arguments['from'], 'to': arguments['to'],
             'bucket_seconds': bucket, 'top_limit': top, 'limit': limit}
    if len(_json(query)) > MAX_REQUEST:
        raise ValueError
    return query, start, end, count, cursor


def _valid_scan(scan):
    if type(scan) is not dict or scan.keys() != _SCAN_KEYS or type(scan['scan_limited']) is not bool:
        return False
    # These are the existing reader's physical maxima, including its final read.
    for key in _SCAN_KEYS - {'scan_limited'}:
        maximum = MAX_FILES if key == 'files_scanned' else MAX_SCAN_BYTES + MAX_EVENT_LINE + 1
        if type(scan[key]) is not int or not 0 <= scan[key] <= maximum:
            return False
    return True


def _event_ns(event):
    if type(event) is not dict or set(event) not in (_EVENT_KEYS, _EVENT_KEYS - {'user_login'}):
        raise ValueError
    received = event['received_at']
    login = event.get('user_login')
    badges = event['badges']
    if not (
        type(event['schema']) is str and event['schema'] == 'cafe.chat_event.v1'
        and type(event['channel']) is str and _CHANNEL_RE.fullmatch(event['channel'])
        and type(received) is dict and received.keys() == {'monotonic_ns', 'utc'}
        and _clock(received['monotonic_ns'])
        and (event['sent_at'] is None or _clock(event['sent_at']))
        and (event['message_id'] is None or type(event['message_id']) is str
             and 1 <= len(event['message_id']) <= 128)
        and (login is None or type(login) is str and re.fullmatch(r'[a-z0-9_]{1,25}', login))
        and (event['display_name'] is None or type(event['display_name']) is str
             and len(event['display_name']) <= 128)
        and type(badges) is list and len(badges) <= 64
        and all(type(badge) is dict and badge.keys() == {'name', 'version'}
                and all(type(value) is str and 1 <= len(value) <= 64 for value in badge.values())
                for badge in badges)
        and type(event['text']) is str and len(event['text']) <= 16384
        and type(event['kind']) is str and event['kind'] in _KINDS
        and type(event['session_id']) is str
        and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}', event['session_id'])
    ):
        raise ValueError
    return _utc_ns(received['utc'])


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _words(text):
    token = []
    length = 0
    for char in text.casefold():
        if char.isalpha() or char.isdecimal():
            length += 1
            if length <= 64:
                token.append(char)
        else:
            if 0 < length <= 64:
                yield ''.join(token)
            token = []
            length = 0
    if 0 < length <= 64:
        yield ''.join(token)


def _aggregate(query, start, end, count, events, scan):
    if (events is None or isinstance(events, (str, bytes, bytearray, dict, io.IOBase))
            or type(events) is GeneratorType and events.gi_code is DataReader.events.__code__):
        raise ValueError
    scan_before = _json(scan)
    fingerprint = sha256(b'cafe.mcp_jsonl_stats.snapshot.v1\x00')
    counts = [0] * count
    words = Counter()
    chatters = set()
    messages = 0
    loaded_bytes = 0
    for event in events:
        instant = _event_ns(event)
        raw = _json(event) + b'\n'
        loaded_bytes += len(raw)
        if len(raw) > MAX_EVENT_LINE or loaded_bytes > MAX_SCAN_BYTES:
            raise ValueError
        fingerprint.update(raw)
        if event['kind'] != 'message' or event['channel'] != query['channel'] or not start <= instant < end:
            continue
        counts[(instant - start) // (query['bucket_seconds'] * _NANO)] += 1
        messages += 1
        login = event.get('user_login')
        if login is not None:
            chatters.add(login)
        words.update(_words(event['text']))
    if not _valid_scan(scan) or _json(scan) != scan_before:
        raise ValueError
    fingerprint.update(b'\x00scan\x00' + scan_before)
    precision = len(query['from'].split('.')[1]) - 1 if '.' in query['from'] else 0
    buckets = [{'start': _stamp(start + index * query['bucket_seconds'] * _NANO, precision),
                'count': total} for index, total in enumerate(counts)]
    maximum = max(counts)
    complete = not (scan['scan_limited'] or scan['corrupt_lines'] or scan['truncated_lines'])
    data = {
        'message_count': messages, 'unique_chatters': len(chatters),
        'messages_per_bucket': buckets,
        'top_words': [{'word': word, 'count': total} for word, total in
                      sorted(words.items(), key=lambda item: (-item[1], item[0]))[:query['top_limit']]],
        'peaks': [dict(bucket) for bucket in buckets if maximum > 0 and bucket['count'] == maximum],
        'top_emotes': None, 'emotes_status': 'metadata_unavailable', 'complete': complete,
    }
    return data, fingerprint.hexdigest()


def _cursor(query, fingerprint, offset):
    return sha256(b'cafe.mcp_jsonl_stats.cursor.v1\x00'
                  + _json([query, fingerprint, offset])).hexdigest()


def _failure(code: ErrorCode, scan) -> ReadResult:
    return {'schema': 'cafe.mcp_read.v1', 'availability': 'unavailable', 'data': None,
            'page': {'next_cursor': None, 'complete': False},
            'scan': scan if _valid_scan(scan) else None, 'error': code}


def _fits_response(result):
    # Bound the current server's data+scan wrapper and its duplicated MCP text.
    # Reserve 4,300 additional bytes for numeric IDs; the caller still applies
    # its authoritative final frame limit, including unusually large IDs.
    payload = {'data': result, 'scan': result['scan']}
    text = _json(payload).decode('utf-8')
    frame = {'jsonrpc': '2.0', 'id': '\x00' * 96,
             'result': {'content': [{'type': 'text', 'text': text}],
                        'structuredContent': payload, 'isError': result['error'] is not None}}
    return len(json.dumps(frame, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 4301 <= MAX_RESPONSE


def execute(name: str, arguments: dict, events, *, scan: dict) -> ReadResult:
    """Compute one closed result in a single pass, without I/O or control calls."""
    try:
        query, start, end, count, cursor = _arguments(name, arguments)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _failure('invalid_arguments', scan)
    if not _valid_scan(scan):
        return _failure('provider_unavailable', scan)
    try:
        aggregate, fingerprint = _aggregate(query, start, end, count, events, scan)
    except Exception:
        # Iterator, validation, and encoding failures never disclose their text.
        return _failure('provider_unavailable', scan)
    complete = aggregate['complete']
    following = None
    data = aggregate
    if name == 'chat_activity':
        offset = 0
        if cursor is not None:
            offset = next((candidate for candidate in range(query['limit'], count, query['limit'])
                           if cursor == _cursor(query, fingerprint, candidate)), None)
            if offset is None:
                return _failure('stale_cursor', scan)
        end_offset = offset + query['limit']
        if end_offset < count:
            following = _cursor(query, fingerprint, end_offset)
        data = {'channel': query['channel'], 'from': query['from'], 'to': query['to'],
                'bucket_seconds': query['bucket_seconds'],
                'buckets': aggregate['messages_per_bucket'][offset:end_offset], 'complete': complete}
    result: ReadResult = {
        'schema': 'cafe.mcp_read.v1', 'availability': 'available', 'data': data,
        'page': {'next_cursor': following, 'complete': complete}, 'scan': scan,
        'error': None if complete else 'scan_limited',
    }
    try:
        if _fits_response(result):
            return result
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    return _failure('response_too_large', scan)
