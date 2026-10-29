"""Bounded stdlib-only MCP stdio framing over confined read-only chat files."""
import argparse
from datetime import datetime
from decimal import Decimal
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
_CODES = {-32700: 'Parse error', -32600: 'Invalid Request',
          -32601: 'Method not found', -32602: 'Invalid params'}


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


class DataReader:
    def __init__(self, root):
        self.root = Path(root).absolute()
        _directory(self.root)
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


def tools():
    return [{'name': name, 'description': 'Read-only chat query: ' + name,
             'inputSchema': {'type': 'object', 'additionalProperties': False,
                             'properties': {key: _argument_schema(kind) for key, kind in fields.items()},
                             'required': sorted(_REQUIRED[name])},
             'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, fields in _TOOL_ARGUMENTS.items()]


def validate_arguments(name, arguments):
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


class Server:
    def __init__(self, data, *, query=None, clock=None):
        self.data = Path(data).absolute(); _directory(self.data)
        self.state = 'new'; self.clock = clock
        if query is None:
            from .mcp_queries import execute
            query = execute
        self.query = query

    def handle(self, request):
        if type(request) is not dict or request.get('jsonrpc') != '2.0' or type(request.get('method')) is not str or set(request) - {'jsonrpc', 'id', 'method', 'params'}:
            return _error(None, -32600)
        identity = request.get('id')
        notification = 'id' not in request
        if not notification and not (type(identity) is int or type(identity) is str and len(identity) <= 96):
            return _error(None, -32600)
        method = request['method']; params = request.get('params', {})
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
                                     'capabilities': {'tools': {'listChanged': False}},
                                     'serverInfo': {'name': 'cafe', 'version': '0.1.0'}})
        if self.state != 'ready':
            return _error(identity, -32600)
        if method == 'tools/list':
            return _result(identity, {'tools': tools()}) if params == {} else _error(identity, -32602)
        if method != 'tools/call':
            return _error(identity, -32601)
        if type(params) is not dict or set(params) - {'name', 'arguments'} or type(params.get('name')) is not str:
            return _error(identity, -32602)
        try:
            args = validate_arguments(params['name'], params.get('arguments', {}))
        except ValueError:
            return _error(identity, -32602)
        try:
            reader = DataReader(self.data)
            # Freeze one exact query instant before scanning any records.
            if self.clock is None:
                now_ns = time.time_ns()
            else:
                value = self.clock()
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
    parser.add_argument('--data', required=True)
    args = parser.parse_args(argv)
    try:
        return serve(Server(args.data), sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError):
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
