"""Closed Twitch endpoint, read-only sends and durable confined JSONL storage."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import secrets
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

from websockets import __version__ as _transport_version
from websockets.asyncio.client import connect as _connect

OFFICIAL_URL = 'wss://irc-ws.chat.twitch.tv:443'
MAX_LINE = 131072
_MAX_CLOCK = (1 << 64) - 1
_FIELDS = frozenset({'schema', 'channel', 'received_at', 'sent_at', 'message_id',
                     'user_login', 'display_name', 'badges', 'text', 'kind', 'session_id'})
_KINDS = frozenset({'message', 'join_state', 'notice', 'clear', 'reconnect'})
_CODES = frozenset({'invalid_channel', 'invalid_endpoint', 'network_failed',
                    'readonly_command', 'invalid_event', 'unsafe_path',
                    'storage_failed', 'transport_unavailable'})


class ChatError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if code in _CODES else 'storage_failed')


def validate_channel(channel):
    if type(channel) is not str:
        raise ChatError('invalid_channel')
    value = channel.lower()
    if not re.fullmatch(r'[a-z0-9_]{3,25}', value, flags=re.ASCII):
        raise ChatError('invalid_channel')
    return value


def validate_options(max_connections, backoff_initial, backoff_max, sleep, session_id):
    if max_connections is not None and (type(max_connections) is not int or max_connections < 1):
        raise ChatError('invalid_event')
    if (type(backoff_initial) not in (int, float) or not math.isfinite(backoff_initial)
            or not 0 < backoff_initial <= 60 or type(backoff_max) not in (int, float)
            or not math.isfinite(backoff_max) or not 0 < backoff_max <= 60 or not callable(sleep)):
        raise ChatError('invalid_event')
    if session_id is None:
        session_id = 'chat-' + secrets.token_hex(12)
    if not _identity(session_id):
        raise ChatError('invalid_event')
    return max_connections, min(float(backoff_initial), float(backoff_max)), float(backoff_max), session_id


def validate_url(url):
    if type(url) is not str:
        raise ChatError('invalid_endpoint')
    if url == OFFICIAL_URL:
        return url
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'ws' and parsed.hostname == '127.0.0.1' and
                 parsed.username is None and parsed.password is None and
                 parsed.path in ('', '/') and not parsed.query and not parsed.fragment and
                 type(parsed.port) is int and 0 < parsed.port <= 65535 and
                 url in ('ws://127.0.0.1:' + str(parsed.port),
                         'ws://127.0.0.1:' + str(parsed.port) + '/'))
    except ValueError:
        valid = False
    if not valid:
        raise ChatError('invalid_endpoint')
    return url


class _ExactConnect(_connect):
    def process_redirect(self, exc):
        return ChatError('network_failed')


async def open_connection(url):
    url = validate_url(url)
    if _transport_version != '17.2':
        raise ChatError('transport_unavailable')
    logger = logging.Logger('cafe.chat.private', level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler()); logger.propagate = False
    failed = False
    try:
        # Omit ssl entirely: wss obtains the verified default TLS context.
        return await _ExactConnect(url, proxy=None, compression=None, logger=logger,
                                   open_timeout=5, close_timeout=1, ping_interval=None,
                                   max_size=MAX_LINE, max_queue=4, write_limit=16384)
    except asyncio.CancelledError:
        raise
    except Exception:
        failed = True
    if failed:
        raise ChatError('network_failed')


async def readonly_send(ws, command):
    if type(command) is not str or '\r' in command or '\n' in command:
        raise ChatError('readonly_command')
    allowed = (bool(re.fullmatch(r'NICK justinfan[0-9]{1,20}', command)) or
               command == 'CAP REQ :twitch.tv/tags twitch.tv/commands' or
               bool(re.fullmatch(r'JOIN #[a-z0-9_]{3,25}', command)) or
               bool(re.fullmatch(r'PONG :[^\x00-\x1f\x7f]{1,512}', command)))
    if not allowed:
        raise ChatError('readonly_command')
    await ws.send(command + '\r\n')


def _clock(value):
    return (type(value) is str and bool(re.fullmatch(r'0|[1-9][0-9]{0,19}', value))
            and int(value) <= _MAX_CLOCK)


def _identity(value):
    return (type(value) is str and 1 <= len(value) <= 96 and
            bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', value)))


def _utc(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z', value):
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset().total_seconds() == 0
    except (ValueError, OverflowError):
        return False


def validate_event(event):
    valid = type(event) is dict and set(event) == _FIELDS
    if valid:
        received = event['received_at']
        valid = (event['schema'] == 'cafe.chat_event.v1' and
                 type(event['channel']) is str and event['channel'] == validate_channel(event['channel']) and
                 type(received) is dict and set(received) == {'monotonic_ns', 'utc'} and
                 _clock(received['monotonic_ns']) and _utc(received['utc']) and
                 (event['sent_at'] is None or _clock(event['sent_at'])) and
                 (event['message_id'] is None or type(event['message_id']) is str and 1 <= len(event['message_id']) <= 128) and
                 (event['user_login'] is None or type(event['user_login']) is str and bool(re.fullmatch(r'[a-z0-9_]{1,25}', event['user_login']))) and
                 (event['display_name'] is None or type(event['display_name']) is str and len(event['display_name']) <= 128) and
                 type(event['badges']) is list and len(event['badges']) <= 64 and
                 type(event['text']) is str and len(event['text']) <= 16384 and
                 type(event['kind']) is str and event['kind'] in _KINDS and _identity(event['session_id']))
    if valid:
        for badge in event['badges']:
            valid = (type(badge) is dict and set(badge) == {'name', 'version'} and
                     all(type(v) is str and 1 <= len(v) <= 64 for v in badge.values()))
            if not valid:
                break
    if not valid:
        raise ChatError('invalid_event')
    return event


def _safe_stat(info, directory=False):
    okay = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not okay or not isinstance(info.st_ino, int) or info.st_ino <= 0:
        raise ChatError('unsafe_path')
    if os.name == 'nt':
        attributes = getattr(info, 'st_file_attributes', None)
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', None)
        if attributes is None or reparse is None or attributes & reparse:
            raise ChatError('unsafe_path')


def _directory(path, create=False):
    if create:
        path.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise ChatError('unsafe_path')
    _safe_stat(path.lstat(), directory=True)


def _open_file(path, flags):
    if path.is_symlink():
        raise ChatError('unsafe_path')
    try:
        before = path.lstat()
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
        before = None
    if before is not None:
        _safe_stat(before)
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        info = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        _safe_stat(info); _safe_stat(current)
        identity = (info.st_dev, info.st_ino)
        if identity != (current.st_dev, current.st_ino) or before is not None and identity != (before.st_dev, before.st_ino):
            raise ChatError('unsafe_path')
        return fd
    except BaseException:
        os.close(fd)
        raise


class JsonlWriter:
    def __init__(self, root, channel, *, sync_events=20, sync_seconds=2.0, clock=time.monotonic):
        if type(sync_events) is not int or not 1 <= sync_events <= 1000 or not isinstance(sync_seconds, (int, float)) or not 0 < sync_seconds <= 10:
            raise ChatError('invalid_event')
        self.root = Path(root).absolute()
        _directory(self.root)
        self.channel = validate_channel(channel)
        self.folder = self.root / 'twitch' / self.channel
        _directory(self.root / 'twitch', True); _directory(self.folder, True)
        self._fd = None; self._day = None; self._pending = 0
        self._sync_events = sync_events; self._sync_seconds = sync_seconds
        self._clock = clock; self._last_sync = clock(); self.closed = False; self.poisoned = False
        self.events = 0; self.syncs = 0

    def _poison(self):
        # A failed append may already have written a prefix. Never reuse its
        # descriptor or acknowledge a later record after that torn fragment.
        self.poisoned = True
        self.closed = True
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                # The enclosing operation always returns storage_failed.
                # No durability success is reported for this poisoned writer.
                pass

    def _check(self):
        _directory(self.root); _directory(self.root / 'twitch'); _directory(self.folder)

    def _sync(self):
        if self._fd is not None and self._pending:
            os.fsync(self._fd)
            self.syncs += 1; self._pending = 0; self._last_sync = self._clock()

    def append(self, event):
        validate_event(event)
        if self.poisoned:
            raise ChatError('storage_failed')
        if self.closed or event['channel'] != self.channel:
            raise ChatError('invalid_event')
        data = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
        if len(data) > MAX_LINE:
            raise ChatError('invalid_event')
        day = event['received_at']['utc'][:10]
        try:
            self._check()
            if day != self._day:
                self._close_file()
                path = self.folder / (day + '.jsonl')
                self._fd = _open_file(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
                self._check()
                if path.stat().st_size:
                    check = _open_file(path, os.O_RDONLY)
                    with os.fdopen(check, 'rb') as stream:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b'\n':
                            self._close_file()
                            raise ChatError('storage_failed')
                self._day = day
            offset = 0
            while offset < len(data):
                wrote = os.write(self._fd, data[offset:])
                if wrote <= 0:
                    raise OSError('short_write')
                offset += wrote
            self.events += 1; self._pending += 1
            if self._pending >= self._sync_events:
                self._sync()
            self.flush_due()
        except (OSError, UnicodeError, ValueError):
            self._poison()
            raise ChatError('storage_failed') from None

    def flush_due(self):
        try:
            if self._pending and self._clock() - self._last_sync >= self._sync_seconds:
                self._sync()
        except OSError:
            self._poison()
            raise ChatError('storage_failed') from None

    def _close_file(self):
        if self._fd is not None:
            fd = self._fd
            try:
                self._sync()
            finally:
                self._fd = None
                os.close(fd)

    def close(self):
        try:
            if self.poisoned:
                raise ChatError('storage_failed')
            self._close_file()
        except OSError:
            self._poison()
            raise ChatError('storage_failed') from None
        finally:
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _unique_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = child
    return value


def read_jsonl_file(path, stats=None):
    """Read only complete valid records; never repair a torn final line."""
    stats = stats if stats is not None else {'invalid': 0, 'truncated': 0}
    fd = _open_file(Path(path), os.O_RDONLY)
    with os.fdopen(fd, 'rb') as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if not line.endswith(b'\n'):
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b'\n'):
                        line = stream.readline(MAX_LINE + 1)
                    stats['invalid'] = stats.get('invalid', 0) + 1
                    continue
                stats['truncated'] = stats.get('truncated', 0) + 1
                break
            try:
                value = json.loads(line, object_pairs_hook=_unique_pairs)
                validate_event(value)
            except (ValueError, UnicodeError, TypeError, ChatError, RecursionError):
                stats['invalid'] = stats.get('invalid', 0) + 1
                continue
            yield value


def main(argv=None):
    """Production source-module caller; only the approved official URL is used."""
    import argparse
    parser = argparse.ArgumentParser(prog='cafe-chat', description=__doc__)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--data', required=True)
    args = parser.parse_args(argv)
    try:
        channel = validate_channel(args.channel)
        root = Path(args.data).absolute()
        _directory(root)
        from . import twitch_chat
        try:
            asyncio.run(twitch_chat.run_chat(channel, root, url=OFFICIAL_URL))
        except twitch_chat.ChatError:
            # A source-module launch uses __main__ for this module; the loop's
            # relative import can have a distinct canonical exception class.
            return 2
    except KeyboardInterrupt:
        return 130
    except (ChatError, OSError, ValueError):
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
