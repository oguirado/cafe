"""Bounded stdlib-only MCP stdio framing over confined read-only chat files."""
import argparse
from datetime import datetime
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

PROTOCOL_VERSION = '2025-11-25'
MAX_REQUEST = 65536
MAX_RESPONSE = 65536
MAX_EVENT_LINE = 131072
MAX_SCAN_BYTES = 33554432
MAX_FILES = 4096
_EVENT_FIELDS = frozenset({'schema', 'channel', 'received_at', 'sent_at', 'message_id',
                          'user_login', 'display_name', 'badges', 'text', 'kind', 'session_id'})
_TOOL_ARGUMENTS = {
    'list_channels_and_days': {},
    'recent_messages': {'channel': 'channel', 'limit': 'limit', 'since': 'utc'},
    'search_messages': {'channel': 'channel', 'query': 'query', 'limit': 'limit'},
    'top_chatters': {'channel': 'channel', 'window_minutes': 'window'},
    'message_rate': {'channel': 'channel', 'bucket_seconds': 'bucket'},
    'session_summary': {'channel': 'channel', 'day': 'day'},
}
_REQUIRED = {
    'list_channels_and_days': set(), 'recent_messages': {'channel'},
    'search_messages': {'channel', 'query'}, 'top_chatters': {'channel', 'window_minutes'},
    'message_rate': {'channel', 'bucket_seconds'}, 'session_summary': {'channel', 'day'},
}
_CONTROL_IDENTIFIER = r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}'
_CONTROL_ARGUMENTS = {
    'select_capsule': {'capsule_id': 'control_id'},
    'start_recording': {'request_id': 'control_id', 'capsule_id': 'control_id'},
    'stop_recording': {'request_id': 'control_id', 'capsule_id': 'control_id'},
    'send_to_obs': {'request_id': 'control_id', 'capsule_id': 'control_id'},
}
_CONTROL_REQUIRED = {
    'select_capsule': {'capsule_id'}, 'start_recording': {'request_id'},
    'stop_recording': {'request_id'}, 'send_to_obs': {'request_id'},
}
_CONTROL_TOOL_NAMES = frozenset(_CONTROL_ARGUMENTS)
_CODES = {-32700: 'Parse error', -32600: 'Invalid Request',
          -32601: 'Method not found', -32602: 'Invalid params'}
_READ_ARGUMENTS = {
    'list_capsules': {'limit': 'page_limit', 'cursor': 'cursor'},
    'list_candidate_windows': {'limit': 'page_limit', 'cursor': 'cursor'},
    'recording_status': {'capsule_id': 'control_id'},
    'obs_status': {},
    'messages_by_user': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
                         'limit': 'page_limit', 'cursor': 'cursor'},
    'messages_between': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
                         'limit': 'page_limit', 'cursor': 'cursor'},
    'chat_statistics': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
                        'bucket_seconds': 'bucket', 'top_limit': 'top_limit'},
    'chat_activity': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
                      'limit': 'page_limit', 'cursor': 'cursor'},
    'demo_guide': {'step': 'step'},
}
_RUNTIME_READS = frozenset({'list_capsules', 'list_candidate_windows',
                          'recording_status', 'obs_status'})
_CHAT_READS = frozenset({'messages_by_user', 'messages_between'})
_STATISTICS_READS = frozenset({'chat_statistics', 'chat_activity'})
_READ_ERRORS = frozenset({'invalid_arguments', 'provider_unavailable',
                         'invalid_snapshot', 'stale_cursor', 'query_unavailable',
                         'scan_limited', 'response_too_large'})
_GUIDE_URI = 'cafe://demo/guide'


class DataError(ValueError):
    pass


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = child
    return value


def _not_json(value):
    raise ValueError('non_json_number')


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_not_json)


def _canonical_clock(value):
    return (type(value) is str and bool(re.fullmatch(r'0|[1-9][0-9]{0,19}', value))
            and int(value) <= (1 << 64) - 1)


