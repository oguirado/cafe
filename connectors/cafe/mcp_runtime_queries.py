"""PRO-01: pure read-model proposal; live native integration remains pending.

The future mcp_server composition adds tool_definitions() to its six existing
chat definitions and dispatches here after its closed request validation. It
continues to own DataReader, iterator lifetime, and the original 65536-byte MCP
framing check (the text/structured transport can contain a result twice).
This module never acquires a snapshot, opens a file, logs, or imports control.

Injected native projection contract -- NOT an existing runtime wire protocol:
    provider(kind, arguments) -> None | {
        session_id: Id, authority_epoch: Clock, snapshot_id: Id,
        data: <projection below> | None,
        page: {offset: int, next_offset: int | None, complete: bool}}
The reliable in-process adapter owns session and immutable snapshot identity.
snapshot_id must change on ANY observation/page-set/completeness change, even
when authority revision is unchanged. The current runtime stdout snapshot has
neither this identity nor this adapter; passing that raw snapshot fails closed.
The provider receives the tool name, a fresh dict of canonical filters, limit
for paged tools, and snapshot_id (None initially, expected ID on continuation).
Native list calls also receive offset (initially 0). Public cursors are never
forwarded. Status/chat envelopes require offset=0 and next_offset=None.

Closed data projections preserve the native field names:
* list_capsules: {capsules: {id: {capsule_id, person_label, generation,
  feeds: {id: {feed_id, role, generation}}}}}.
* list_candidate_windows: {windows: [WindowView from windows_window_registry]}.
* recording_status: {recordings: [{capsule_id, state, feeds: [{feed_id, state,
  written_frames, backpressure}]}], recording_available: bool | None}.
* obs_status: {obs_available: bool | None, services: {obs: str | None}}.
* chat queries: {}. Already-validated events and DataReader.stats arrive through
  execute's keyword-only parameters. Missing native scalars are projected as
  explicit None by the adapter; data=None means the query is unavailable.

page.complete is the adapter's explicit attestation, never inferred from a
small window count, native total, or end of one native page. A terminal partial
page has next_offset=None, complete=False. Native pages must honor the requested
offset/limit; next_offset advances by exactly the returned row count.

One ReadModel belongs to one consumer session. Slots are in-memory display
aliases keyed by capsule ID AND generation; session/epoch changes clear them
and reject the transition call. No API translates a slot into an ID. Cursors
are bounded, in-memory pagination references, not authorization credentials;
they carry no native capability or confirmation and are never persisted.
At most 128 cursors/page fingerprints and 4096 slot identities are retained;
each cursor's native identity history also stops at 4096 entries. Exhausting
identity bookkeeping returns query_unavailable without recycling any alias;
the caller can start a new consumer session/ReadModel. Evicted cursors are
invalid_arguments. These are local memory budgets, not larger protocol limits.
Chat pages contain the newest remaining messages, ascending within each page,
exactly as recent_messages does; following a cursor moves toward older rows.
The adapter supplies a fresh DataReader-bounded iterable on each continuation.
"""

from collections import OrderedDict
import hashlib
import heapq
import itertools
import json
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

from . import mcp_queries as _queries
from .mcp_server import (MAX_RESPONSE, MAX_SCAN_BYTES, MAX_EVENT_LINE, MAX_FILES,
                         _canonical_clock, _utc, _valid_event)

__all__ = ['ReadModel', 'ReadResult', 'tool_definitions']

Id = str
Clock = str
ErrorCode = Literal['invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
                    'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large']
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}')
_CHANNEL = re.compile(r'[a-z0-9_]{3,25}')
_LOGIN = re.compile(r'[a-z0-9_]{1,25}')
_U64 = (1 << 64) - 1
_U32 = (1 << 32) - 1
_MODEL_SERIALS = itertools.count(1)
_CURSOR_CAP = 128
_IDENTITY_CAP = 4096
_SCAN_FIELDS = frozenset({'corrupt_lines', 'truncated_lines', 'bytes_scanned',
                          'files_scanned', 'scan_limited'})
_ERRORS = ('invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
           'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large')
_NATIVE_LISTS = frozenset({'list_capsules', 'list_candidate_windows'})
_CHAT = frozenset({'messages_by_user', 'messages_between'})
_PAGED = _NATIVE_LISTS | _CHAT
_NAMES = ('list_capsules', 'list_candidate_windows', 'recording_status',
          'obs_status', 'messages_by_user', 'messages_between')


