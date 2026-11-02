"""Read only the host-bound, closed and sanitized runtime trace stream."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SCHEMA_SHA256 = '7aba1cda71ed585c4313e0577717a14bd42b09bc1e122315d0f4f846e2e301b9'
MAX_FILE_BYTES = 2097152
MAX_ROWS = 4096
MAX_LINE_BYTES = 4096
MAX_ENTRIES = 64
_NAME = re.compile(r'trace-[0-9a-f]{32}\.jsonl')


class TraceError(ValueError):
    """Deliberately contains no raw filesystem or record detail."""


def _fail():
    raise TraceError('diagnostics_unavailable')


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            _fail()
        value[key] = child
    return value


def _constant(_value):
    _fail()


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)


def _identity(info):
    return info.st_dev, info.st_ino


def _safe(info, directory=False):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _fail()
    if not isinstance(info.st_ino, int) or info.st_ino <= 0:
        _fail()
    if not directory and info.st_nlink != 1:
        _fail()
    if os.name == 'nt':
        flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', None)
        attributes = getattr(info, 'st_file_attributes', None)
        if flag is None or attributes is None or attributes & flag:
            _fail()


def _root_state(root):
    if '..' in root.parts or not root.is_absolute():
        _fail()
    state = []
    for part in (*reversed(root.parents), root):
        info = part.lstat()
        _safe(info, True)
        state.append(_identity(info))
    if root.resolve(strict=True) != root:
        _fail()
    return tuple(state)


def _open_root(root, state):
    """Pin all POSIX ancestors while descending; Windows uses identity checks."""
    if os.name == 'nt':
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root.anchor, flags)
    try:
        if _identity(os.fstat(descriptor)) != state[0]:
            _fail()
        for index, part in enumerate(root.parts[1:], 1):
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if _identity(os.fstat(descriptor)) != state[index]:
                _fail()
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_file(root, name, before, root_fd, state, maximum):
    _safe(before)
    if before.st_size > maximum:
        _fail()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(root / name, flags) if root_fd is None else os.open(name, flags, dir_fd=root_fd)
    try:
        opened = os.fstat(fd)
        _safe(opened)
        if _identity(opened) != _identity(before) or opened.st_size > maximum:
            _fail()
        if _root_state(root) != state:
            _fail()
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        _safe(after)
        current = (root / name).lstat()
        _safe(current)
        if len(raw) > maximum or after.st_size > maximum or _identity(current) != _identity(opened) or _identity(after) != _identity(opened) or _root_state(root) != state:
            _fail()
        return raw
    finally:
        if fd is not None:
            os.close(fd)


def _schema():
    path = Path(__file__).parents[2] / 'src/contracts/runtime_logging.json'
    state = _root_state(path.parent)
    root_fd = _open_root(path.parent, state)
    try:
        raw = _read_file(path.parent, path.name, path.lstat(), root_fd, state, 65536)
    finally:
        if root_fd is not None:
            os.close(root_fd)
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
        _fail()
    return _json(raw)


def _valid(row, schema, sequence):
    if type(row) is not dict or set(row) != set(schema['fields']):
        return False
    if row['schema'] != schema['schema'] or type(row['seq']) is not int or row['seq'] != sequence:
        return False
    if type(row['elapsed_ms']) is not int or not 0 <= row['elapsed_ms'] <= 86400000:
        return False
    enums = {'event': 'events', 'op': 'operations', 'bridge': 'bridges', 'reason': 'reasons',
             'result_state': 'states', 'result_code': 'codes', 'stage': 'stages', 'recording_state': 'recording_states'}
    if any(type(row[key]) is not str or row[key] not in schema[group] for key, group in enums.items()):
        return False
    bounds = {'capsule_count': 2, 'paired_feed_count': 4, 'listed_window_count': 3,
              'minimized_window_count': 3, 'visible_window_count': 3, 'recorded_frames': 4294967295}
    if any(row[key] is not None and (type(row[key]) is not int or not 0 <= row[key] <= maximum)
           for key, maximum in bounds.items()):
        return False
    if row['base_commit'] is not None and (type(row['base_commit']) is not str or not re.fullmatch(r'[0-9a-f]{40}', row['base_commit'])):
        return False
    return type(row['authority_sha256']) is str and bool(re.fullmatch(r'[0-9a-f]{64}', row['authority_sha256']))


def recent(root, limit=32):
    """Never accept a path from MCP arguments; root belongs to server startup."""
    if type(limit) is not int or not 1 <= limit <= 32:
        _fail()
    try:
        schema = _schema()
        root = Path(root)
        state = _root_state(root)
        root_fd = _open_root(root, state)
        try:
            candidates = []
            with os.scandir(root if root_fd is None else root_fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_ENTRIES or not _NAME.fullmatch(entry.name):
                        _fail()
                    info = entry.stat(follow_symlinks=False)
                    _safe(info)
                    candidates.append((info.st_mtime_ns, entry.name, info))
            if not candidates or _root_state(root) != state:
                _fail()
            _mtime, name, before = max(candidates, key=lambda item: (item[0], item[1]))
            raw = _read_file(root, name, before, root_fd, state, MAX_FILE_BYTES)
        finally:
            if root_fd is not None:
                os.close(root_fd)
        # Only an unterminated append tail is excluded, never a malformed complete row.
        incomplete = bool(raw) and not raw.endswith(b'\n')
        lines = raw.split(b'\n')
        tail = lines.pop()
        if incomplete and len(tail) > MAX_LINE_BYTES:
            _fail()
        if len(lines) > MAX_ROWS:
            _fail()
        rows = []
        for sequence, line in enumerate(lines, 1):
            if len(line) + 1 > MAX_LINE_BYTES:
                _fail()
            row = _json(line)
            if not _valid(row, schema, sequence):
                _fail()
            rows.append(row)
        if not rows:
            _fail()
        return {'rows': rows[-limit:], 'incomplete_tail': incomplete, 'truncated': len(rows) > limit}
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        _fail()
