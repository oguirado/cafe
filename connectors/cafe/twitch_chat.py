"""Anonymous read-only Twitch IRC client with durable JSONL persistence.

Connects only to the official verified-TLS Twitch IRC endpoint or an explicit
numeric loopback fixture endpoint, logs in anonymously, answers PING frames,
and persists parsed chat events through the durable writer. Every
outbound frame passes through ``twitch_storage.readonly_send``; the transport
sender is never used directly. Nothing is ever written to stdout, stderr, or
logs from this module, and raised errors carry only domain codes.
"""

import asyncio
from datetime import datetime, timezone
import random
import re
import time

try:
    from .twitch_storage import (
        ChatError,
        JsonlWriter,
        open_connection,
        readonly_send,
        validate_channel,
        validate_options,
        validate_url,
    )
except ImportError:  # loaded without package context; same helper module
    from cafe.twitch_storage import (
        ChatError,
        JsonlWriter,
        open_connection,
        readonly_send,
        validate_channel,
        validate_options,
        validate_url,
    )

__all__ = ['OFFICIAL_URL', 'parse_irc', 'run_chat']

OFFICIAL_URL = 'wss://irc-ws.chat.twitch.tv:443'

_RECV_TIMEOUT = 0.5
_BUF_LIMIT = 1048576
_SLEEP_POLL = 0.05
_MAX_U64 = (1 << 64) - 1
_CAP_REQ = 'CAP REQ :twitch.tv/tags twitch.tv/commands'

_CONTROL_RE = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')
_U64_RE = re.compile(r'0|[1-9][0-9]{0,19}')
_LOGIN_RE = re.compile(r'[a-z0-9_]{1,25}')

_TAG_UNESCAPES = {':': ';', 's': ' ', '\\': '\\', 'r': '\r', 'n': '\n'}
_CHANNEL_COMMANDS = frozenset({
    'PRIVMSG', 'NOTICE', 'JOIN', 'ROOMSTATE', 'CLEARCHAT', 'CLEARMSG',
})


class _Malformed(Exception):
    """Internal marker for input counted as malformed, never propagated."""