class PageArgs(TypedDict, total=False):
    limit: int
    cursor: Optional[str]


class Page(TypedDict):
    next_cursor: Optional[str]
    complete: bool


class Scan(TypedDict):
    corrupt_lines: int
    truncated_lines: int
    bytes_scanned: int
    files_scanned: int
    scan_limited: bool


class ReadResult(TypedDict):
    schema: Literal['cafe.mcp_read.v1']
    availability: Literal['available', 'unavailable']
    data: Union[Dict[str, Any], List[Any], None]
    page: Page
    scan: Optional[Scan]
    error: Optional[ErrorCode]


class _Failure(Exception):
    def __init__(self, code):
        self.code = code


def _require(condition, code='invalid_snapshot'):
    if not condition:
        raise _Failure(code)


def _closed(value, fields):
    _require(type(value) is dict and set(value) == set(fields))


def _id(value):
    return type(value) is str and _IDENT.fullmatch(value) is not None


def _integer(value, minimum=0, maximum=_U64):
    return type(value) is int and minimum <= value <= maximum


def _text(value, nonempty=False):
    if type(value) is not str or nonempty and not value:
        return False
    try:
        value.encode('utf-8')
        return True
    except UnicodeError:
        return False


def _nullable_bool(value):
    return value is None or type(value) is bool


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _result(data=None, *, complete=False, cursor=None, scan=None, error=None):
    return {'schema': 'cafe.mcp_read.v1',
            'availability': 'unavailable' if data is None else 'available',
            'data': data, 'page': {'next_cursor': cursor, 'complete': complete},
            'scan': scan, 'error': error}


def _bounded(result):
    # Conservative standard JSON representation plus newline. The future MCP
    # adapter must ALSO retain mcp_server's existing full-frame size check.
    size = 1
    for part in json.JSONEncoder(allow_nan=False).iterencode(result):
        size += len(part.encode('utf-8'))
        if size > MAX_RESPONSE:
            return _result(error='response_too_large', scan=result['scan'])
    return result


def _object(properties):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': sorted(properties)}