def _utc(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z', value):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def _valid_event(value):
    try:
        return (type(value) is dict and set(value) == _EVENT_FIELDS and
                value['schema'] == 'cafe.chat_event.v1' and
                type(value['channel']) is str and bool(re.fullmatch(r'[a-z0-9_]{3,25}', value['channel'])) and
                type(value['received_at']) is dict and set(value['received_at']) == {'monotonic_ns', 'utc'} and
                _canonical_clock(value['received_at']['monotonic_ns']) and _utc(value['received_at']['utc']) and
                (value['sent_at'] is None or _canonical_clock(value['sent_at'])) and
                (value['message_id'] is None or type(value['message_id']) is str and 1 <= len(value['message_id']) <= 128) and
                (value['user_login'] is None or type(value['user_login']) is str and bool(re.fullmatch(r'[a-z0-9_]{1,25}', value['user_login']))) and
                (value['display_name'] is None or type(value['display_name']) is str and len(value['display_name']) <= 128) and
                type(value['badges']) is list and len(value['badges']) <= 64 and
                all(type(b) is dict and set(b) == {'name', 'version'} and all(type(x) is str and 1 <= len(x) <= 64 for x in b.values()) for b in value['badges']) and
                type(value['text']) is str and len(value['text']) <= 16384 and
                type(value['kind']) is str and value['kind'] in {'message', 'join_state', 'notice', 'clear', 'reconnect'} and
                type(value['session_id']) is str and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}', value['session_id'])))
    except (TypeError, ValueError, OverflowError):
        return False


def _safe_stat(info, directory=False):
    okay = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not okay or not isinstance(info.st_ino, int) or info.st_ino <= 0:
        raise DataError('unsafe_data_path')
    if os.name == 'nt':
        attributes = getattr(info, 'st_file_attributes', None)
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', None)
        if attributes is None or reparse is None or attributes & reparse:
            raise DataError('unsafe_data_path')


def _directory(path):
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise DataError('unsafe_data_path')
    _safe_stat(path.lstat(), directory=True)


def _canonical_root(value):
    try:
        supplied = Path(value)
        if '..' in supplied.parts:
            raise DataError('unsafe_data_path')
        raw = supplied.absolute()
        components = tuple(reversed(raw.parents)) + (raw,)
        identities = []
        for component in components:
            info = component.lstat()
            _safe_stat(info, directory=True)
            identities.append((info.st_dev, info.st_ino))
        canonical = raw.resolve(strict=True)
        for component, identity in zip(components, identities):
            info = component.lstat()
            _safe_stat(info, directory=True)
            if (info.st_dev, info.st_ino) != identity:
                raise DataError('unsafe_data_path')
        info = canonical.lstat()
        _safe_stat(info, directory=True)
        if (info.st_dev, info.st_ino) != identities[-1]:
            raise DataError('unsafe_data_path')
        _directory(canonical)
        return canonical
    except (OSError, ValueError, TypeError):
        raise DataError('unsafe_data_path') from None


class DataReader:
    def __init__(self, root):
        self.root = _canonical_root(root)
        self.stats = {'corrupt_lines': 0, 'truncated_lines': 0, 'bytes_scanned': 0,
                      'files_scanned': 0, 'scan_limited': False}

    def events(self, channel=None):
        folder = self.root / 'twitch'
        _directory(self.root)
        if not folder.exists() and not folder.is_symlink():
            return
        _directory(folder)
        channels = [folder / channel] if channel is not None else sorted(folder.iterdir())
        count = 0
        for child in channels:
            if not re.fullmatch(r'[a-z0-9_]{3,25}', child.name):
                raise DataError('unsafe_data_path')
            if not child.exists() and not child.is_symlink():
                continue
            _directory(child)
            for path in sorted(child.iterdir()):
                count += 1
                if count > MAX_FILES:
                    self.stats['scan_limited'] = True
                    return
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl', path.name) or path.is_symlink():
                    raise DataError('unsafe_data_path')
                _directory(self.root); _directory(folder); _directory(child)
                before = path.lstat()
                _safe_stat(before)
                flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                         getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
                fd = os.open(path, flags)
                try:
                    info = os.fstat(fd); current = path.stat(follow_symlinks=False)
                    _safe_stat(info); _safe_stat(current)
                    identity = (info.st_dev, info.st_ino)
                    if identity != (before.st_dev, before.st_ino) or identity != (current.st_dev, current.st_ino):
                        raise DataError('unsafe_data_path')
                    _directory(self.root); _directory(folder); _directory(child)
                    with os.fdopen(fd, 'rb') as stream:
                        fd = None
                        self.stats['files_scanned'] += 1
                        while True:
                            line = stream.readline(MAX_EVENT_LINE + 1)
                            if not line:
                                break
                            self.stats['bytes_scanned'] += len(line)
                            if self.stats['bytes_scanned'] > MAX_SCAN_BYTES:
                                self.stats['scan_limited'] = True
                                return
                            if not line.endswith(b'\n'):
                                if len(line) > MAX_EVENT_LINE:
                                    self.stats['corrupt_lines'] += 1
                                    while line and not line.endswith(b'\n'):
                                        line = stream.readline(MAX_EVENT_LINE + 1)
                                        self.stats['bytes_scanned'] += len(line)
                                        if self.stats['bytes_scanned'] > MAX_SCAN_BYTES:
                                            self.stats['scan_limited'] = True
                                            return
                                    continue
                                self.stats['truncated_lines'] += 1
                                break
                            try:
                                value = _loads(line)
                            except (ValueError, UnicodeError, RecursionError):
                                self.stats['corrupt_lines'] += 1
                                continue
                            if not _valid_event(value) or value['channel'] != child.name or value['received_at']['utc'][:10] != path.stem:
                                self.stats['corrupt_lines'] += 1
                                continue
                            yield value
                finally:
                    if fd is not None:
                        os.close(fd)


def _argument_schema(kind):
    if kind == 'channel':
        return {'type': 'string', 'pattern': '^[a-z0-9_]{3,25}$'}
    if kind == 'query':
        return {'type': 'string', 'minLength': 1, 'maxLength': 512}
    if kind in {'utc', 'day'}:
        return {'type': 'string', 'maxLength': 30 if kind == 'utc' else 10}
    return {'type': 'integer', 'minimum': 1,
            'maximum': {'limit': 200, 'window': 1440, 'bucket': 3600}[kind]}


def tools(control_enabled=False, diagnostics_enabled=False, chat_enabled=True, organizer_enabled=False):
    base = [{'name': name, 'description': 'Read-only chat query: ' + name,
             'inputSchema': {'type': 'object', 'additionalProperties': False,
                             'properties': {key: _argument_schema(kind) for key, kind in fields.items()},
                             'required': sorted(_REQUIRED[name])},
             'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, fields in _TOOL_ARGUMENTS.items()]
    if not chat_enabled:
        base = []
    if diagnostics_enabled:
        base.append({'name': 'recent_diagnostics', 'description': 'Read-only sanitized runtime diagnostics',
                     'inputSchema': {'type': 'object', 'additionalProperties': False,
                                     'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 32}}},
                     'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                     'idempotentHint': True, 'openWorldHint': False}})
    if organizer_enabled:
        from .mcp_organizer import tool_definitions
        base.extend(tool_definitions())
    if not control_enabled:
        return base
    return base + [{'name': name, 'description': 'Authority control: ' + name,
                    'inputSchema': {'type': 'object', 'additionalProperties': False,
                                    'properties': {key: {'type': 'string', 'pattern': '^' + _CONTROL_IDENTIFIER + '$', 'minLength': 1, 'maxLength': 96} for key in fields},
                                    'required': sorted(_CONTROL_REQUIRED[name])},
                    'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                                    'idempotentHint': False, 'openWorldHint': False}}
                   for name, fields in _CONTROL_ARGUMENTS.items()]


def validate_arguments(name, arguments):
    if name == 'recent_diagnostics':
        if type(arguments) is not dict or set(arguments) - {'limit'}:
            raise ValueError('invalid_params')
        limit = arguments.get('limit', 32)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('invalid_params')
        return {'limit': limit}
    if name in _CONTROL_TOOL_NAMES:
        if type(arguments) is not dict:
            raise ValueError('invalid_params')
        fields = _CONTROL_ARGUMENTS[name]
        if set(arguments) - set(fields) or not _CONTROL_REQUIRED[name] <= set(arguments):
            raise ValueError('invalid_params')
        for value in arguments.values():
            if type(value) is not str or not re.fullmatch(_CONTROL_IDENTIFIER, value):
                raise ValueError('invalid_params')
        return dict(arguments)
    if name not in _TOOL_ARGUMENTS or type(arguments) is not dict:
        raise ValueError('invalid_params')
    fields = _TOOL_ARGUMENTS[name]
    if set(arguments) - set(fields) or not _REQUIRED[name] <= set(arguments):
        raise ValueError('invalid_params')
    for key, value in arguments.items():
        kind = fields[key]
        if kind == 'channel':
            valid = type(value) is str and bool(re.fullmatch(r'[a-z0-9_]{3,25}', value))
        elif kind == 'query':
            valid = type(value) is str and 1 <= len(value) <= 512
        elif kind == 'utc':
            valid = _utc(value)
        elif kind == 'day':
            valid = type(value) is str and bool(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value))
            if valid:
                try:
                    datetime.strptime(value, '%Y-%m-%d')
                except ValueError:
                    valid = False
        else:
            valid = type(value) is int and 1 <= value <= {'limit': 200, 'window': 1440, 'bucket': 3600}[kind]
        if not valid:
            raise ValueError('invalid_params')
    return dict(arguments)


def _error(identity, code):
    return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': code, 'message': _CODES[code]}}


def _result(identity, result):
    return {'jsonrpc': '2.0', 'id': identity, 'result': result}


def _tool_error():
    return {'content': [{'type': 'text', 'text': 'data_unavailable_or_result_too_large'}], 'isError': True}


def _query_now_ns(clock):
    # Freeze one exact query instant before scanning any records.
    if clock is None:
        return time.time_ns()
    value = clock()
    if type(value) not in (int, float, Decimal):
        raise ValueError('invalid_query_clock')
    precise = Decimal(str(value))
    if not precise.is_finite():
        raise ValueError('invalid_query_clock')
    precise *= 1000000000
    if precise != precise.to_integral_value():
        raise ValueError('invalid_query_clock')
    now_ns = int(precise)
    if type(now_ns) is not int:
        raise ValueError('invalid_query_clock')
    return now_ns


def _read_schema(kind):
    if kind == 'cursor':
        return {'type': ['string', 'null'], 'maxLength': 512}
    if kind == 'control_id':
        return {'type': 'string', 'pattern': '^' + _CONTROL_IDENTIFIER + '$',
                'minLength': 1, 'maxLength': 96}
    if kind in {'page_limit', 'top_limit', 'step'}:
        result = {'type': 'integer', 'minimum': 1,
                  'maximum': {'page_limit': 200, 'top_limit': 50, 'step': 7}[kind]}
        if kind == 'page_limit':
            result['default'] = 50
        return result
    result = _argument_schema(kind)
    if kind == 'bucket':
        result['default'] = 60
    return result


def _read_definitions():
    # The frozen registry remains discoverable before serial dependencies land.
    return [{'name': name, 'description': 'Read-only typed query: ' + name,
             'inputSchema': {'type': 'object', 'additionalProperties': False,
                             'properties': {key: _read_schema(kind) for key, kind in fields.items()},
                             'required': ['channel', 'from', 'to'] if 'channel' in fields else []},
             'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, fields in _READ_ARGUMENTS.items()]


def _optional_module(name):
    try:
        return importlib.import_module('.' + name, __package__)
    except Exception:
        # Absence or an unusable optional integration cannot disable old queries.
        return None


def _cursor(value):
    try:
        return value is None or (type(value) is str and 0 < len(value.encode('utf-8')) <= 512)
    except UnicodeError:
        return False


def _read_instant(value):
    # Validation has already excluded offsets and invalid calendar dates.
    moment = datetime.fromisoformat(value[:19])
    seconds = ((moment.toordinal() * 24 + moment.hour) * 60 + moment.minute) * 60 + moment.second
    fraction = value[20:-1] if value[19:20] == '.' else ''
    return seconds * 1000000000 + int(fraction.ljust(9, '0') or '0')


def _validate_read_arguments(name, arguments):
    fields = _READ_ARGUMENTS[name]
    required = {'channel', 'from', 'to'} if 'channel' in fields else set()
    if type(arguments) is not dict or set(arguments) - set(fields) or not required <= set(arguments):
        raise ValueError('invalid_arguments')
    for key, value in arguments.items():
        kind = fields[key]
        if kind == 'cursor':
            valid = _cursor(value)
        elif kind == 'channel':
            valid = type(value) is str and bool(re.fullmatch(r'[a-z0-9_]{3,25}', value))
        elif kind == 'control_id':
            valid = type(value) is str and bool(re.fullmatch(_CONTROL_IDENTIFIER, value))
        elif kind == 'utc':
            valid = _utc(value)
        else:
            valid = type(value) is int and 1 <= value <= {'page_limit': 200, 'bucket': 3600,
                                                         'top_limit': 50, 'step': 7}[kind]
        if not valid:
            raise ValueError('invalid_arguments')
    if 'from' in arguments:
        duration = _read_instant(arguments['to']) - _read_instant(arguments['from'])
        if duration <= 0:
            raise ValueError('invalid_arguments')
        if name in _STATISTICS_READS:
            bucket = arguments.get('bucket_seconds', 60) * 1000000000
            if (duration + bucket - 1) // bucket > 1000:
                raise ValueError('invalid_arguments')
    return dict(arguments)


def _unavailable(error, scan=None):
    return {'schema': 'cafe.mcp_read.v1', 'availability': 'unavailable', 'data': None,
            'page': {'next_cursor': None, 'complete': False}, 'scan': scan, 'error': error}


def _keys(value, keys):
    return type(value) is dict and set(value) == set(keys.split())


def _identifier(value):
    return type(value) is str and bool(re.fullmatch(_CONTROL_IDENTIFIER, value))


def _runtime_projection(name, data):
    """Check the frozen projected result, never a native snapshot transport."""
    def sequence(rows, check):
        return type(rows) is list and all(check(row) for row in rows)

    def feed(row):
        return (_keys(row, 'feed_id role generation') and _identifier(row['feed_id']) and
                type(row['role']) is str and _canonical_clock(row['generation']))

    def capsule(row):
        return (_keys(row, 'capsule_id slot person_label generation feeds') and
                _identifier(row['capsule_id']) and type(row['slot']) is str and
                bool(re.fullmatch(r'slot-[0-9]+', row['slot'])) and type(row['person_label']) is str and
                _canonical_clock(row['generation']) and sequence(row['feeds'], feed))

    def window(row):
        return (_keys(row, 'window_token generation layout_revision title executable rect visible minimized') and
                _identifier(row['window_token']) and _canonical_clock(row['generation']) and
                _canonical_clock(row['layout_revision']) and type(row['title']) is str and
                type(row['executable']) is str and _keys(row['rect'], 'width height') and
                all(type(value) is int and value >= 0 for value in row['rect'].values()) and
                type(row['visible']) is bool and type(row['minimized']) is bool)

    def recording_feed(row):
        return (_keys(row, 'feed_id state written_frames backpressure') and _identifier(row['feed_id']) and
                type(row['state']) is str and
                all(value is None or _canonical_clock(value)
                    for value in (row['written_frames'], row['backpressure'])))

    def recording_capsule(row):
        return (_keys(row, 'capsule_id state feeds') and _identifier(row['capsule_id']) and
                type(row['state']) is str and sequence(row['feeds'], recording_feed))

    if name == 'list_capsules':
        return sequence(data, capsule)
    if name == 'list_candidate_windows':
        return sequence(data, window)
    if name == 'recording_status':
        return (_keys(data, 'capsules recorder_available') and
                (data['recorder_available'] is None or type(data['recorder_available']) is bool) and
                sequence(data['capsules'], recording_capsule))
    return (_keys(data, 'available configured state') and
            all(value is None or type(value) is bool for value in (data['available'], data['configured'])) and
            (data['state'] is None or type(data['state']) is str))


def _statistics_projection(data):
    def count(value):
        return type(value) is int and value >= 0

    def buckets(rows):
        return (type(rows) is list and len(rows) <= 1000 and
                all(_keys(row, 'start count') and _utc(row['start']) and count(row['count']) for row in rows))

    return (_keys(data, 'message_count unique_chatters messages_per_bucket top_words peaks top_emotes emotes_status complete') and
            count(data['message_count']) and count(data['unique_chatters']) and
            buckets(data['messages_per_bucket']) and buckets(data['peaks']) and
            type(data['top_words']) is list and len(data['top_words']) <= 50 and
            all(_keys(row, 'word count') and type(row['word']) is str and row['word'] and count(row['count'])
                for row in data['top_words']) and data['top_emotes'] is None and
            data['emotes_status'] == 'metadata_unavailable' and type(data['complete']) is bool)


def _chat_projection(value):
    # Grouping belongs to the read module; embedded event rows retain the
    # original closed event schema and canonical clock rules.
    if type(value) is dict:
        if value.get('schema') == 'cafe.chat_event.v1':
            return _valid_event(value)
        return all(_chat_projection(child) for child in value.values())
    if type(value) is list:
        return all(_chat_projection(child) for child in value)
    return True


def _checked_read_result(name, value, scan, arguments):
    # Detach results from dependency-owned mutable state and reject non-JSON data.
    value = _loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    if (not _keys(value, 'schema availability data page scan error') or
            value['schema'] != 'cafe.mcp_read.v1' or
            value['availability'] not in ('available', 'unavailable') or
            value['error'] is not None and value['error'] not in _READ_ERRORS or
            not _keys(value['page'], 'next_cursor complete') or
            not _cursor(value['page']['next_cursor']) or type(value['page']['complete']) is not bool or
            value['data'] is not None and type(value['data']) not in (dict, list) or
            value['scan'] is not None and type(value['scan']) is not dict):
        raise ValueError('invalid_snapshot')
    if value['page']['complete'] and value['page']['next_cursor'] is not None:
        raise ValueError('invalid_snapshot')
    if value['error'] not in (None, 'scan_limited'):
        if (value['availability'] != 'unavailable' or value['data'] is not None or
                value['page'] != {'next_cursor': None, 'complete': False}):
            raise ValueError('invalid_snapshot')
    elif value['error'] is None and (value['availability'] != 'available' or value['data'] is None):
        raise ValueError('invalid_snapshot')
    if value['data'] is not None and name in _RUNTIME_READS and not _runtime_projection(name, value['data']):
        raise ValueError('invalid_snapshot')
    if (type(value['data']) is list and 'limit' in _READ_ARGUMENTS[name] and
            len(value['data']) > arguments.get('limit', 50)):
        raise ValueError('invalid_snapshot')
    if value['data'] is not None and name in _CHAT_READS and not _chat_projection(value['data']):
        raise ValueError('invalid_snapshot')
    if value['data'] is not None and name in _STATISTICS_READS:
        if name == 'chat_statistics' or type(value['data']) is dict:
            if not _statistics_projection(value['data']):
                raise ValueError('invalid_snapshot')
            if (name == 'chat_activity' and
                    len(value['data']['messages_per_bucket']) > arguments.get('limit', 50) or
                    name == 'chat_statistics' and
                    len(value['data']['top_words']) > arguments.get('top_limit', 50)):
                raise ValueError('invalid_snapshot')
        elif (len(value['data']) > arguments.get('limit', 50) or
              any(not _keys(row, 'start count') or not _utc(row['start']) or
                  type(row['count']) is not int or row['count'] < 0 for row in value['data'])):
            raise ValueError('invalid_snapshot')
    value['scan'] = scan
    if (value['error'] == 'scan_limited' or
            scan is not None and (scan['scan_limited'] or scan['corrupt_lines'] or scan['truncated_lines'])):
        value['page'] = {'next_cursor': None, 'complete': False}
        if type(value['data']) is dict and 'complete' in value['data']:
            value['data']['complete'] = False
        if value['error'] is None or value['error'] == 'scan_limited':
            value['error'] = 'scan_limited'
    return value


def _guide_error(error):
    return {'schema': 'cafe.demo_guide.v1', 'steps': [],
            'control_mode': 'direct_with_optional_confirmation', 'error': error}


def _checked_guide(value, step=None):
    value = _loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    if (not _keys(value, 'schema steps control_mode error') or
            value['schema'] != 'cafe.demo_guide.v1' or value['control_mode'] != 'direct_with_optional_confirmation' or
            value['error'] not in (None, 'invalid_arguments', 'unknown_resource') or type(value['steps']) is not list):
        raise ValueError('invalid_snapshot')
    if value['error'] is not None:
        if value['steps']:
            raise ValueError('invalid_snapshot')
        return value
    expected = [step] if step is not None else list(range(1, 8))
    if [row.get('id') if type(row) is dict else None for row in value['steps']] != expected:
        raise ValueError('invalid_snapshot')
    for row in value['steps']:
        if (not _keys(row, 'id instruction expected evidence_required depends_on live_status') or
                type(row['id']) is not int or row['live_status'] != 'pending' or
                any(type(row[key]) is not str or not row[key] for key in ('instruction', 'expected', 'evidence_required')) or
                type(row['depends_on']) is not list or
                any(type(item) is not int or not 1 <= item < row['id'] for item in row['depends_on'])):
            raise ValueError('invalid_snapshot')
    return value


def _read_response(identity, value):
    payload = {'data': value, 'scan': value['scan']}
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return _result(identity, {'content': [{'type': 'text', 'text': text}],
                              'structuredContent': payload, 'isError': value['error'] is not None})


def _bounded_read_response(identity, value):
    response = _read_response(identity, value)
    if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
        return _read_response(identity, _unavailable('response_too_large', value['scan']))
    return response


def _dependency_definitions(module, names):
    if module is None:
        return None
    try:
        definitions = _loads(json.dumps(module.tool_definitions(), allow_nan=False))
        if type(definitions) is not list or len(definitions) != len(names):
            return None
        if {item['name'] for item in definitions} != names:
            return None
        for item in definitions:
            schema = item['inputSchema']
            fields = _READ_ARGUMENTS[item['name']]
            required = {'channel', 'from', 'to'} if 'channel' in fields else set()
            if (schema.get('type') != 'object' or schema.get('additionalProperties') is not False or
                    type(schema.get('properties')) is not dict or set(schema['properties']) != set(fields) or
                    type(schema.get('required')) is not list or set(schema['required']) != required or
                    item['annotations'].get('readOnlyHint') is not True or
                    item['annotations'].get('destructiveHint') is not False):
                return None
        return definitions
    except Exception:
        return None


class Server:
    def __init__(self, data=None, *, query=None, clock=None, control=None, diagnostics=None, readonly_provider=None, organizer=None):
        if data is None and diagnostics is None and organizer is None:
            raise ValueError('data_unavailable')
        self.data = _canonical_root(data) if data is not None else None
        self.diagnostics = diagnostics
        self.organizer = organizer
        self.state = 'new'; self.clock = clock; self.control = control
        if query is None:
            from .mcp_queries import execute
            query = execute
        self.query = query
        self.readonly_provider = readonly_provider
        self._read_model = None
        self._statistics = None
        self._guide = None
        definitions = {item['name']: item for item in _read_definitions()}
        # Integration seams are optional and independent; no runtime discovery,
        # native snapshot transport, proposal execution, or credential lookup.
        for module_name, names in [('mcp_runtime_queries', _RUNTIME_READS | _CHAT_READS),
                                   ('mcp_chat_digest', _STATISTICS_READS),
                                   ('mcp_operator_runbook', {'demo_guide'})]:
            module = _optional_module(module_name)
            supplied = _dependency_definitions(module, names)
            if supplied is None:
                continue
            try:
                if module_name == 'mcp_runtime_queries':
                    model = module.ReadModel(snapshot_provider=readonly_provider)
                    if not callable(model.execute):
                        continue
                    self._read_model = model
                elif module_name == 'mcp_chat_digest':
                    if not callable(module.execute):
                        continue
                    self._statistics = module
                else:
                    if not callable(module.demo_guide) or not callable(module.read_resource):
                        continue
                    resources = module.resource_definitions()
                    if (type(resources) is not list or len(resources) != 1 or
                            type(resources[0]) is not dict or resources[0].get('uri') != _GUIDE_URI):
                        continue
                    self._guide = module
            except Exception:
                continue
            # Keep the frozen descriptors, including bounded fixed text: a
            # dependency cannot widen discovery or leak an arbitrary field.
        self._read_tools = list(definitions.values())

    def _guide_call(self, identity, arguments):
        if self._guide is None:
            return _bounded_read_response(identity, _unavailable('provider_unavailable'))
        try:
            value = _checked_guide(self._guide.demo_guide(arguments), arguments.get('step'))
            if value['error'] == 'unknown_resource':
                raise ValueError('invalid_snapshot')
        except Exception:
            return _bounded_read_response(identity, _unavailable('invalid_snapshot'))
        return self._guide_response(identity, value)

    def _guide_response(self, identity, value):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        response = _result(identity, {'content': [{'type': 'text', 'text': text}],
                                      'structuredContent': value, 'isError': value['error'] is not None})
        if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
            return _bounded_read_response(identity, _unavailable('response_too_large'))
        return response

    def _resources(self, identity, method, params):
        if method == 'resources/list':
            if params != {}:
                return _error(identity, -32602)
            return _result(identity, {'resources': [{'uri': _GUIDE_URI, 'name': 'demo_guide',
                                                    'mimeType': 'application/json'}]})
        if not _keys(params, 'uri') or type(params['uri']) is not str:
            return _error(identity, -32602)
        if params['uri'] != _GUIDE_URI:
            value = _guide_error('unknown_resource')
        elif self._guide is None:
            value = _unavailable('provider_unavailable')
        else:
            try:
                value = _checked_guide(self._guide.read_resource(_GUIDE_URI))
                if value['error'] is not None:
                    raise ValueError('invalid_snapshot')
            except Exception:
                value = _unavailable('invalid_snapshot')
        # Only the contracted resource is exposed. Never reflect unknown URIs.
        response = _result(identity, {'contents': [{'uri': _GUIDE_URI, 'mimeType': 'application/json',
                                                   'text': json.dumps(value, ensure_ascii=False,
                                                                      allow_nan=False, separators=(',', ':'))}]})
        if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
            response['result']['contents'][0]['text'] = json.dumps(_unavailable('response_too_large'), separators=(',', ':'))
        return response

    def _readonly_call(self, identity, name, arguments):
        try:
            arguments = _validate_read_arguments(name, arguments)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            if name == 'demo_guide':
                return self._guide_response(identity, _guide_error('invalid_arguments'))
            return _bounded_read_response(identity, _unavailable('invalid_arguments'))
        if name == 'demo_guide':
            return self._guide_call(identity, arguments)
        if ((name in _RUNTIME_READS and not callable(self.readonly_provider)) or
                (name in _RUNTIME_READS | _CHAT_READS and self._read_model is None) or
                (name in _STATISTICS_READS and self._statistics is None)):
            return _bounded_read_response(identity, _unavailable('provider_unavailable'))
        scan = None
        try:
            if name in _RUNTIME_READS:
                value = self._read_model.execute(name, arguments, events=None, scan=None)
            else:
                reader = DataReader(self.data)
                records = reader.events(arguments['channel'])
                try:
                    # One bounded validated scan, finalized before aggregation.
                    # Optional modules receive values only, never file paths.
                    events = list(records)
                finally:
                    records.close()
                    scan = dict(reader.stats)
                if name in _CHAT_READS:
                    value = self._read_model.execute(name, arguments, events=events, scan=dict(scan))
                else:
                    value = self._statistics.execute(name, arguments, events, scan=dict(scan))
        except Exception:
            error = 'provider_unavailable' if name in _RUNTIME_READS else 'query_unavailable'
            return _bounded_read_response(identity, _unavailable(error, scan))
        try:
            value = _checked_read_result(name, value, scan, arguments)
            return _bounded_read_response(identity, value)
        except Exception:
            return _bounded_read_response(identity, _unavailable('invalid_snapshot', scan))

    def handle(self, request):
        if type(request) is not dict or request.get('jsonrpc') != '2.0' or type(request.get('method')) is not str or set(request) - {'jsonrpc', 'id', 'method', 'params'}:
            return _error(None, -32600)
        identity = request.get('id')
        notification = 'id' not in request
        if not notification and not (type(identity) is int or type(identity) is str and len(identity) <= 96):
            return _error(None, -32600)
        method = request['method']; params = request.get('params', {})
        # Real MCP clients attach _meta to params; it carries no arguments for this server.
        if type(params) is dict and '_meta' in params:
            params = {key: value for key, value in params.items() if key != '_meta'}
        if notification:
            if method == 'notifications/initialized' and self.state == 'initializing' and params == {}:
                self.state = 'ready'
            return None
        if method == 'ping':
            return _result(identity, {}) if params == {} else _error(identity, -32602)
        if method == 'initialize':
            if self.state != 'new' or type(params) is not dict or not {'protocolVersion', 'capabilities', 'clientInfo'} <= set(params) or type(params['protocolVersion']) is not str or type(params['capabilities']) is not dict or type(params['clientInfo']) is not dict:
                return _error(identity, -32602)
            self.state = 'initializing'
            return _result(identity, {'protocolVersion': PROTOCOL_VERSION,
                                     'capabilities': {'tools': {'listChanged': False},
                                                      'resources': {'subscribe': False, 'listChanged': False}},
                                     'serverInfo': {'name': 'cafe', 'version': '0.1.0'}})
        if self.state != 'ready':
            return _error(identity, -32600)
        if method == 'tools/list':
            return _result(identity, {'tools': tools(control_enabled=self.control is not None, diagnostics_enabled=self.diagnostics is not None, chat_enabled=self.data is not None, organizer_enabled=self.organizer is not None) + (self._read_tools if self.data is not None else [])}) if params == {} else _error(identity, -32602)
        if method in {'resources/list', 'resources/read'}:
            return self._resources(identity, method, params)
        if method != 'tools/call':
            return _error(identity, -32601)
        if type(params) is not dict or set(params) - {'name', 'arguments'} or type(params.get('name')) is not str:
            return _error(identity, -32602)
        if params['name'] in _READ_ARGUMENTS:
            return self._readonly_call(identity, params['name'], params.get('arguments', {}))
        from .mcp_organizer import ORGANIZER_TOOLS, unavailable_result
        if params['name'] in ORGANIZER_TOOLS:
            if self.organizer is None:
                return _error(identity, -32602)
            try:
                result = self.organizer.execute(params['name'], params.get('arguments', {}))
                response = _result(identity, result)
                if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 1 > MAX_RESPONSE:
                    return _result(identity, unavailable_result())
                return response
            except Exception:
                return _result(identity, unavailable_result())
        try:
            args = validate_arguments(params['name'], params.get('arguments', {}))
        except ValueError:
            return _error(identity, -32602)
        if params['name'] == 'recent_diagnostics':
            if self.diagnostics is None:
                return _error(identity, -32602)
            try:
                from .runtime_logging import recent
                data = recent(self.diagnostics, args['limit'])
                payload = {'data': data}
                result = {'content': [{'type': 'text', 'text': json.dumps(payload, separators=(',', ':'), allow_nan=False)}],
                          'structuredContent': payload, 'isError': False}
                response = _result(identity, result)
                if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
                    return _result(identity, _tool_error())
                return response
            except (OSError, ValueError, TypeError, RecursionError):
                return _result(identity, _tool_error())
        if params['name'] in _TOOL_ARGUMENTS and self.data is None:
            return _error(identity, -32602)
        if params['name'] in _CONTROL_TOOL_NAMES:
            if self.control is None:
                return _error(identity, -32602)
            try:
                # Approval lifetime belongs to the control plane monotonic clock.
                result = self.control.execute(params['name'], args)
                if type(result) is not dict or set(result) != {'content', 'structuredContent', 'isError'} or type(result['isError']) is not bool:
                    raise ValueError('invalid_control_result')
                response = _result(identity, result)
                if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
                    return _result(identity, _tool_error())
                return response
            except (OSError, ValueError, TypeError, RecursionError):
                return _result(identity, _tool_error())
        try:
            reader = DataReader(self.data)
            now_ns = _query_now_ns(self.clock)
            records = reader.events(args.get('channel'))
            try:
                data = self.query(params['name'], args, records, now_ns // 1000000, now_ns=now_ns)
            finally:
                records.close()
            payload = {'data': data, 'scan': reader.stats}
            text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            result = {'content': [{'type': 'text', 'text': text}], 'structuredContent': payload, 'isError': False}
            response = _result(identity, result)
            if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
                return _result(identity, _tool_error())
            return response
        except (OSError, ValueError, TypeError, RecursionError):
            return _result(identity, _tool_error())


def serve(server, input_stream, output_stream):
    while True:
        raw = input_stream.readline(MAX_EVENT_LINE + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
            while len(raw) > MAX_REQUEST and not raw.endswith(b'\n'):
                raw = input_stream.readline(MAX_EVENT_LINE + 1)
            response = _error(None, -32700)
        else:
            try:
                request = _loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                response = _error(None, -32700)
            else:
                response = server.handle(request)
        if response is not None:
            data = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
            if len(data) > MAX_RESPONSE:
                data = json.dumps(_error(None, -32600), separators=(',', ':')).encode() + b'\n'
            output_stream.write(data); output_stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data')
    parser.add_argument('--diagnostics')
    parser.add_argument('--organizer-rendezvous')
    parser.add_argument('--organizer-root')
    args = parser.parse_args(argv)
    try:
        organizer = None
        if args.organizer_rendezvous is not None:
            from .mcp_organizer import BrokerOrganizerProvider, OrganizerPlane
            organizer = OrganizerPlane(BrokerOrganizerProvider(args.organizer_rendezvous))
        elif args.organizer_root is not None:
            from .mcp_organizer import BrokerOrganizerProvider, OrganizerPlane, current_broker
            root = Path(args.organizer_root)
            organizer = OrganizerPlane(BrokerOrganizerProvider(lambda: current_broker(root)))
        return serve(Server(args.data, diagnostics=args.diagnostics, organizer=organizer), sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError):
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