def _unescape_tag(value):
    out = []
    i = 0
    end = len(value)
    while i < end:
        char = value[i]
        if char == '\\':
            if i + 1 >= end:
                break
            nxt = value[i + 1]
            out.append(_TAG_UNESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(char)
            i += 1
    return ''.join(out)


def parse_irc(line):
    """Parse one IRC line into tags/prefix/command/params, else None."""
    try:
        return _parse_irc(line)
    except Exception:
        return None


def _parse_irc(line):
    if type(line) is not str:
        return None
    if line.endswith('\n'):
        line = line[:-1]
        if line.endswith('\r'):
            line = line[:-1]
    elif line.endswith('\r'):
        line = line[:-1]
    line = line.lstrip(' ')
    if not line or _CONTROL_RE.search(line):
        return None
    tags = {}
    if line.startswith('@'):
        space = line.find(' ')
        if space == -1:
            return None
        block = line[1:space]
        if not block:
            return None
        for part in block.split(';'):
            if '=' in part:
                key, _, value = part.partition('=')
            else:
                key, value = part, ''
            if not key or key in tags:
                return None
            tags[key] = _unescape_tag(value)
        line = line[space + 1:].lstrip(' ')
        if not line:
            return None
    prefix = None
    if line.startswith(':'):
        space = line.find(' ')
        if space == -1:
            return None
        prefix = line[1:space]
        if not prefix:
            return None
        line = line[space + 1:].lstrip(' ')
        if not line:
            return None
    space = line.find(' ')
    if space == -1:
        command, rest = line, ''
    else:
        command, rest = line[:space], line[space + 1:]
    if not command:
        return None
    params = []
    rest = rest.lstrip(' ')
    while rest:
        if rest.startswith(':'):
            params.append(rest[1:])
            break
        space = rest.find(' ')
        if space == -1:
            params.append(rest)
            break
        params.append(rest[:space])
        rest = rest[space + 1:].lstrip(' ')
    return {'tags': tags, 'prefix': prefix, 'command': command, 'params': params}


def _utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _build_event(parsed, channel, session_id):
    """Build a frozen-schema event dict, None to ignore, or raise _Malformed."""
    command = parsed['command'].upper()
    tags = parsed['tags']
    params = parsed['params']
    if command in _CHANNEL_COMMANDS:
        if not params:
            raise _Malformed
        if params[0].lower() != '#' + channel:
            return None
    if command == 'PRIVMSG':
        if len(params) < 2:
            raise _Malformed
        text = params[1]
        if len(text) > 16384:
            raise _Malformed
        kind = 'message'
    elif command == 'NOTICE':
        kind = 'notice'
        text = ''
    elif command == 'JOIN' or command == 'ROOMSTATE':
        kind = 'join_state'
        text = ''
    elif command == 'CLEARCHAT' or command == 'CLEARMSG':
        kind = 'clear'
        text = ''
    elif command == 'RECONNECT':
        kind = 'reconnect'
        text = ''
    else:
        return None
    raw_ts = tags.get('tmi-sent-ts')
    if raw_ts is not None and _U64_RE.fullmatch(raw_ts) and int(raw_ts) <= _MAX_U64:
        sent_at = raw_ts
    else:
        sent_at = None
    raw_id = tags.get('id')
    if raw_id is None:
        message_id = None
    elif 1 <= len(raw_id) <= 128:
        message_id = raw_id
    else:
        raise _Malformed
    user_login = None
    candidates = []
    login_tag = tags.get('login')
    if login_tag:
        candidates.append(login_tag)
    if parsed['prefix']:
        candidates.append(parsed['prefix'].split('!', 1)[0].split('@', 1)[0])
    for candidate in candidates:
        lowered = candidate.lower()
        if _LOGIN_RE.fullmatch(lowered):
            user_login = lowered
            break
    raw_dn = tags.get('display-name')
    if raw_dn is None:
        display_name = None
    elif len(raw_dn) <= 128:
        display_name = raw_dn
    else:
        raise _Malformed
    badges = []
    raw_badges = tags.get('badges')
    if raw_badges:
        entries = raw_badges.split(',')
        if len(entries) > 64:
            raise _Malformed
        for entry in entries:
            name, sep, version = entry.partition('/')
            if (not sep or '/' in version or not 1 <= len(name) <= 64
                    or not 1 <= len(version) <= 64):
                raise _Malformed
            badges.append({'name': name, 'version': version})
    return {
        'schema': 'cafe.chat_event.v1',
        'channel': channel,
        'received_at': {'monotonic_ns': str(time.monotonic_ns()), 'utc': _utc_now()},
        'sent_at': sent_at,
        'message_id': message_id,
        'user_login': user_login,
        'display_name': display_name,
        'badges': badges,
        'text': text,
        'kind': kind,
        'session_id': session_id,
    }


async def _handle_line(parsed, ws, writer, channel, session_id):
    """Handle one parsed line; returns (outcome, malformed_delta)."""
    command = parsed['command'].upper()
    if command == 'PING':
        params = parsed['params']
        if not params or not params[-1]:
            return (None, 1)
        try:
            await readonly_send(ws, 'PONG :' + params[-1])
        except ChatError:
            return (None, 1)
        return (None, 0)
    if command == 'RECONNECT':
        try:
            event = _build_event(parsed, channel, session_id)
        except _Malformed:
            return ('reconnect', 1)
        try:
            writer.append(event)
        except ChatError as exc:
            if exc.args and exc.args[0] == 'invalid_event':
                return ('reconnect', 1)
            raise
        return ('reconnect', 0)
    try:
        event = _build_event(parsed, channel, session_id)
    except _Malformed:
        return (None, 1)
    if event is None:
        return (None, 0)
    try:
        writer.append(event)
    except ChatError as exc:
        if exc.args and exc.args[0] == 'invalid_event':
            return (None, 1)
        raise
    return ('appended', 0)


async def _backoff_sleep(sleep, delay, stop_event):
    """One sleep() call per retry; abort early when stop_event is set."""
    if stop_event is None:
        await sleep(delay)
        return
    waiter = asyncio.ensure_future(sleep(delay))
    try:
        while True:
            if stop_event.is_set():
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    if not waiter.cancelled():
                        raise
                return
            try:
                await asyncio.wait_for(asyncio.shield(waiter), _SLEEP_POLL)
            except asyncio.TimeoutError:
                continue
            return
    finally:
        if not waiter.done():
            waiter.cancel()


async def run_chat(channel, root, *, url=OFFICIAL_URL, max_connections=None,
                   stop_event=None, backoff_initial=1.0, backoff_max=30.0,
                   sleep=asyncio.sleep, session_id=None):
    """Run the anonymous read-only chat loop; return closed counters."""
    channel = validate_channel(channel)
    url = validate_url(url)
    max_connections, backoff_initial, backoff_max, session_id = validate_options(
        max_connections, backoff_initial, backoff_max, sleep, session_id)
    writer = JsonlWriter(root, channel)
    messages = 0
    malformed = 0
    reconnects = 0
    connections = 0
    current_backoff = backoff_initial
    nick = 'NICK justinfan%d' % random.randint(0, 9999999999)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_connections is not None and connections >= max_connections:
                break
            if connections:
                reconnects += 1
            connections += 1
            try:
                ws = await open_connection(url)
            except ChatError as exc:
                if not exc.args or exc.args[0] != 'network_failed':
                    raise
            else:
                buf = ''
                try:
                    await readonly_send(ws, nick)
                    await readonly_send(ws, _CAP_REQ)
                    await readonly_send(ws, 'JOIN #' + channel)
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT)
                        except asyncio.TimeoutError:
                            writer.flush_due()
                            continue
                        if isinstance(msg, bytes):
                            try:
                                msg = msg.decode('utf-8')
                            except UnicodeDecodeError:
                                malformed += 1
                                continue
                        if type(msg) is not str:
                            malformed += 1
                            continue
                        buf += msg
                        if len(buf) > _BUF_LIMIT:
                            malformed += 1
                            buf = ''
                            continue
                        chunks = buf.split('\n')
                        buf = chunks.pop()
                        reconnect_now = False
                        for raw in chunks:
                            parsed = parse_irc(raw)
                            if parsed is None:
                                malformed += 1
                                continue
                            outcome, bad = await _handle_line(
                                parsed, ws, writer, channel, session_id)
                            malformed += bad
                            if outcome == 'appended':
                                messages += 1
                            elif outcome == 'reconnect':
                                reconnect_now = True
                                break
                        writer.flush_due()
                        if reconnect_now:
                            break
                    if buf:
                        malformed += 1
                        buf = ''
                except asyncio.CancelledError:
                    raise
                except ChatError:
                    raise
                except Exception:
                    if buf:
                        malformed += 1
                        buf = ''
                finally:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if stop_event is not None and stop_event.is_set():
                break
            if max_connections is not None and connections >= max_connections:
                break
            delay = current_backoff
            if delay > 0:
                delay += random.uniform(0.0, delay)
            if delay > backoff_max:
                delay = backoff_max
            await _backoff_sleep(sleep, delay, stop_event)
            current_backoff *= 2.0
            if current_backoff > backoff_max:
                current_backoff = backoff_max
        return {'messages': messages, 'malformed': malformed,
                'reconnects': reconnects, 'connections': connections}
    finally:
        writer.close()