def _null(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


def tool_definitions() -> List[Dict[str, Any]]:
    """Return six fresh, closed, typed definitions; existing chat tools stay put."""
    ident = {'type': 'string', 'pattern': '^' + _IDENT.pattern + '$', 'maxLength': 96}
    clock = {'type': 'string', 'pattern': '^(0|[1-9][0-9]{0,19})$',
             'maxLength': 20, 'description': 'Canonical decimal u64, at most 18446744073709551615.'}
    string = {'type': 'string'}
    boolean = {'type': 'boolean'}
    count = {'type': 'integer', 'minimum': 0, 'maximum': _U64}
    feed = _object({'feed_id': ident, 'role': {'enum': ['game', 'camera']}, 'generation': clock})
    capsule = _object({'capsule_id': ident, 'slot': {'type': 'string', 'pattern': '^slot-[1-9][0-9]*$'},
                       'person_label': string, 'generation': clock,
                       'feeds': {'type': 'array', 'items': feed, 'maxItems': 2}})
    window = _object({'window_token': ident, 'generation': clock, 'layout_revision': clock,
                      'title': string, 'executable': string,
                      'rect': _object({name: {'type': 'integer', 'minimum': 1, 'maximum': _U32}
                                       for name in ('width', 'height')}),
                      'visible': boolean, 'minimized': boolean})
    recording_feed = _object({'feed_id': ident, 'state': string,
                              'written_frames': _null(clock), 'backpressure': _null(clock)})
    recording = _object({'capsule_id': ident, 'state': string,
                         'feeds': {'type': 'array', 'items': recording_feed}})
    utc = {'type': 'string', 'maxLength': 30,
           'pattern': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$',
           'description': 'Calendar-valid UTC; the interval is [from,to).'}
    channel = {'type': 'string', 'pattern': '^' + _CHANNEL.pattern + '$'}
    login = {'type': 'string', 'pattern': '^' + _LOGIN.pattern + '$'}
    event = _object({
        'schema': {'const': 'cafe.chat_event.v1'}, 'channel': channel,
        'received_at': _object({'monotonic_ns': clock, 'utc': utc}), 'sent_at': _null(clock),
        'message_id': _null({'type': 'string', 'minLength': 1, 'maxLength': 128}),
        'user_login': _null(login), 'display_name': _null({'type': 'string', 'maxLength': 128}),
        'badges': {'type': 'array', 'maxItems': 64, 'items': _object({
            name: {'type': 'string', 'minLength': 1, 'maxLength': 64} for name in ('name', 'version')})},
        'text': {'type': 'string', 'maxLength': 16384},
        'kind': {'enum': ['message', 'join_state', 'notice', 'clear', 'reconnect']}, 'session_id': ident})
    data_schemas = {
        'list_capsules': {'type': 'array', 'items': capsule, 'maxItems': 200},
        'list_candidate_windows': {'type': 'array', 'items': window, 'maxItems': 200},
        'recording_status': _object({'capsules': {'type': 'array', 'items': recording},
                                     'recorder_available': _null(boolean)}),
        'obs_status': _object({'available': _null(boolean), 'configured': _null(boolean),
                               'state': _null(string)}),
        'messages_by_user': {'type': 'array', 'items': event, 'maxItems': 200},
        'messages_between': {'type': 'array', 'items': event, 'maxItems': 200}}
    scan_schema = _object({name: boolean if name == 'scan_limited' else count
                           for name in sorted(_SCAN_FIELDS)})
    definitions = []
    for name in _NAMES:
        properties = {}
        required = []
        if name in _PAGED:
            properties.update({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
                               'cursor': _null({'type': 'string', 'minLength': 1, 'maxLength': 512,
                                                'description': 'Opaque local cursor, at most 512 UTF-8 bytes.'})})
        if name in _CHAT:
            properties.update({'channel': channel, 'from': utc, 'to': utc})
            required.extend(['channel', 'from', 'to'])
        if name == 'messages_by_user':
            properties['user_login'] = login
            required.append('user_login')
        if name == 'recording_status':
            properties['capsule_id'] = ident
        output = _object({'schema': {'const': 'cafe.mcp_read.v1'},
                          'availability': {'enum': ['available', 'unavailable']},
                          'data': _null(data_schemas[name]),
                          'page': _object({'next_cursor': _null({'type': 'string', 'maxLength': 512}),
                                           'complete': boolean}),
                          'scan': _null(scan_schema), 'error': _null({'enum': list(_ERRORS)})})
        definitions.append({'name': name, 'description': 'Read-only snapshot query: ' + name,
                            'inputSchema': {'type': 'object', 'additionalProperties': False,
                                            'properties': properties, 'required': sorted(required)},
                            'outputSchema': output,
                            'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                            'idempotentHint': True, 'openWorldHint': False}})
    # Detach even schema subobjects shared internally during construction.
    return json.loads(_json(definitions))


def _arguments(name, arguments):
    _require(type(name) is str and name in _NAMES, 'query_unavailable')
    _require(type(arguments) is dict, 'invalid_arguments')
    allowed = {'limit', 'cursor'} if name in _PAGED else set()
    required = set()
    if name in _CHAT:
        required = {'channel', 'from', 'to'}
    if name == 'messages_by_user':
        required.add('user_login')
    if name == 'recording_status':
        allowed.add('capsule_id')
    allowed |= required
    _require(not set(arguments) - allowed and required <= set(arguments), 'invalid_arguments')
    args = dict(arguments)
    if name in _PAGED:
        args.setdefault('limit', _queries.DEFAULT_LIMIT)
        _require(_integer(args['limit'], 1, _queries.MAX_LIMIT), 'invalid_arguments')
        cursor = args.pop('cursor', None)
        _require(cursor is None or _text(cursor, True) and len(cursor.encode('utf-8')) <= 512,
                 'invalid_arguments')
    else:
        cursor = None
    if 'capsule_id' in args:
        _require(_id(args['capsule_id']), 'invalid_arguments')
    if name in _CHAT:
        _require(type(args['channel']) is str and _CHANNEL.fullmatch(args['channel']) is not None,
                 'invalid_arguments')
        _require(_utc(args['from']) and _utc(args['to']), 'invalid_arguments')
        _require(_queries._utc_to_ns(args['from']) < _queries._utc_to_ns(args['to']), 'invalid_arguments')
    if name == 'messages_by_user':
        _require(type(args['user_login']) is str and _LOGIN.fullmatch(args['user_login']) is not None,
                 'invalid_arguments')
    return args, cursor


def _envelope(value):
    _closed(value, {'session_id', 'authority_epoch', 'snapshot_id', 'data', 'page'})
    _require(_id(value['session_id']) and _canonical_clock(value['authority_epoch'])
             and _id(value['snapshot_id']))
    page = value['page']
    _closed(page, {'offset', 'next_offset', 'complete'})
    _require(_integer(page['offset']) and type(page['complete']) is bool)
    _require(page['next_offset'] is None or _integer(page['next_offset'], 1))
    _require(page['next_offset'] is None or not page['complete'])
    return (value['session_id'], value['authority_epoch'], value['snapshot_id']), dict(page)


def _scan_summary(scan):
    if scan is None:
        return None
    _closed(scan, _SCAN_FIELDS)
    _require(type(scan['scan_limited']) is bool and
             all(_integer(scan[name]) for name in _SCAN_FIELDS - {'scan_limited'}))
    # DataReader stops after the first read chunk crossing its byte budget;
    # its file counter never exceeds MAX_FILES, even when enumeration is cut.
    _require(scan['files_scanned'] <= MAX_FILES and
             scan['bytes_scanned'] <= MAX_SCAN_BYTES + MAX_EVENT_LINE + 1)
    _require(scan['bytes_scanned'] <= MAX_SCAN_BYTES or scan['scan_limited'])
    return dict(scan)


def _capsules(data):
    _closed(data, {'capsules'})
    capsules = data['capsules']
    _require(type(capsules) is dict)
    rows, feed_ids = [], set()
    _require(all(_id(key) for key in capsules))
    for key in sorted(capsules):
        capsule = capsules[key]
        _closed(capsule, {'capsule_id', 'person_label', 'generation', 'feeds'})
        _require(_id(capsule['capsule_id']) and key == capsule['capsule_id']
                 and _canonical_clock(capsule['generation']))
        label = capsule['person_label']
        _require(_text(label, True) and bool(label.strip()) and len(label.encode('utf-8')) <= 256)
        feeds = capsule['feeds']
        _require(type(feeds) is dict and len(feeds) <= 2 and all(_id(key) for key in feeds))
        children, roles = [], set()
        for feed_key in sorted(feeds):
            feed = feeds[feed_key]
            _closed(feed, {'feed_id', 'role', 'generation'})
            _require(_id(feed['feed_id']) and feed['feed_id'] == feed_key
                     and feed_key not in feed_ids and _canonical_clock(feed['generation']))
            _require(type(feed['role']) is str and feed['role'] in {'game', 'camera'}
                     and feed['role'] not in roles)
            roles.add(feed['role'])
            feed_ids.add(feed_key)
            children.append(dict(feed))
        rows.append({'capsule_id': key, 'person_label': label,
                     'generation': capsule['generation'], 'feeds': children})
    return rows


def _windows(data):
    _closed(data, {'windows'})
    _require(type(data['windows']) is list)
    rows, handles = [], set()
    for view in data['windows']:
        _closed(view, {'handle', 'generation', 'layout_revision', 'process_id',
                       'executable', 'title', 'class', 'rect', 'visible', 'minimized'})
        _require(_id(view['handle']) and view['handle'] not in handles)
        _require(_canonical_clock(view['generation']) and _canonical_clock(view['layout_revision']))
        _require(_integer(view['process_id'], 1, _U32) and _text(view['title'])
                 and _text(view['executable'], True) and _text(view['class'], True))
        _require(type(view['visible']) is bool and type(view['minimized']) is bool)
        rect = view['rect']
        _closed(rect, {'x', 'y', 'width', 'height'})
        _require(all(_integer(rect[key], -(1 << 31), (1 << 31) - 1) for key in ('x', 'y')))
        _require(all(_integer(rect[key], 1, _U32) for key in ('width', 'height')))
        handles.add(view['handle'])
        rows.append({'window_token': view['handle'], 'generation': view['generation'],
                     'layout_revision': view['layout_revision'], 'title': view['title'],
                     'executable': view['executable'],
                     'rect': {'width': rect['width'], 'height': rect['height']},
                     'visible': view['visible'], 'minimized': view['minimized']})
    return rows


def _recordings(data, capsule_id):
    _closed(data, {'recordings', 'recording_available'})
    _require(type(data['recordings']) is list and _nullable_bool(data['recording_available']))
    rows, capsule_ids, feed_ids = [], set(), set()
    for capsule in data['recordings']:
        _closed(capsule, {'capsule_id', 'state', 'feeds'})
        _require(_id(capsule['capsule_id']) and capsule['capsule_id'] not in capsule_ids
                 and _text(capsule['state'], True) and type(capsule['feeds']) is list)
        capsule_ids.add(capsule['capsule_id'])
        feeds = []
        for feed in capsule['feeds']:
            _closed(feed, {'feed_id', 'state', 'written_frames', 'backpressure'})
            _require(_id(feed['feed_id']) and feed['feed_id'] not in feed_ids and _text(feed['state'], True))
            _require(all(feed[name] is None or _canonical_clock(feed[name])
                         for name in ('written_frames', 'backpressure')))
            feed_ids.add(feed['feed_id'])
            feeds.append(dict(feed))
        if capsule_id is None or capsule_id == capsule['capsule_id']:
            rows.append({'capsule_id': capsule['capsule_id'], 'state': capsule['state'],
                         'feeds': feeds})
    return {'capsules': rows,
            'recorder_available': data['recording_available']}


def _obs(data):
    _closed(data, {'obs_available', 'services'})
    _require(_nullable_bool(data['obs_available']))
    _closed(data['services'], {'obs'})
    state = data['services']['obs']
    _require(state is None or _text(state, True))
    # Native runtime_service_owner projects RuntimeServices' literal obs state.
    # obs_configurable is deliberately not consumed: it describes capability.
    configured = True if state == 'configured' else False if state == 'absent' else None
    return {'available': data['obs_available'], 'configured': configured, 'state': state}


def _candidate_key(candidate):
    return (candidate.recv_ns, candidate.session, candidate.mono_ns, candidate.msg, candidate.seq)


class ReadModel:
    def __init__(self, snapshot_provider=None):
        self._provider = snapshot_provider
        self._session = None
        self._slots = {}
        self._next_slot = 1
        self._serial = next(_MODEL_SERIALS)
        self._cursor_serial = 0
        self._cursors = OrderedDict()
        self._observed_pages = OrderedDict()

    def _observe_session(self, identity):
        session = identity[:2]
        if self._session is None:
            self._session = session
        elif self._session != session:
            self._session = session
            self._slots.clear()
            self._next_slot = 1
            self._cursors.clear()
            self._observed_pages.clear()
            raise _Failure('invalid_snapshot')

    def _remember(self, table, key, value):
        table[key] = value
        table.move_to_end(key)
        while len(table) > _CURSOR_CAP:
            table.popitem(last=False)

    def _cursor(self, query, identity, position, digest, seen):
        self._cursor_serial += 1
        # No entropy, clock, credential, or confirmation minting. Only the
        # issuing instance's in-memory entry can give this pagination meaning.
        material = [self._serial, self._cursor_serial, query, identity, position, digest]
        reference = 'read-' + hashlib.sha256(_json(material)).hexdigest()
        return reference, {'query': query, 'identity': identity, 'position': position,
                           'digest': digest, 'seen': seen}

    def _invalidate_snapshot(self, identity):
        for reference in tuple(self._cursors):
            if self._cursors[reference]['identity'] == identity:
                del self._cursors[reference]

    def _chat_page(self, args, events, scan, prior, native_complete):
        _require(events is not None, 'query_unavailable')
        _require(type(events) not in (dict, str, bytes, bytearray))
        lower, upper = _queries._utc_to_ns(args['from']), _queries._utc_to_ns(args['to'])
        limit = args['limit']
        boundary = prior['position'] if prior is not None else None
        heap, sequence = [], 0
        digest = hashlib.sha256()
        for event in events:
            _require(_valid_event(event))
            encoded = _json(event)
            digest.update(encoded)
            digest.update(b'\n')
            if event['kind'] != 'message' or event['channel'] != args['channel']:
                continue
            instant = _queries._recv_ns(event)
            if not lower <= instant < upper:
                continue
            if 'user_login' in args and event['user_login'] != args['user_login']:
                continue
            candidate = _queries._Candidate(None, instant, _queries._mono_ns(event),
                                             _queries._msg_key(event), event['session_id'], sequence)
            sequence += 1
            if boundary is not None and _candidate_key(candidate) >= boundary:
                continue
            # Copy at observation time: a generator may reuse/mutate its dict.
            candidate.event = json.loads(encoded)
            heapq.heappush(heap, candidate)
            if len(heap) > limit + 1:
                heapq.heappop(heap)
        final_scan = _scan_summary(scan)  # DataReader mutates stats during iteration.
        digest.update(_json([final_scan, native_complete]))
        fingerprint = digest.hexdigest()
        if prior is not None and fingerprint != prior['digest']:
            raise _Failure('stale_cursor')
        heap.sort()  # Reuses mcp_queries' total order, including in-session ties.
        more = len(heap) > limit
        selected = heap[-limit:]
        position = _candidate_key(selected[0]) if more else None
        limited = final_scan is not None and final_scan['scan_limited']
        clean = final_scan is not None and not any(final_scan[name]
                    for name in ('corrupt_lines', 'truncated_lines', 'scan_limited'))
        return ([candidate.event for candidate in selected], position, fingerprint,
                bool(native_complete and clean and not more), final_scan,
                'scan_limited' if limited else None)

    def execute(self, name, arguments, *, events=None, scan=None) -> ReadResult:
        """Run one closed read query; callbacks are trusted in-process reads only."""
        try:
            args, cursor = _arguments(name, arguments)
            query = hashlib.sha256(_json([name, args])).hexdigest()
            prior = None
            if cursor is not None:
                prior = self._cursors.get(cursor)
                _require(prior is not None and prior['query'] == query, 'invalid_arguments')
            if self._provider is None:
                raise _Failure('provider_unavailable')
            _require(callable(self._provider))
            supplied = dict(args)
            supplied['snapshot_id'] = None if prior is None else prior['identity'][2]
            offset = 0 if prior is None else prior['position']
            if name in _NATIVE_LISTS:
                supplied['offset'] = offset
            try:
                snapshot = self._provider(name, supplied)
            except Exception:
                raise _Failure('provider_unavailable') from None
            if snapshot is None:
                raise _Failure('provider_unavailable')
            identity, page = _envelope(snapshot)
            self._observe_session(identity)
            if prior is not None and identity != prior['identity']:
                raise _Failure('stale_cursor')
            raw = snapshot['data']
            _require(raw is not None, 'query_unavailable')
            position, digest, final_scan, error = None, None, None, None
            slots, next_slot = dict(self._slots), self._next_slot
            seen = () if prior is None else prior['seen']
            observation = None
            if name in _NATIVE_LISTS:
                rows = _capsules(raw) if name == 'list_capsules' else _windows(raw)
                _require(page['offset'] == offset and len(rows) <= args['limit'])
                position = page['next_offset']
                _require(position is None or rows and position == offset + len(rows))
                if name == 'list_capsules':
                    identities = tuple(('capsule', row['capsule_id']) for row in rows)
                    identities += tuple(('feed', feed['feed_id'])
                                        for row in rows for feed in row['feeds'])
                else:
                    identities = tuple(('window', row['window_token']) for row in rows)
                _require(not set(identities).intersection(seen))
                seen = seen + identities
                _require(len(seen) <= _IDENTITY_CAP, 'query_unavailable')
                digest = hashlib.sha256(_json([raw, page])).hexdigest()
                observation = (query, identity, offset)
                if (observation in self._observed_pages and
                        self._observed_pages[observation] != digest):
                    self._invalidate_snapshot(identity)
                    raise _Failure('stale_cursor' if prior is not None else 'invalid_snapshot')
                if name == 'list_capsules':
                    for row in rows:
                        slot_key = (row['capsule_id'], row['generation'])
                        if slot_key not in slots:
                            _require(len(slots) < _IDENTITY_CAP, 'query_unavailable')
                            slots[slot_key] = 'slot-' + str(next_slot)
                            next_slot += 1
                        row['slot'] = slots[slot_key]
                data, complete = rows, page['complete']
            else:
                _require(page['offset'] == 0 and page['next_offset'] is None)
                if name in _CHAT:
                    _closed(raw, set())
                    data, position, digest, complete, final_scan, error = self._chat_page(
                        args, events, scan, prior, page['complete'])
                else:
                    data = (_recordings(raw, args.get('capsule_id')) if name == 'recording_status'
                            else _obs(raw))
                    complete = page['complete']
            reference, state = (None, None) if position is None else self._cursor(
                query, identity, position, digest, seen)
            result = _bounded(_result(data, complete=complete, cursor=reference,
                                      scan=final_scan, error=error))
            if result['error'] != 'response_too_large':
                self._slots, self._next_slot = slots, next_slot
                if reference is not None:
                    self._remember(self._cursors, reference, state)
                if observation is not None:
                    self._remember(self._observed_pages, observation, digest)
            return result
        except _Failure as error:
            return _result(error=error.code)
        except Exception:
            # Malformed native/event/scan objects never disclose exception text.
            return _result(error='invalid_snapshot')
