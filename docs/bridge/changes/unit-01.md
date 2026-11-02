# PRO-01 — Read-only runtime and chat query library

Scope: `unverified_proposal`. Base: `f1e874050e305645a90e64267e50f8625daae9bc`.

## Assumptions

1. Live native acquisition is pending. The injected in-process adapter must supply trustworthy session/epoch and immutable snapshot IDs that cover every observation and page; the current runtime stdout revision alone is insufficient.
2. The adapter honors the documented page request and explicitly attests completeness. A lone native window page remains partial; unknown native status fields are null.
3. One ReadModel is one consumer session. Slots are display-only aliases keyed by capsule ID plus generation; session/epoch changes reset them, and exhausted local bookkeeping refuses without alias recycling.
4. Chat input is the existing DataReader-validated bounded iterable plus its final scan summary. Each continuation supplies a fresh identical snapshot; pages preserve existing newest-selection and ascending-within-page order.
5. Later serial composition adds these six tools to mcp_server and retains its existing framing cap. This patch adds no acquisition, control, IPC, authentication, network, logging, or live acceptance evidence.

## Complete product diff

The diff contains exactly the two new product/test paths; these transport wrappers are excluded to avoid a self-referential patch.

```diff
diff --git a/connectors/cafe/mcp_runtime_readonly.py b/connectors/cafe/mcp_runtime_readonly.py
new file mode 100644
index 0000000..a4dac3d
--- /dev/null
+++ b/connectors/cafe/mcp_runtime_readonly.py
@@ -0,0 +1,597 @@
+"""PRO-01: pure read-model proposal; live native integration remains pending.
+
+The future mcp_server composition adds tool_definitions() to its six existing
+chat definitions and dispatches here after its closed request validation. It
+continues to own DataReader, iterator lifetime, and the original 65536-byte MCP
+framing check (the text/structured transport can contain a result twice).
+This module never acquires a snapshot, opens a file, logs, or imports control.
+
+Injected native projection contract -- NOT an existing runtime wire protocol:
+    provider(kind, arguments) -> None | {
+        session_id: Id, authority_epoch: Clock, snapshot_id: Id,
+        data: <projection below> | None,
+        page: {offset: int, next_offset: int | None, complete: bool}}
+The reliable in-process adapter owns session and immutable snapshot identity.
+snapshot_id must change on ANY observation/page-set/completeness change, even
+when authority revision is unchanged. The current runtime stdout snapshot has
+neither this identity nor this adapter; passing that raw snapshot fails closed.
+The provider receives the tool name, a fresh dict of canonical filters, limit
+for paged tools, and snapshot_id (None initially, expected ID on continuation).
+Native list calls also receive offset (initially 0). Public cursors are never
+forwarded. Status/chat envelopes require offset=0 and next_offset=None.
+
+Closed data projections preserve the native field names:
+* list_capsules: {capsules: {id: {capsule_id, person_label, generation,
+  feeds: {id: {feed_id, role, generation}}}}}.
+* list_candidate_windows: {windows: [WindowView from windows_window_registry]}.
+* recording_status: {recordings: [{capsule_id, state, feeds: [{feed_id, state,
+  written_frames, backpressure}]}], recording_available: bool | None}.
+* obs_status: {obs_available: bool | None, services: {obs: str | None}}.
+* chat queries: {}. Already-validated events and DataReader.stats arrive through
+  execute's keyword-only parameters. Missing native scalars are projected as
+  explicit None by the adapter; data=None means the query is unavailable.
+
+page.complete is the adapter's explicit attestation, never inferred from a
+small window count, native total, or end of one native page. A terminal partial
+page has next_offset=None, complete=False. Native pages must honor the requested
+offset/limit; next_offset advances by exactly the returned row count.
+
+One ReadModel belongs to one consumer session. Slots are in-memory display
+aliases keyed by capsule ID AND generation; session/epoch changes clear them
+and reject the transition call. No API translates a slot into an ID. Cursors
+are bounded, in-memory pagination references, not authorization credentials;
+they carry no native capability or confirmation and are never persisted.
+At most 128 cursors/page fingerprints and 4096 slot identities are retained;
+each cursor's native identity history also stops at 4096 entries. Exhausting
+identity bookkeeping returns query_unavailable without recycling any alias;
+the caller can start a new consumer session/ReadModel. Evicted cursors are
+invalid_arguments. These are local memory budgets, not larger protocol limits.
+Chat pages contain the newest remaining messages, ascending within each page,
+exactly as recent_messages does; following a cursor moves toward older rows.
+The adapter supplies a fresh DataReader-bounded iterable on each continuation.
+"""
+
+from collections import OrderedDict
+import hashlib
+import heapq
+import itertools
+import json
+import re
+from typing import Any, Dict, List, Literal, Optional, TypedDict, Union
+
+from . import mcp_queries as _queries
+from .mcp_server import (MAX_RESPONSE, MAX_SCAN_BYTES, MAX_EVENT_LINE, MAX_FILES,
+                         _canonical_clock, _utc, _valid_event)
+
+__all__ = ['ReadModel', 'ReadResult', 'tool_definitions']
+
+Id = str
+Clock = str
+ErrorCode = Literal['invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
+                    'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large']
+_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}')
+_CHANNEL = re.compile(r'[a-z0-9_]{3,25}')
+_LOGIN = re.compile(r'[a-z0-9_]{1,25}')
+_U64 = (1 << 64) - 1
+_U32 = (1 << 32) - 1
+_MODEL_SERIALS = itertools.count(1)
+_CURSOR_CAP = 128
+_IDENTITY_CAP = 4096
+_SCAN_FIELDS = frozenset({'corrupt_lines', 'truncated_lines', 'bytes_scanned',
+                          'files_scanned', 'scan_limited'})
+_ERRORS = ('invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
+           'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large')
+_NATIVE_LISTS = frozenset({'list_capsules', 'list_candidate_windows'})
+_CHAT = frozenset({'messages_by_user', 'messages_between'})
+_PAGED = _NATIVE_LISTS | _CHAT
+_NAMES = ('list_capsules', 'list_candidate_windows', 'recording_status',
+          'obs_status', 'messages_by_user', 'messages_between')
+
+
+class PageArgs(TypedDict, total=False):
+    limit: int
+    cursor: Optional[str]
+
+
+class Page(TypedDict):
+    next_cursor: Optional[str]
+    complete: bool
+
+
+class Scan(TypedDict):
+    corrupt_lines: int
+    truncated_lines: int
+    bytes_scanned: int
+    files_scanned: int
+    scan_limited: bool
+
+
+class ReadResult(TypedDict):
+    schema: Literal['cafe.mcp_read.v1']
+    availability: Literal['available', 'unavailable']
+    data: Union[Dict[str, Any], List[Any], None]
+    page: Page
+    scan: Optional[Scan]
+    error: Optional[ErrorCode]
+
+
+class _Failure(Exception):
+    def __init__(self, code):
+        self.code = code
+
+
+def _require(condition, code='invalid_snapshot'):
+    if not condition:
+        raise _Failure(code)
+
+
+def _closed(value, fields):
+    _require(type(value) is dict and set(value) == set(fields))
+
+
+def _id(value):
+    return type(value) is str and _IDENT.fullmatch(value) is not None
+
+
+def _integer(value, minimum=0, maximum=_U64):
+    return type(value) is int and minimum <= value <= maximum
+
+
+def _text(value, nonempty=False):
+    if type(value) is not str or nonempty and not value:
+        return False
+    try:
+        value.encode('utf-8')
+        return True
+    except UnicodeError:
+        return False
+
+
+def _nullable_bool(value):
+    return value is None or type(value) is bool
+
+
+def _json(value):
+    return json.dumps(value, ensure_ascii=False, allow_nan=False,
+                      sort_keys=True, separators=(',', ':')).encode('utf-8')
+
+
+def _result(data=None, *, complete=False, cursor=None, scan=None, error=None):
+    return {'schema': 'cafe.mcp_read.v1',
+            'availability': 'unavailable' if data is None else 'available',
+            'data': data, 'page': {'next_cursor': cursor, 'complete': complete},
+            'scan': scan, 'error': error}
+
+
+def _bounded(result):
+    # Conservative standard JSON representation plus newline. The future MCP
+    # adapter must ALSO retain mcp_server's existing full-frame size check.
+    size = 1
+    for part in json.JSONEncoder(allow_nan=False).iterencode(result):
+        size += len(part.encode('utf-8'))
+        if size > MAX_RESPONSE:
+            return _result(error='response_too_large', scan=result['scan'])
+    return result
+
+
+def _object(properties):
+    return {'type': 'object', 'additionalProperties': False,
+            'properties': properties, 'required': sorted(properties)}
+
+
+def _null(schema):
+    return {'anyOf': [schema, {'type': 'null'}]}
+
+
+def tool_definitions() -> List[Dict[str, Any]]:
+    """Return six fresh, closed, typed definitions; existing chat tools stay put."""
+    ident = {'type': 'string', 'pattern': '^' + _IDENT.pattern + '$', 'maxLength': 96}
+    clock = {'type': 'string', 'pattern': '^(0|[1-9][0-9]{0,19})$',
+             'maxLength': 20, 'description': 'Canonical decimal u64, at most 18446744073709551615.'}
+    string = {'type': 'string'}
+    boolean = {'type': 'boolean'}
+    count = {'type': 'integer', 'minimum': 0, 'maximum': _U64}
+    feed = _object({'feed_id': ident, 'role': {'enum': ['game', 'camera']}, 'generation': clock})
+    capsule = _object({'capsule_id': ident, 'slot': {'type': 'string', 'pattern': '^slot-[1-9][0-9]*$'},
+                       'person_label': string, 'generation': clock,
+                       'feeds': {'type': 'array', 'items': feed, 'maxItems': 2}})
+    window = _object({'window_token': ident, 'generation': clock, 'layout_revision': clock,
+                      'title': string, 'executable': string,
+                      'rect': _object({name: {'type': 'integer', 'minimum': 1, 'maximum': _U32}
+                                       for name in ('width', 'height')}),
+                      'visible': boolean, 'minimized': boolean})
+    recording_feed = _object({'feed_id': ident, 'state': string,
+                              'written_frames': _null(clock), 'backpressure': _null(clock)})
+    recording = _object({'capsule_id': ident, 'state': string,
+                         'feeds': {'type': 'array', 'items': recording_feed}})
+    utc = {'type': 'string', 'maxLength': 30,
+           'pattern': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$',
+           'description': 'Calendar-valid UTC; the interval is [from,to).'}
+    channel = {'type': 'string', 'pattern': '^' + _CHANNEL.pattern + '$'}
+    login = {'type': 'string', 'pattern': '^' + _LOGIN.pattern + '$'}
+    event = _object({
+        'schema': {'const': 'cafe.chat_event.v1'}, 'channel': channel,
+        'received_at': _object({'monotonic_ns': clock, 'utc': utc}), 'sent_at': _null(clock),
+        'message_id': _null({'type': 'string', 'minLength': 1, 'maxLength': 128}),
+        'user_login': _null(login), 'display_name': _null({'type': 'string', 'maxLength': 128}),
+        'badges': {'type': 'array', 'maxItems': 64, 'items': _object({
+            name: {'type': 'string', 'minLength': 1, 'maxLength': 64} for name in ('name', 'version')})},
+        'text': {'type': 'string', 'maxLength': 16384},
+        'kind': {'enum': ['message', 'join_state', 'notice', 'clear', 'reconnect']}, 'session_id': ident})
+    data_schemas = {
+        'list_capsules': {'type': 'array', 'items': capsule, 'maxItems': 200},
+        'list_candidate_windows': {'type': 'array', 'items': window, 'maxItems': 200},
+        'recording_status': _object({'capsules': {'type': 'array', 'items': recording},
+                                     'recorder_available': _null(boolean)}),
+        'obs_status': _object({'available': _null(boolean), 'configured': _null(boolean),
+                               'state': _null(string)}),
+        'messages_by_user': {'type': 'array', 'items': event, 'maxItems': 200},
+        'messages_between': {'type': 'array', 'items': event, 'maxItems': 200}}
+    scan_schema = _object({name: boolean if name == 'scan_limited' else count
+                           for name in sorted(_SCAN_FIELDS)})
+    definitions = []
+    for name in _NAMES:
+        properties = {}
+        required = []
+        if name in _PAGED:
+            properties.update({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
+                               'cursor': _null({'type': 'string', 'minLength': 1, 'maxLength': 512,
+                                                'description': 'Opaque local cursor, at most 512 UTF-8 bytes.'})})
+        if name in _CHAT:
+            properties.update({'channel': channel, 'from': utc, 'to': utc})
+            required.extend(['channel', 'from', 'to'])
+        if name == 'messages_by_user':
+            properties['user_login'] = login
+            required.append('user_login')
+        if name == 'recording_status':
+            properties['capsule_id'] = ident
+        output = _object({'schema': {'const': 'cafe.mcp_read.v1'},
+                          'availability': {'enum': ['available', 'unavailable']},
+                          'data': _null(data_schemas[name]),
+                          'page': _object({'next_cursor': _null({'type': 'string', 'maxLength': 512}),
+                                           'complete': boolean}),
+                          'scan': _null(scan_schema), 'error': _null({'enum': list(_ERRORS)})})
+        definitions.append({'name': name, 'description': 'Read-only snapshot query: ' + name,
+                            'inputSchema': {'type': 'object', 'additionalProperties': False,
+                                            'properties': properties, 'required': sorted(required)},
+                            'outputSchema': output,
+                            'annotations': {'readOnlyHint': True, 'destructiveHint': False,
+                                            'idempotentHint': True, 'openWorldHint': False}})
+    # Detach even schema subobjects shared internally during construction.
+    return json.loads(_json(definitions))
+
+
+def _arguments(name, arguments):
+    _require(type(name) is str and name in _NAMES, 'query_unavailable')
+    _require(type(arguments) is dict, 'invalid_arguments')
+    allowed = {'limit', 'cursor'} if name in _PAGED else set()
+    required = set()
+    if name in _CHAT:
+        required = {'channel', 'from', 'to'}
+    if name == 'messages_by_user':
+        required.add('user_login')
+    if name == 'recording_status':
+        allowed.add('capsule_id')
+    allowed |= required
+    _require(not set(arguments) - allowed and required <= set(arguments), 'invalid_arguments')
+    args = dict(arguments)
+    if name in _PAGED:
+        args.setdefault('limit', _queries.DEFAULT_LIMIT)
+        _require(_integer(args['limit'], 1, _queries.MAX_LIMIT), 'invalid_arguments')
+        cursor = args.pop('cursor', None)
+        _require(cursor is None or _text(cursor, True) and len(cursor.encode('utf-8')) <= 512,
+                 'invalid_arguments')
+    else:
+        cursor = None
+    if 'capsule_id' in args:
+        _require(_id(args['capsule_id']), 'invalid_arguments')
+    if name in _CHAT:
+        _require(type(args['channel']) is str and _CHANNEL.fullmatch(args['channel']) is not None,
+                 'invalid_arguments')
+        _require(_utc(args['from']) and _utc(args['to']), 'invalid_arguments')
+        _require(_queries._utc_to_ns(args['from']) < _queries._utc_to_ns(args['to']), 'invalid_arguments')
+    if name == 'messages_by_user':
+        _require(type(args['user_login']) is str and _LOGIN.fullmatch(args['user_login']) is not None,
+                 'invalid_arguments')
+    return args, cursor
+
+
+def _envelope(value):
+    _closed(value, {'session_id', 'authority_epoch', 'snapshot_id', 'data', 'page'})
+    _require(_id(value['session_id']) and _canonical_clock(value['authority_epoch'])
+             and _id(value['snapshot_id']))
+    page = value['page']
+    _closed(page, {'offset', 'next_offset', 'complete'})
+    _require(_integer(page['offset']) and type(page['complete']) is bool)
+    _require(page['next_offset'] is None or _integer(page['next_offset'], 1))
+    _require(page['next_offset'] is None or not page['complete'])
+    return (value['session_id'], value['authority_epoch'], value['snapshot_id']), dict(page)
+
+
+def _scan_summary(scan):
+    if scan is None:
+        return None
+    _closed(scan, _SCAN_FIELDS)
+    _require(type(scan['scan_limited']) is bool and
+             all(_integer(scan[name]) for name in _SCAN_FIELDS - {'scan_limited'}))
+    # DataReader stops after the first read chunk crossing its byte budget;
+    # its file counter never exceeds MAX_FILES, even when enumeration is cut.
+    _require(scan['files_scanned'] <= MAX_FILES and
+             scan['bytes_scanned'] <= MAX_SCAN_BYTES + MAX_EVENT_LINE + 1)
+    _require(scan['bytes_scanned'] <= MAX_SCAN_BYTES or scan['scan_limited'])
+    return dict(scan)
+
+
+def _capsules(data):
+    _closed(data, {'capsules'})
+    capsules = data['capsules']
+    _require(type(capsules) is dict)
+    rows, feed_ids = [], set()
+    _require(all(_id(key) for key in capsules))
+    for key in sorted(capsules):
+        capsule = capsules[key]
+        _closed(capsule, {'capsule_id', 'person_label', 'generation', 'feeds'})
+        _require(_id(capsule['capsule_id']) and key == capsule['capsule_id']
+                 and _canonical_clock(capsule['generation']))
+        label = capsule['person_label']
+        _require(_text(label, True) and bool(label.strip()) and len(label.encode('utf-8')) <= 256)
+        feeds = capsule['feeds']
+        _require(type(feeds) is dict and len(feeds) <= 2 and all(_id(key) for key in feeds))
+        children, roles = [], set()
+        for feed_key in sorted(feeds):
+            feed = feeds[feed_key]
+            _closed(feed, {'feed_id', 'role', 'generation'})
+            _require(_id(feed['feed_id']) and feed['feed_id'] == feed_key
+                     and feed_key not in feed_ids and _canonical_clock(feed['generation']))
+            _require(type(feed['role']) is str and feed['role'] in {'game', 'camera'}
+                     and feed['role'] not in roles)
+            roles.add(feed['role'])
+            feed_ids.add(feed_key)
+            children.append(dict(feed))
+        rows.append({'capsule_id': key, 'person_label': label,
+                     'generation': capsule['generation'], 'feeds': children})
+    return rows
+
+
+def _windows(data):
+    _closed(data, {'windows'})
+    _require(type(data['windows']) is list)
+    rows, handles = [], set()
+    for view in data['windows']:
+        _closed(view, {'handle', 'generation', 'layout_revision', 'process_id',
+                       'executable', 'title', 'class', 'rect', 'visible', 'minimized'})
+        _require(_id(view['handle']) and view['handle'] not in handles)
+        _require(_canonical_clock(view['generation']) and _canonical_clock(view['layout_revision']))
+        _require(_integer(view['process_id'], 1, _U32) and _text(view['title'])
+                 and _text(view['executable'], True) and _text(view['class'], True))
+        _require(type(view['visible']) is bool and type(view['minimized']) is bool)
+        rect = view['rect']
+        _closed(rect, {'x', 'y', 'width', 'height'})
+        _require(all(_integer(rect[key], -(1 << 31), (1 << 31) - 1) for key in ('x', 'y')))
+        _require(all(_integer(rect[key], 1, _U32) for key in ('width', 'height')))
+        handles.add(view['handle'])
+        rows.append({'window_token': view['handle'], 'generation': view['generation'],
+                     'layout_revision': view['layout_revision'], 'title': view['title'],
+                     'executable': view['executable'],
+                     'rect': {'width': rect['width'], 'height': rect['height']},
+                     'visible': view['visible'], 'minimized': view['minimized']})
+    return rows
+
+
+def _recordings(data, capsule_id):
+    _closed(data, {'recordings', 'recording_available'})
+    _require(type(data['recordings']) is list and _nullable_bool(data['recording_available']))
+    rows, capsule_ids, feed_ids = [], set(), set()
+    for capsule in data['recordings']:
+        _closed(capsule, {'capsule_id', 'state', 'feeds'})
+        _require(_id(capsule['capsule_id']) and capsule['capsule_id'] not in capsule_ids
+                 and _text(capsule['state'], True) and type(capsule['feeds']) is list)
+        capsule_ids.add(capsule['capsule_id'])
+        feeds = []
+        for feed in capsule['feeds']:
+            _closed(feed, {'feed_id', 'state', 'written_frames', 'backpressure'})
+            _require(_id(feed['feed_id']) and feed['feed_id'] not in feed_ids and _text(feed['state'], True))
+            _require(all(feed[name] is None or _canonical_clock(feed[name])
+                         for name in ('written_frames', 'backpressure')))
+            feed_ids.add(feed['feed_id'])
+            feeds.append(dict(feed))
+        if capsule_id is None or capsule_id == capsule['capsule_id']:
+            rows.append({'capsule_id': capsule['capsule_id'], 'state': capsule['state'],
+                         'feeds': feeds})
+    return {'capsules': rows,
+            'recorder_available': data['recording_available']}
+
+
+def _obs(data):
+    _closed(data, {'obs_available', 'services'})
+    _require(_nullable_bool(data['obs_available']))
+    _closed(data['services'], {'obs'})
+    state = data['services']['obs']
+    _require(state is None or _text(state, True))
+    # Native runtime_service_owner projects RuntimeServices' literal obs state.
+    # obs_configurable is deliberately not consumed: it describes capability.
+    configured = True if state == 'configured' else False if state == 'absent' else None
+    return {'available': data['obs_available'], 'configured': configured, 'state': state}
+
+
+def _candidate_key(candidate):
+    return (candidate.recv_ns, candidate.session, candidate.mono_ns, candidate.msg, candidate.seq)
+
+
+class ReadModel:
+    def __init__(self, snapshot_provider=None):
+        self._provider = snapshot_provider
+        self._session = None
+        self._slots = {}
+        self._next_slot = 1
+        self._serial = next(_MODEL_SERIALS)
+        self._cursor_serial = 0
+        self._cursors = OrderedDict()
+        self._observed_pages = OrderedDict()
+
+    def _observe_session(self, identity):
+        session = identity[:2]
+        if self._session is None:
+            self._session = session
+        elif self._session != session:
+            self._session = session
+            self._slots.clear()
+            self._next_slot = 1
+            self._cursors.clear()
+            self._observed_pages.clear()
+            raise _Failure('invalid_snapshot')
+
+    def _remember(self, table, key, value):
+        table[key] = value
+        table.move_to_end(key)
+        while len(table) > _CURSOR_CAP:
+            table.popitem(last=False)
+
+    def _cursor(self, query, identity, position, digest, seen):
+        self._cursor_serial += 1
+        # No entropy, clock, credential, or confirmation minting. Only the
+        # issuing instance's in-memory entry can give this pagination meaning.
+        material = [self._serial, self._cursor_serial, query, identity, position, digest]
+        reference = 'read-' + hashlib.sha256(_json(material)).hexdigest()
+        return reference, {'query': query, 'identity': identity, 'position': position,
+                           'digest': digest, 'seen': seen}
+
+    def _invalidate_snapshot(self, identity):
+        for reference in tuple(self._cursors):
+            if self._cursors[reference]['identity'] == identity:
+                del self._cursors[reference]
+
+    def _chat_page(self, args, events, scan, prior, native_complete):
+        _require(events is not None, 'query_unavailable')
+        _require(type(events) not in (dict, str, bytes, bytearray))
+        lower, upper = _queries._utc_to_ns(args['from']), _queries._utc_to_ns(args['to'])
+        limit = args['limit']
+        boundary = prior['position'] if prior is not None else None
+        heap, sequence = [], 0
+        digest = hashlib.sha256()
+        for event in events:
+            _require(_valid_event(event))
+            encoded = _json(event)
+            digest.update(encoded)
+            digest.update(b'\n')
+            if event['kind'] != 'message' or event['channel'] != args['channel']:
+                continue
+            instant = _queries._recv_ns(event)
+            if not lower <= instant < upper:
+                continue
+            if 'user_login' in args and event['user_login'] != args['user_login']:
+                continue
+            candidate = _queries._Candidate(None, instant, _queries._mono_ns(event),
+                                             _queries._msg_key(event), event['session_id'], sequence)
+            sequence += 1
+            if boundary is not None and _candidate_key(candidate) >= boundary:
+                continue
+            # Copy at observation time: a generator may reuse/mutate its dict.
+            candidate.event = json.loads(encoded)
+            heapq.heappush(heap, candidate)
+            if len(heap) > limit + 1:
+                heapq.heappop(heap)
+        final_scan = _scan_summary(scan)  # DataReader mutates stats during iteration.
+        digest.update(_json([final_scan, native_complete]))
+        fingerprint = digest.hexdigest()
+        if prior is not None and fingerprint != prior['digest']:
+            raise _Failure('stale_cursor')
+        heap.sort()  # Reuses mcp_queries' total order, including in-session ties.
+        more = len(heap) > limit
+        selected = heap[-limit:]
+        position = _candidate_key(selected[0]) if more else None
+        limited = final_scan is not None and final_scan['scan_limited']
+        clean = final_scan is not None and not any(final_scan[name]
+                    for name in ('corrupt_lines', 'truncated_lines', 'scan_limited'))
+        return ([candidate.event for candidate in selected], position, fingerprint,
+                bool(native_complete and clean and not more), final_scan,
+                'scan_limited' if limited else None)
+
+    def execute(self, name, arguments, *, events=None, scan=None) -> ReadResult:
+        """Run one closed read query; callbacks are trusted in-process reads only."""
+        try:
+            args, cursor = _arguments(name, arguments)
+            query = hashlib.sha256(_json([name, args])).hexdigest()
+            prior = None
+            if cursor is not None:
+                prior = self._cursors.get(cursor)
+                _require(prior is not None and prior['query'] == query, 'invalid_arguments')
+            if self._provider is None:
+                raise _Failure('provider_unavailable')
+            _require(callable(self._provider))
+            supplied = dict(args)
+            supplied['snapshot_id'] = None if prior is None else prior['identity'][2]
+            offset = 0 if prior is None else prior['position']
+            if name in _NATIVE_LISTS:
+                supplied['offset'] = offset
+            try:
+                snapshot = self._provider(name, supplied)
+            except Exception:
+                raise _Failure('provider_unavailable') from None
+            if snapshot is None:
+                raise _Failure('provider_unavailable')
+            identity, page = _envelope(snapshot)
+            self._observe_session(identity)
+            if prior is not None and identity != prior['identity']:
+                raise _Failure('stale_cursor')
+            raw = snapshot['data']
+            _require(raw is not None, 'query_unavailable')
+            position, digest, final_scan, error = None, None, None, None
+            slots, next_slot = dict(self._slots), self._next_slot
+            seen = () if prior is None else prior['seen']
+            observation = None
+            if name in _NATIVE_LISTS:
+                rows = _capsules(raw) if name == 'list_capsules' else _windows(raw)
+                _require(page['offset'] == offset and len(rows) <= args['limit'])
+                position = page['next_offset']
+                _require(position is None or rows and position == offset + len(rows))
+                if name == 'list_capsules':
+                    identities = tuple(('capsule', row['capsule_id']) for row in rows)
+                    identities += tuple(('feed', feed['feed_id'])
+                                        for row in rows for feed in row['feeds'])
+                else:
+                    identities = tuple(('window', row['window_token']) for row in rows)
+                _require(not set(identities).intersection(seen))
+                seen = seen + identities
+                _require(len(seen) <= _IDENTITY_CAP, 'query_unavailable')
+                digest = hashlib.sha256(_json([raw, page])).hexdigest()
+                observation = (query, identity, offset)
+                if (observation in self._observed_pages and
+                        self._observed_pages[observation] != digest):
+                    self._invalidate_snapshot(identity)
+                    raise _Failure('stale_cursor' if prior is not None else 'invalid_snapshot')
+                if name == 'list_capsules':
+                    for row in rows:
+                        slot_key = (row['capsule_id'], row['generation'])
+                        if slot_key not in slots:
+                            _require(len(slots) < _IDENTITY_CAP, 'query_unavailable')
+                            slots[slot_key] = 'slot-' + str(next_slot)
+                            next_slot += 1
+                        row['slot'] = slots[slot_key]
+                data, complete = rows, page['complete']
+            else:
+                _require(page['offset'] == 0 and page['next_offset'] is None)
+                if name in _CHAT:
+                    _closed(raw, set())
+                    data, position, digest, complete, final_scan, error = self._chat_page(
+                        args, events, scan, prior, page['complete'])
+                else:
+                    data = (_recordings(raw, args.get('capsule_id')) if name == 'recording_status'
+                            else _obs(raw))
+                    complete = page['complete']
+            reference, state = (None, None) if position is None else self._cursor(
+                query, identity, position, digest, seen)
+            result = _bounded(_result(data, complete=complete, cursor=reference,
+                                      scan=final_scan, error=error))
+            if result['error'] != 'response_too_large':
+                self._slots, self._next_slot = slots, next_slot
+                if reference is not None:
+                    self._remember(self._cursors, reference, state)
+                if observation is not None:
+                    self._remember(self._observed_pages, observation, digest)
+            return result
+        except _Failure as error:
+            return _result(error=error.code)
+        except Exception:
+            # Malformed native/event/scan objects never disclose exception text.
+            return _result(error='invalid_snapshot')
diff --git a/tests/integration/test_mcp_runtime_readonly.py b/tests/integration/test_mcp_runtime_readonly.py
new file mode 100644
index 0000000..a8a448c
--- /dev/null
+++ b/tests/integration/test_mcp_runtime_readonly.py
@@ -0,0 +1,903 @@
+"""Synthetic library contract checks; no live acceptance or MCP registration.
+
+Run with the standard library runner from the repository root:
+    python -B -m unittest discover -s tests/integration -p test_mcp_runtime_readonly.py
+
+The injected provider below describes the proposed native projection contract.
+It does not acquire native state, prove product integration, or close live markers.
+"""
+from contextlib import ExitStack, redirect_stderr, redirect_stdout
+from copy import deepcopy
+import inspect
+import io
+import itertools
+import json
+import logging
+import os
+from pathlib import Path
+import secrets
+import socket
+import subprocess
+import sys
+import unittest
+from unittest import mock
+import uuid
+
+ROOT = Path(__file__).resolve().parents[2]
+sys.path.insert(0, str(ROOT / 'connectors'))
+from cafe import mcp_control as control
+from cafe import mcp_queries as queries
+from cafe import mcp_runtime_readonly as runtime
+from cafe import mcp_server as server
+
+
+TOOLS = {'list_capsules', 'list_candidate_windows', 'recording_status',
+         'obs_status', 'messages_by_user', 'messages_between'}
+ERRORS = {'invalid_arguments', 'provider_unavailable', 'invalid_snapshot',
+          'stale_cursor', 'query_unavailable', 'scan_limited', 'response_too_large'}
+FROM = '2025-11-20T18:00:00Z'
+TO = '2025-11-20T19:00:00Z'
+CHAT = {'channel': 'demo_channel', 'from': FROM, 'to': TO}
+
+
+def scan(**changes):
+    result = {'corrupt_lines': 0, 'truncated_lines': 0, 'bytes_scanned': 1234,
+              'files_scanned': 1, 'scan_limited': False}
+    result.update(changes)
+    return result
+
+
+def event(index=1, **changes):
+    result = {
+        'schema': 'cafe.chat_event.v1', 'channel': 'demo_channel',
+        'received_at': {'monotonic_ns': str(index),
+                        'utc': '2025-11-20T18:00:%02dZ' % (index % 60)},
+        'sent_at': None, 'message_id': 'message_synth_%02d' % index,
+        'user_login': 'synthetic_guest_01', 'display_name': 'Synthetic guest 01',
+        'badges': [], 'text': 'Synthetic message %02d' % index,
+        'kind': 'message', 'session_id': 'session_synth_chat_01',
+    }
+    result.update(changes)
+    return result
+
+
+def capsule(index=1, generation='7'):
+    identity = 'capsule_synth_%02d' % index
+    game = 'feed_synth_game_%02d' % index
+    camera = 'feed_synth_camera_%02d' % index
+    return {'capsule_id': identity, 'person_label': 'synthetic_guest_%02d' % index,
+            'generation': generation,
+            'feeds': {game: {'feed_id': game, 'role': 'game', 'generation': '8'},
+                      camera: {'feed_id': camera, 'role': 'camera', 'generation': '9'}}}
+
+
+def window(index=1):
+    return {'handle': 'window_synth_%02d' % index, 'generation': '3',
+            'layout_revision': '4', 'process_id': 100 + index,
+            'executable': 'synthetic_app.exe', 'title': 'Synthetic shared title',
+            'class': 'SyntheticWindowClass',
+            'rect': {'x': -10, 'y': 20, 'width': 1920, 'height': 1080},
+            'visible': True, 'minimized': False}
+
+
+class SyntheticProvider:
+    """Owned in-memory fixture, never a product/native snapshot acquisition."""
+
+    def __init__(self):
+        self.session_id = 'session_synth_native_01'
+        self.epoch = '1'
+        self.snapshot_id = 'snapshot_synth_01'
+        self.capsules = [capsule()]
+        self.windows = [window()]
+        self.recordings = [{
+            'capsule_id': 'capsule_synth_01', 'state': 'recording',
+            'feeds': [{'feed_id': 'feed_synth_game_01', 'state': 'recording',
+                       'written_frames': '18446744073709551615', 'backpressure': None},
+                      {'feed_id': 'feed_synth_camera_01', 'state': 'stopped',
+                       'written_frames': None, 'backpressure': '0'}],
+        }]
+        self.recording_available = True
+        self.obs_available = True
+        self.obs_state = 'configured'
+        self.partial = False
+        self.transform = None
+        self.calls = []
+
+    def __call__(self, kind, arguments):
+        self.calls.append((kind, deepcopy(arguments)))
+        page = {'offset': 0, 'next_offset': None, 'complete': not self.partial}
+        if kind in {'list_capsules', 'list_candidate_windows'}:
+            source = self.capsules if kind == 'list_capsules' else self.windows
+            offset = arguments['offset']
+            rows = deepcopy(source[offset:offset + arguments['limit']])
+            more = offset + len(rows) < len(source)
+            page = {'offset': offset, 'next_offset': offset + len(rows) if more else None,
+                    'complete': not more and not self.partial}
+            data = ({'capsules': {row['capsule_id']: row for row in rows}}
+                    if kind == 'list_capsules' else {'windows': rows})
+        elif kind == 'recording_status':
+            data = {'recordings': deepcopy(self.recordings),
+                    'recording_available': self.recording_available}
+        elif kind == 'obs_status':
+            data = {'obs_available': self.obs_available, 'services': {'obs': self.obs_state}}
+        elif kind in {'messages_between', 'messages_by_user'}:
+            data = {}
+        else:
+            raise AssertionError('unexpected synthetic provider query')
+        result = {'session_id': self.session_id, 'authority_epoch': self.epoch,
+                  'snapshot_id': self.snapshot_id, 'data': data, 'page': page}
+        if self.transform is not None:
+            self.transform(result)
+        return result
+
+
+class ReadModelTests(unittest.TestCase):
+    def setUp(self):
+        self.provider = SyntheticProvider()
+        self.model = runtime.ReadModel(self.provider)
+
+    def assert_result(self, result, *, error=None, available=True):
+        self.assertIs(type(result), dict)
+        self.assertEqual(set(result), {'schema', 'availability', 'data', 'page', 'scan', 'error'})
+        self.assertEqual(result['schema'], 'cafe.mcp_read.v1')
+        self.assertEqual(result['availability'], 'available' if available else 'unavailable')
+        self.assertIn(type(result['data']), (dict, list, type(None)))
+        self.assertIs(type(result['page']), dict)
+        self.assertEqual(set(result['page']), {'next_cursor', 'complete'})
+        self.assertIs(type(result['page']['complete']), bool)
+        cursor = result['page']['next_cursor']
+        self.assertTrue(cursor is None or type(cursor) is str and len(cursor.encode('utf-8')) <= 512)
+        self.assertTrue(result['error'] is None or result['error'] in ERRORS)
+        self.assertEqual(result['error'], error)
+        if result['scan'] is not None:
+            self.assertIs(type(result['scan']), dict)
+            self.assertEqual(set(result['scan']), set(scan()))
+            self.assertIs(type(result['scan']['scan_limited']), bool)
+            for key in set(scan()) - {'scan_limited'}:
+                self.assertIs(type(result['scan'][key]), int)
+                self.assertGreaterEqual(result['scan'][key], 0)
+        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode('utf-8')) + 1,
+                             server.MAX_RESPONSE)
+        if not available:
+            self.assertIsNone(result['data'])
+            self.assertFalse(result['page']['complete'])
+            self.assertIsNone(result['page']['next_cursor'])
+        return result
+
+    def assert_error(self, result, code):
+        return self.assert_result(result, error=code, available=False)
+
+    def chat(self, events, *, name='messages_between', arguments=None, stats=None):
+        return self.model.execute(name, dict(CHAT) if arguments is None else arguments,
+                                  events=iter(events), scan=scan() if stats is None else stats)
+
+    def test_frozen_signatures_have_keyword_only_events_and_scan(self):
+        self.assertEqual(str(inspect.signature(runtime.ReadModel)), '(snapshot_provider=None)')
+        params = inspect.signature(runtime.ReadModel.execute).parameters
+        self.assertEqual(list(params), ['self', 'name', 'arguments', 'events', 'scan'])
+        self.assertEqual(params['events'].kind, inspect.Parameter.KEYWORD_ONLY)
+        self.assertEqual(params['scan'].kind, inspect.Parameter.KEYWORD_ONLY)
+        with self.assertRaises(TypeError):
+            self.model.execute('messages_between', CHAT, [], scan())
+
+    def test_tool_definitions_are_closed_typed_readonly_and_disjoint(self):
+        definitions = runtime.tool_definitions()
+        self.assertIs(type(definitions), list)
+        self.assertEqual(len(definitions), 6)
+        self.assertEqual({item['name'] for item in definitions}, TOOLS)
+        self.assertTrue(TOOLS.isdisjoint({item['name'] for item in server.tools()}))
+        for item in definitions:
+            with self.subTest(tool=item['name']):
+                self.assertIs(type(item), dict)
+                self.assertIs(type(item['description']), str)
+                schema = item['inputSchema']
+                self.assertEqual(schema['type'], 'object')
+                self.assertIs(schema['additionalProperties'], False)
+                self.assertIs(type(schema['properties']), dict)
+                self.assertIs(type(schema.get('required', [])), list)
+                self.assertTrue(set(schema.get('required', [])) <= set(schema['properties']))
+                self.assertIs(item['annotations']['readOnlyHint'], True)
+                self.assertIs(item['annotations']['destructiveHint'], False)
+                if 'limit' in schema['properties']:
+                    self.assertEqual(schema['properties']['limit']['type'], 'integer')
+                    self.assertEqual(schema['properties']['limit']['default'], 50)
+                    self.assertEqual(schema['properties']['limit']['maximum'], 200)
+        definitions[0]['inputSchema']['properties']['synthetic_poison'] = {}
+        self.assertNotIn('synthetic_poison', runtime.tool_definitions()[0]['inputSchema']['properties'])
+
+    def test_list_capsules_happy_path_preserves_ids_clocks_and_display_label(self):
+        result = self.assert_result(self.model.execute('list_capsules', {}))
+        self.assertTrue(result['page']['complete'])
+        self.assertIsNone(result['scan'])
+        self.assertEqual(len(result['data']), 1)
+        row = result['data'][0]
+        self.assertEqual(set(row), {'capsule_id', 'slot', 'person_label', 'generation', 'feeds'})
+        self.assertEqual({key: row[key] for key in ('capsule_id', 'slot', 'person_label', 'generation')},
+                         {'capsule_id': 'capsule_synth_01', 'slot': 'slot-1',
+                          'person_label': 'synthetic_guest_01', 'generation': '7'})
+        self.assertEqual({feed['role']: feed for feed in row['feeds']},
+                         {feed['role']: feed for feed in capsule()['feeds'].values()})
+        self.assertEqual(self.provider.calls, [('list_capsules',
+                         {'limit': 50, 'offset': 0, 'snapshot_id': None})])
+
+    def test_list_candidate_windows_projects_native_fields_only(self):
+        self.provider.windows = [window(1), window(2)]
+        result = self.assert_result(self.model.execute('list_candidate_windows', {}))
+        self.assertEqual([row['window_token'] for row in result['data']],
+                         ['window_synth_01', 'window_synth_02'])
+        for row in result['data']:
+            self.assertEqual(set(row), {'window_token', 'generation', 'layout_revision',
+                                       'title', 'executable', 'rect', 'visible', 'minimized'})
+            self.assertEqual(row['rect'], {'width': 1920, 'height': 1080})
+            self.assertEqual(row['generation'], '3')
+            self.assertEqual(row['layout_revision'], '4')
+            self.assertIs(row['visible'], True)
+            self.assertIs(row['minimized'], False)
+        self.assertEqual(result['data'][0]['title'], result['data'][1]['title'])
+
+    def test_recording_status_happy_path_and_optional_capsule_filter(self):
+        other = deepcopy(self.provider.recordings[0])
+        other['capsule_id'] = 'capsule_synth_02'
+        other['feeds'] = []
+        self.provider.recordings.append(other)
+        result = self.assert_result(self.model.execute('recording_status',
+                                                       {'capsule_id': 'capsule_synth_01'}))
+        self.assertEqual(result['data'], {'capsules': [self.provider.recordings[0]],
+                                         'recorder_available': True})
+        self.assertEqual(self.provider.calls[-1], ('recording_status',
+                         {'capsule_id': 'capsule_synth_01', 'snapshot_id': None}))
+        all_rows = self.assert_result(self.model.execute('recording_status', {}))
+        self.assertEqual(len(all_rows['data']['capsules']), 2)
+
+    def test_recording_unknown_values_remain_null_and_u64_remains_string(self):
+        self.provider.recording_available = None
+        result = self.assert_result(self.model.execute('recording_status', {}))
+        self.assertIsNone(result['data']['recorder_available'])
+        feeds = result['data']['capsules'][0]['feeds']
+        self.assertEqual(feeds[0]['written_frames'], '18446744073709551615')
+        self.assertIsNone(feeds[0]['backpressure'])
+        self.assertIsNone(feeds[1]['written_frames'])
+        self.assertEqual(feeds[1]['backpressure'], '0')
+        self.assertEqual(set(feeds[0]), {'feed_id', 'state', 'written_frames', 'backpressure'})
+
+    def test_slot_display_alias_is_never_resolved_for_status(self):
+        row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
+        result = self.assert_result(self.model.execute('recording_status', {'capsule_id': row['slot']}))
+        self.assertEqual(result['data']['capsules'], [])
+        self.assertEqual(self.provider.calls[-1][1]['capsule_id'], 'slot-1')
+
+    def test_obs_status_happy_path_and_unknown_native_states(self):
+        for available, state, configured in [(True, 'configured', True),
+                                              (False, 'absent', False),
+                                              (True, 'synthetic_unknown', None),
+                                              (None, None, None)]:
+            with self.subTest(state=state):
+                self.provider.obs_available = available
+                self.provider.obs_state = state
+                result = self.assert_result(self.model.execute('obs_status', {}))
+                self.assertEqual(result['data'], {'available': available,
+                                                 'configured': configured, 'state': state})
+
+    def test_messages_between_happy_path_matches_existing_query_order(self):
+        rows = [event(3), event(1), event(2)]
+        expected = queries.execute('recent_messages', {'channel': 'demo_channel'}, iter(rows), 0)
+        result = self.assert_result(self.chat(rows))
+        self.assertEqual(result['data'], expected)
+        self.assertTrue(result['page']['complete'])
+        self.assertEqual(result['scan'], scan())
+        for row in result['data']:
+            self.assertTrue(server._valid_event(row))
+        self.assertEqual(self.provider.calls[-1], ('messages_between',
+                         dict(CHAT, limit=50, snapshot_id=None)))
+
+    def test_messages_by_user_filters_channel_kind_and_exact_login(self):
+        rows = [event(1), event(2, user_login='synthetic_guest_02'),
+                event(3, channel='synthetic_other'), event(4, kind='notice'),
+                event(5, user_login=None), event(6)]
+        args = dict(CHAT, user_login='synthetic_guest_01')
+        result = self.assert_result(self.chat(rows, name='messages_by_user', arguments=args))
+        self.assertEqual([row['message_id'] for row in result['data']],
+                         ['message_synth_01', 'message_synth_06'])
+
+    def test_without_provider_all_six_tools_are_unavailable_even_with_events(self):
+        model = runtime.ReadModel()
+        for name in TOOLS:
+            args = dict(CHAT) if name.startswith('messages_') else {}
+            if name == 'messages_by_user':
+                args['user_login'] = 'synthetic_guest_01'
+            with self.subTest(tool=name):
+                self.assert_error(model.execute(name, args, events=iter([event()]), scan=scan()),
+                                  'provider_unavailable')
+
+    def test_none_and_exception_providers_are_unavailable_without_text_leaks(self):
+        marker = 'SYNTHETIC_PROVIDER_EXCEPTION_DO_NOT_RETURN'
+        for provider in [lambda name, args: None,
+                         mock.Mock(side_effect=RuntimeError(marker))]:
+            with self.subTest(provider_type=type(provider).__name__):
+                result = runtime.ReadModel(provider).execute('list_capsules', {})
+                self.assert_error(result, 'provider_unavailable')
+                self.assertNotIn(marker, json.dumps(result))
+
+    def test_noncallable_provider_is_invalid_snapshot(self):
+        for provider in [0, False, {}, [], 'synthetic_provider']:
+            with self.subTest(provider_type=type(provider).__name__):
+                self.assert_error(runtime.ReadModel(provider).execute('list_capsules', {}),
+                                  'invalid_snapshot')
+
+    def test_malformed_provider_envelope_fails_closed(self):
+        mutations = [lambda value: value.update(extra=True),
+                     lambda value: value.pop('session_id'),
+                     lambda value: value.update(session_id=''),
+                     lambda value: value.update(snapshot_id=''),
+                     lambda value: value.update(authority_epoch=True),
+                     lambda value: value.update(authority_epoch='01'),
+                     lambda value: value.update(data=[]),
+                     lambda value: value.update(page=None)]
+        for mutate in mutations:
+            with self.subTest(case=mutations.index(mutate)):
+                self.provider.transform = mutate
+                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
+                                  'invalid_snapshot')
+        for malformed in [True, [], 'synthetic_snapshot', 1]:
+            self.assert_error(runtime.ReadModel(lambda name, args, v=malformed: v)
+                              .execute('list_capsules', {}), 'invalid_snapshot')
+
+    def test_explicitly_unavailable_native_query_never_returns_empty_success(self):
+        self.provider.transform = lambda value: value.update(data=None)
+        self.assert_error(self.model.execute('list_capsules', {}), 'query_unavailable')
+
+    def test_epoch_change_invalidates_call_and_resets_slot_mapping(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        first = self.assert_result(self.model.execute('list_capsules', {}))
+        initial = {row['capsule_id']: row['slot'] for row in first['data']}
+        self.assertEqual(initial['capsule_synth_02'], 'slot-2')
+        self.provider.epoch = '2'
+        self.provider.capsules = [capsule(2)]
+        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')
+        fresh = self.assert_result(self.model.execute('list_capsules', {}))
+        self.assertEqual(fresh['data'][0]['slot'], 'slot-1')
+
+    def test_native_session_change_also_fails_once_and_resets_aliases(self):
+        self.assert_result(self.model.execute('list_capsules', {}))
+        self.provider.session_id = 'session_synth_native_02'
+        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')
+        self.assertEqual(self.assert_result(self.model.execute('list_capsules', {}))
+                         ['data'][0]['slot'], 'slot-1')
+
+    def test_slots_follow_opaque_identity_and_generation_not_array_position(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        first = self.assert_result(self.model.execute('list_capsules', {}))
+        slots = {row['capsule_id']: row['slot'] for row in first['data']}
+        self.provider.capsules.reverse()
+        again = self.assert_result(self.model.execute('list_capsules', {}))
+        self.assertEqual({row['capsule_id']: row['slot'] for row in again['data']}, slots)
+        self.provider.capsules = [capsule(1, generation='8')]
+        self.provider.snapshot_id = 'snapshot_synth_02'
+        changed = self.assert_result(self.model.execute('list_capsules', {}))
+        self.assertNotEqual(changed['data'][0]['slot'], slots['capsule_synth_01'])
+
+    def test_limit_rejects_bool_float_string_and_out_of_range_before_provider(self):
+        for limit in [True, False, 1.0, '1', 0, -1, 201, None]:
+            with self.subTest(limit=limit):
+                self.assert_error(self.model.execute('list_capsules', {'limit': limit}),
+                                  'invalid_arguments')
+        self.assertEqual(self.provider.calls, [])
+
+    def test_limit_one_and_two_hundred_are_valid(self):
+        for limit in [1, 200]:
+            with self.subTest(limit=limit):
+                self.assert_result(self.model.execute('list_capsules', {'limit': limit}))
+
+    def test_unknown_and_existing_chat_tool_names_are_not_new_tools(self):
+        for name in ['synthetic_unknown', *[item['name'] for item in server.tools()]]:
+            with self.subTest(tool=name):
+                self.assert_error(self.model.execute(name, {}), 'query_unavailable')
+        self.assertEqual(self.provider.calls, [])
+
+    def test_extra_arguments_and_control_flags_fail_before_provider(self):
+        for name, args in [('obs_status', {'limit': 1}),
+                           ('recording_status', {'cursor': None}),
+                           ('list_capsules', {'confirmed': True}),
+                           ('list_capsules', {'folder': 'synthetic_directory'}),
+                           ('messages_between', dict(CHAT, query='synthetic'))]:
+            with self.subTest(tool=name, fields=sorted(args)):
+                self.assert_error(self.model.execute(name, args), 'invalid_arguments')
+        self.assertEqual(self.provider.calls, [])
+
+    def test_wrong_argument_objects_and_missing_required_filters_fail(self):
+        for args in [None, [], True, 'synthetic', 1]:
+            self.assert_error(self.model.execute('obs_status', args), 'invalid_arguments')
+        for key in CHAT:
+            args = dict(CHAT)
+            del args[key]
+            self.assert_error(self.model.execute('messages_between', args), 'invalid_arguments')
+        self.assert_error(self.model.execute('messages_by_user', dict(CHAT)), 'invalid_arguments')
+        self.assertEqual(self.provider.calls, [])
+
+    def test_invalid_filter_identifiers_and_intervals_fail_closed(self):
+        cases = [dict(CHAT, channel='DEMO_CHANNEL'), dict(CHAT, channel='ab'),
+                 dict(CHAT, channel='a' * 26), dict(CHAT, to=FROM),
+                 dict(CHAT, to='2025-11-20T17:00:00Z'),
+                 dict(CHAT, **{'from': '2025-02-30T18:00:00Z'}),
+                 dict(CHAT, **{'from': '2025-11-20T18:00:00+00:00'}),
+                 dict(CHAT, **{'from': '2025-11-20T18:00:00.1234567890Z'}),
+                 dict(CHAT, **{'from': '2025-11-20T18:00:00Z\n'})]
+        for args in cases:
+            self.assert_error(self.model.execute('messages_between', args), 'invalid_arguments')
+        for login in [None, '', 'SyntheticGuest', 'a' * 26, True]:
+            self.assert_error(self.model.execute('messages_by_user', dict(CHAT, user_login=login)),
+                              'invalid_arguments')
+        for identity in ['', '_synthetic', 'a' * 97, True, None]:
+            self.assert_error(self.model.execute('recording_status', {'capsule_id': identity}),
+                              'invalid_arguments')
+        self.assertEqual(self.provider.calls, [])
+
+    def test_invalid_and_oversized_cursor_fails_before_provider(self):
+        for cursor in [True, 1, {}, [], '', 'cursor_synthetic_unknown', 'x' * 513, '\u00e9' * 257]:
+            with self.subTest(cursor_type=type(cursor).__name__):
+                self.assert_error(self.model.execute('list_capsules', {'cursor': cursor}),
+                                  'invalid_arguments')
+        self.assertEqual(self.provider.calls, [])
+        self.assert_result(self.model.execute('list_capsules', {'cursor': None}))
+
+    def test_native_pagination_uses_requested_page_and_stable_snapshot(self):
+        self.provider.windows = [window(index) for index in range(1, 6)]
+        collected = []
+        cursor = None
+        for offset in [0, 2, 4]:
+            result = self.assert_result(self.model.execute('list_candidate_windows',
+                                                           {'limit': 2, 'cursor': cursor}))
+            collected.extend(row['window_token'] for row in result['data'])
+            self.assertEqual(self.provider.calls[-1][1],
+                             {'limit': 2, 'offset': offset,
+                              'snapshot_id': None if offset == 0 else 'snapshot_synth_01'})
+            cursor = result['page']['next_cursor']
+            self.assertEqual(result['page']['complete'], offset == 4)
+            self.assertEqual(cursor is None, offset == 4)
+        self.assertEqual(collected, ['window_synth_%02d' % index for index in range(1, 6)])
+
+    def test_capsule_pagination_keeps_slot_numbers_across_pages(self):
+        self.provider.capsules = [capsule(index) for index in range(1, 4)]
+        first = self.assert_result(self.model.execute('list_capsules', {'limit': 2}))
+        second = self.assert_result(self.model.execute('list_capsules',
+            {'limit': 2, 'cursor': first['page']['next_cursor']}))
+        self.assertEqual([row['slot'] for row in first['data'] + second['data']],
+                         ['slot-1', 'slot-2', 'slot-3'])
+
+    def test_one_partial_native_page_never_implies_exhaustive_window_inventory(self):
+        self.provider.partial = True
+        result = self.assert_result(self.model.execute('list_candidate_windows', {}))
+        self.assertEqual(len(result['data']), 1)
+        self.assertFalse(result['page']['complete'])
+        self.assertIsNone(result['page']['next_cursor'])
+
+    def test_malformed_native_page_metadata_and_extra_rows_fail_closed(self):
+        mutations = [lambda value: value['page'].update(offset=True),
+                     lambda value: value['page'].update(offset=1),
+                     lambda value: value['page'].update(next_offset=0, complete=False),
+                     lambda value: value['page'].update(next_offset=1),
+                     lambda value: value['page'].update(complete=1),
+                     lambda value: value['page'].update(extra=True),
+                     lambda value: value['data']['windows'].append(window(2))]
+        for mutate in mutations:
+            with self.subTest(case=mutations.index(mutate)):
+                self.provider.transform = mutate
+                self.assert_error(runtime.ReadModel(self.provider)
+                                  .execute('list_candidate_windows', {'limit': 1}),
+                                  'invalid_snapshot')
+
+    def test_cross_query_cursor_and_changed_limit_are_rejected(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
+        before = len(self.provider.calls)
+        self.assert_error(self.model.execute('list_candidate_windows', {'limit': 1, 'cursor': cursor}),
+                          'invalid_arguments')
+        self.assert_error(self.model.execute('list_capsules', {'limit': 2, 'cursor': cursor}),
+                          'invalid_arguments')
+        self.assertEqual(len(self.provider.calls), before)
+
+    def test_cursor_cannot_be_reinterpreted_by_another_model(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
+        other = runtime.ReadModel(self.provider)
+        self.assert_error(other.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
+                          'invalid_arguments')
+
+    def test_changed_snapshot_rejects_native_continuation(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        cursor = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))['page']['next_cursor']
+        self.provider.snapshot_id = 'snapshot_synth_02'
+        self.assert_error(self.model.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
+                          'stale_cursor')
+
+    def test_observed_native_snapshot_conflict_invalidates_existing_cursors(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
+        cursor = first['page']['next_cursor']
+        # The adapter has violated immutable snapshot identity. Even a fresh
+        # read must notice contradictory evidence already observed locally.
+        self.provider.capsules[0]['person_label'] = 'synthetic_changed_label'
+        self.assert_error(self.model.execute('list_capsules', {'limit': 1}),
+                          'invalid_snapshot')
+        self.assert_error(self.model.execute('list_capsules', {'limit': 1, 'cursor': cursor}),
+                          'invalid_arguments')
+
+    def test_changed_replayed_native_page_is_stale_and_invalidates_its_cursor(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
+        args = {'limit': 1, 'cursor': first['page']['next_cursor']}
+        self.assert_result(self.model.execute('list_capsules', args))
+        self.provider.capsules[1]['person_label'] = 'synthetic_changed_second_page'
+        self.assert_error(self.model.execute('list_capsules', args), 'stale_cursor')
+        self.assert_error(self.model.execute('list_capsules', args), 'invalid_arguments')
+
+    def test_native_identity_bookkeeping_limit_preserves_existing_alias(self):
+        self.provider.capsules = [capsule(1), capsule(2)]
+        with mock.patch.object(runtime, '_IDENTITY_CAP', 3):
+            first = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
+            self.assertEqual(first['data'][0]['slot'], 'slot-1')
+            self.assert_error(self.model.execute('list_capsules',
+                              {'limit': 1, 'cursor': first['page']['next_cursor']}),
+                              'query_unavailable')
+            again = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
+            self.assertEqual(again['data'][0]['slot'], 'slot-1')
+
+    def test_slot_capacity_never_recycles_existing_generation_aliases(self):
+        with mock.patch.object(runtime, '_IDENTITY_CAP', 3):
+            for index, generation in enumerate(['7', '8', '9'], 1):
+                self.provider.snapshot_id = 'snapshot_synth_capacity_%d' % index
+                self.provider.capsules = [capsule(1, generation)]
+                row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
+                self.assertEqual(row['slot'], 'slot-%d' % index)
+            self.provider.snapshot_id = 'snapshot_synth_capacity_4'
+            self.provider.capsules = [capsule(1, '10')]
+            self.assert_error(self.model.execute('list_capsules', {}), 'query_unavailable')
+            self.provider.snapshot_id = 'snapshot_synth_capacity_5'
+            self.provider.capsules = [capsule(1, '7')]
+            row = self.assert_result(self.model.execute('list_capsules', {}))['data'][0]
+            self.assertEqual(row['slot'], 'slot-1')
+
+    def test_chat_pagination_selects_newest_then_moves_to_older_rows(self):
+        rows = [event(index) for index in [2, 5, 1, 4, 3]]
+        args = dict(CHAT, limit=2)
+        first = self.assert_result(self.chat(rows, arguments=args))
+        self.assertEqual(first['data'], queries.execute('recent_messages',
+                         {'channel': 'demo_channel', 'limit': 2}, iter(rows), 0))
+        self.assertFalse(first['page']['complete'])
+        args['cursor'] = first['page']['next_cursor']
+        second = self.assert_result(self.chat(rows, arguments=args))
+        self.assertEqual([row['message_id'] for row in second['data']],
+                         ['message_synth_02', 'message_synth_03'])
+        args['cursor'] = second['page']['next_cursor']
+        third = self.assert_result(self.chat(rows, arguments=args))
+        self.assertEqual([row['message_id'] for row in third['data']], ['message_synth_01'])
+        self.assertTrue(third['page']['complete'])
+        self.assertIsNone(third['page']['next_cursor'])
+
+    def test_chat_cursor_is_bound_to_tool_user_channel_and_interval(self):
+        rows = [event(1), event(2)]
+        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
+        cursor = first['page']['next_cursor']
+        cases = [('messages_by_user', dict(CHAT, limit=1, cursor=cursor,
+                                           user_login='synthetic_guest_01')),
+                 ('messages_between', dict(CHAT, limit=1, cursor=cursor, channel='synthetic_other')),
+                 ('messages_between', dict(CHAT, limit=1, cursor=cursor, to='2025-11-20T18:30:00Z'))]
+        for name, args in cases:
+            self.assert_error(self.chat(rows, name=name, arguments=args), 'invalid_arguments')
+        user_args = dict(CHAT, user_login='synthetic_guest_01', limit=1)
+        user_cursor = self.assert_result(self.chat(rows, name='messages_by_user',
+                                                 arguments=user_args))['page']['next_cursor']
+        self.assert_error(self.chat(rows, name='messages_by_user',
+                          arguments=dict(user_args, user_login='synthetic_guest_02', cursor=user_cursor)),
+                          'invalid_arguments')
+
+    def test_chat_content_change_with_same_ordering_keys_is_stale(self):
+        rows = [event(1), event(2)]
+        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
+        changed = deepcopy(rows)
+        changed[0]['text'] = 'Synthetic altered text with unchanged identity'
+        self.assert_error(self.chat(changed, arguments=dict(CHAT, limit=1,
+                          cursor=first['page']['next_cursor'])), 'stale_cursor')
+
+    def test_chat_scan_summary_change_is_stale(self):
+        rows = [event(1), event(2)]
+        first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
+        self.assert_error(self.chat(rows, arguments=dict(CHAT, limit=1,
+                          cursor=first['page']['next_cursor']), stats=scan(bytes_scanned=1235)),
+                          'stale_cursor')
+
+    def test_half_open_interval_uses_all_nine_fraction_digits(self):
+        timestamps = ['2025-11-20T17:59:59.999999999Z', FROM,
+                      '2025-11-20T18:59:59.999999999Z', TO]
+        rows = []
+        for index, utc in enumerate(timestamps, 1):
+            row = event(index)
+            row['received_at']['utc'] = utc
+            rows.append(row)
+        result = self.assert_result(self.chat(rows))
+        self.assertEqual([row['message_id'] for row in result['data']],
+                         ['message_synth_02', 'message_synth_03'])
+        fine_args = dict(CHAT, **{'from': '2025-11-20T18:00:00.000000001Z',
+                                  'to': '2025-11-20T18:00:00.000000003Z'})
+        fine_rows = []
+        for index in range(4):
+            row = event(index)
+            row['received_at']['utc'] = '2025-11-20T18:00:00.%09dZ' % index
+            fine_rows.append(row)
+        fine = self.assert_result(self.chat(fine_rows, arguments=fine_args))
+        self.assertEqual([row['message_id'] for row in fine['data']],
+                         ['message_synth_01', 'message_synth_02'])
+
+    def test_cross_session_ties_match_existing_total_order_for_every_permutation(self):
+        first = event(1, message_id='synthetic_z')
+        second = event(2, message_id='synthetic_a')
+        third = event(1, message_id='synthetic_m', session_id='session_synth_chat_02')
+        for row in (first, second, third):
+            row['received_at']['utc'] = FROM
+        for rows in itertools.permutations([first, second, third]):
+            with self.subTest(order=[row['message_id'] for row in rows]):
+                result = self.assert_result(self.chat(rows))
+                expected = queries.execute('recent_messages', {'channel': 'demo_channel'}, iter(rows), 0)
+                self.assertEqual(result['data'], expected)
+
+    def test_exact_order_key_ties_and_duplicate_events_survive_all_pages(self):
+        rows = [event(1, text='Synthetic tie %d' % index) for index in range(5)]
+        rows.append(deepcopy(rows[-1]))
+        args = dict(CHAT, limit=2)
+        pages = []
+        for expected_complete in [False, False, True]:
+            page = self.assert_result(self.chat(rows, arguments=args))
+            pages.append(page['data'])
+            self.assertEqual(page['page']['complete'], expected_complete)
+            args['cursor'] = page['page']['next_cursor']
+        self.assertEqual([row for page in reversed(pages) for row in page], rows)
+
+    def test_limited_scan_preserves_partial_data_and_exact_statistics(self):
+        stats = scan(corrupt_lines=2, truncated_lines=1, bytes_scanned=33554433,
+                     files_scanned=4, scan_limited=True)
+        result = self.assert_result(self.chat([event()], stats=stats), error='scan_limited')
+        self.assertEqual(result['data'], [event()])
+        self.assertFalse(result['page']['complete'])
+        self.assertEqual(result['scan'], stats)
+
+    def test_scan_statistics_are_observed_after_single_pass_iteration(self):
+        stats = scan(bytes_scanned=0, files_scanned=0)
+        visits = []
+        def records():
+            visits.append('started')
+            yield event()
+            stats.update(bytes_scanned=33554433, files_scanned=2, scan_limited=True)
+            visits.append('exhausted')
+        result = self.assert_result(self.model.execute('messages_between', dict(CHAT),
+                                                       events=records(), scan=stats), error='scan_limited')
+        self.assertEqual(visits, ['started', 'exhausted'])
+        self.assertEqual(result['scan'], stats)
+        stats['files_scanned'] = 3
+        self.assertEqual(result['scan']['files_scanned'], 2)
+        self.assertFalse(result['page']['complete'])
+
+    def test_corrupt_or_truncated_records_preclude_completeness(self):
+        for stats in [scan(corrupt_lines=1), scan(truncated_lines=1)]:
+            result = self.assert_result(self.chat([event()], stats=stats))
+            self.assertFalse(result['page']['complete'])
+            self.assertEqual(result['scan'], stats)
+
+    def test_missing_scan_is_unknown_but_empty_validated_scan_can_be_complete(self):
+        result = self.assert_result(self.model.execute('messages_between', dict(CHAT),
+                                                       events=iter([event()])))
+        self.assertIsNone(result['scan'])
+        self.assertFalse(result['page']['complete'])
+        empty = self.assert_result(self.chat([]))
+        self.assertEqual(empty['data'], [])
+        self.assertTrue(empty['page']['complete'])
+
+    def test_missing_events_is_query_unavailable(self):
+        self.assert_error(self.model.execute('messages_between', dict(CHAT), scan=scan()),
+                          'query_unavailable')
+
+    def test_malformed_scan_summary_is_invalid_snapshot(self):
+        cases = [scan(corrupt_lines=True), scan(bytes_scanned=-1), scan(files_scanned='1'),
+                 scan(scan_limited=1), scan(extra=0), {}, [], True]
+        for stats in cases:
+            with self.subTest(stats_type=type(stats).__name__):
+                self.assert_error(self.model.execute('messages_between', dict(CHAT),
+                                  events=iter([event()]), scan=stats), 'invalid_snapshot')
+
+    def test_impossible_datareader_scan_counters_fail_closed(self):
+        cases = [scan(bytes_scanned=server.MAX_SCAN_BYTES + 1, scan_limited=False),
+                 scan(files_scanned=server.MAX_FILES + 1),
+                 scan(files_scanned=server.MAX_FILES + 1, scan_limited=True),
+                 scan(bytes_scanned=server.MAX_SCAN_BYTES + server.MAX_EVENT_LINE + 2,
+                      scan_limited=True)]
+        for stats in cases:
+            with self.subTest(bytes_scanned=stats['bytes_scanned'],
+                              files_scanned=stats['files_scanned']):
+                self.assert_error(self.chat([event()], stats=stats), 'invalid_snapshot')
+        bounded_overrun = scan(bytes_scanned=server.MAX_SCAN_BYTES + server.MAX_EVENT_LINE + 1,
+                               files_scanned=server.MAX_FILES, scan_limited=True)
+        result = self.assert_result(self.chat([event()], stats=bounded_overrun), error='scan_limited')
+        self.assertEqual(result['scan'], bounded_overrun)
+        self.assertFalse(result['page']['complete'])
+
+    def test_tampered_events_fail_without_partial_success(self):
+        malformed = []
+        for field, value in [('extra', 'synthetic'), ('schema', 'synthetic.invalid'),
+                             ('sent_at', True), ('kind', 'synthetic_unknown')]:
+            row = event()
+            row[field] = value
+            malformed.append(row)
+        for clock in [True, 1, 1.0, '01', '-1', '18446744073709551616']:
+            row = event()
+            row['received_at']['monotonic_ns'] = clock
+            malformed.append(row)
+        invalid_date = event()
+        invalid_date['received_at']['utc'] = '2025-02-30T18:00:00Z'
+        malformed.append(invalid_date)
+        for row in malformed:
+            self.assert_error(self.chat([event(2), row]), 'invalid_snapshot')
+
+    def test_iterator_failure_is_closed_and_does_not_return_previous_rows(self):
+        def records():
+            yield event()
+            raise RuntimeError('SYNTHETIC_ITERATOR_EXCEPTION_DO_NOT_RETURN')
+        result = self.model.execute('messages_between', dict(CHAT), events=records(), scan=scan())
+        self.assert_error(result, 'invalid_snapshot')
+        self.assertNotIn('SYNTHETIC_ITERATOR_EXCEPTION', json.dumps(result))
+
+    def test_oversized_ascii_and_utf8_results_are_rejected(self):
+        for rows in [[event(index, text='s' * 16000) for index in range(6)],
+                     [event(index, text='\U0001f600' * 16000) for index in range(2)]]:
+            with self.subTest(first_text_bytes=len(rows[0]['text'].encode('utf-8'))):
+                self.assert_error(self.chat(rows), 'response_too_large')
+        self.assertEqual(server.MAX_RESPONSE, 65536)
+
+    def test_capsule_and_feed_objects_reject_extras_missing_labels_and_aliases(self):
+        mutations = [lambda row: row.update(extra=True),
+                     lambda row: row.pop('person_label'),
+                     lambda row: row.update(person_label=None),
+                     lambda row: row.update(capsule_id='other_synthetic_id'),
+                     lambda row: row.update(generation=True),
+                     lambda row: row['feeds']['feed_synth_game_01'].update(role='microphone'),
+                     lambda row: row['feeds']['feed_synth_game_01'].update(feed_id='different_synthetic_id'),
+                     lambda row: row['feeds']['feed_synth_game_01'].update(extra=True),
+                     lambda row: row['feeds']['feed_synth_camera_01'].update(role='game')]
+        for mutate in mutations:
+            with self.subTest(case=mutations.index(mutate)):
+                self.provider.capsules = [capsule()]
+                self.provider.transform = lambda value, change=mutate: change(
+                    value['data']['capsules']['capsule_synth_01'])
+                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
+                                  'invalid_snapshot')
+
+    def test_duplicate_feed_identity_across_capsules_is_rejected(self):
+        first, second = capsule(1), capsule(2)
+        second['feeds'] = deepcopy(first['feeds'])
+        self.provider.capsules = [first, second]
+        self.assert_error(self.model.execute('list_capsules', {}), 'invalid_snapshot')
+
+    def test_duplicate_feed_identity_across_native_pages_is_rejected(self):
+        first, second = capsule(1), capsule(2)
+        second['feeds'] = deepcopy(first['feeds'])
+        self.provider.capsules = [first, second]
+        page = self.assert_result(self.model.execute('list_capsules', {'limit': 1}))
+        self.assert_error(self.model.execute('list_capsules',
+                          {'limit': 1, 'cursor': page['page']['next_cursor']}),
+                          'invalid_snapshot')
+
+    def test_noncanonical_native_clocks_are_never_coerced(self):
+        for value in [True, 7, 7.0, '00', '01', '+1', '-1', '1.0', ' 1',
+                      '18446744073709551616', '9' * 21, None]:
+            with self.subTest(value=value):
+                self.provider.capsules = [capsule(generation=value)]
+                self.assert_error(runtime.ReadModel(self.provider).execute('list_capsules', {}),
+                                  'invalid_snapshot')
+
+    def test_malformed_window_shapes_and_duplicate_tokens_fail_closed(self):
+        mutations = [lambda row: row.update(handle=''),
+                     lambda row: row.update(visible=1),
+                     lambda row: row.update(minimized=None),
+                     lambda row: row.update(layout_revision='01'),
+                     lambda row: row.update(process_id=True),
+                     lambda row: row['rect'].update(width=True),
+                     lambda row: row['rect'].update(height=-1),
+                     lambda row: row['rect'].update(extra=1),
+                     lambda row: row.update(extra=True)]
+        for mutate in mutations:
+            self.provider.windows = [window()]
+            mutate(self.provider.windows[0])
+            self.assert_error(runtime.ReadModel(self.provider).execute('list_candidate_windows', {}),
+                              'invalid_snapshot')
+        self.provider.windows = [window(), window()]
+        self.assert_error(runtime.ReadModel(self.provider).execute('list_candidate_windows', {}),
+                          'invalid_snapshot')
+
+    def test_malformed_recording_fields_and_hidden_filtered_row_are_rejected(self):
+        mutations = [lambda data: data.update(recording_available='true'),
+                     lambda data: data['recordings'][0].update(extra=True),
+                     lambda data: data['recordings'][0]['feeds'][0].update(written_frames=True),
+                     lambda data: data['recordings'][0]['feeds'][0].update(backpressure='01')]
+        for mutate in mutations:
+            self.provider.transform = lambda value, change=mutate: change(value['data'])
+            self.assert_error(runtime.ReadModel(self.provider).execute('recording_status', {}),
+                              'invalid_snapshot')
+        self.provider.transform = None
+        hidden = deepcopy(self.provider.recordings[0])
+        hidden['capsule_id'] = 'capsule_synth_02'
+        hidden['feeds'][0]['written_frames'] = True
+        self.provider.recordings.append(hidden)
+        self.assert_error(self.model.execute('recording_status', {'capsule_id': 'capsule_synth_01'}),
+                          'invalid_snapshot')
+
+    def test_malformed_obs_fields_are_rejected_without_invented_configuration(self):
+        for mutate in [lambda data: data.update(obs_available=1),
+                       lambda data: data['services'].update(obs=True),
+                       lambda data: data.update(configured=True),
+                       lambda data: data['services'].update(extra=True)]:
+            self.provider.transform = lambda value, change=mutate: change(value['data'])
+            self.assert_error(runtime.ReadModel(self.provider).execute('obs_status', {}),
+                              'invalid_snapshot')
+
+    def test_returned_data_cannot_mutate_future_provider_state(self):
+        result = self.assert_result(self.model.execute('recording_status', {}))
+        result['data']['capsules'][0]['feeds'][0]['written_frames'] = '0'
+        again = self.assert_result(self.model.execute('recording_status', {}))
+        self.assertEqual(again['data']['capsules'][0]['feeds'][0]['written_frames'],
+                         '18446744073709551615')
+
+    def test_caller_arguments_remain_unchanged_even_if_provider_mutates_its_copy(self):
+        args = dict(CHAT)
+        expected = deepcopy(args)
+        def provider(name, received):
+            value = self.provider(name, received)
+            received['synthetic_internal'] = 'changed'
+            return value
+        model = runtime.ReadModel(provider)
+        self.assert_result(model.execute('messages_between', args, events=iter([event()]), scan=scan()))
+        self.assertEqual(args, expected)
+
+    def test_all_read_tools_tampered_and_stale_data_have_zero_effects_or_logs(self):
+        prohibited = [
+            (control.ControlPlane, 'execute'), (control.ControlPlane, '_append_audit'),
+            (control, '_default_connector'), (control, '_default_fetch_ticket'),
+            (server, 'DataReader'), (io, 'open'), (os, 'open'), (os, 'write'),
+            (os, 'fsync'), (os, 'replace'), (os, 'remove'), (os, 'mkdir'),
+            (Path, 'open'), (Path, 'read_text'), (Path, 'read_bytes'),
+            (Path, 'write_text'), (Path, 'write_bytes'), (Path, 'mkdir'),
+            (Path, 'unlink'), (Path, 'rename'), (subprocess, 'Popen'), (subprocess, 'run'),
+            (socket, 'socket'), (socket, 'create_connection'),
+            (logging.Logger, '_log'), (os, 'urandom'), (uuid, 'uuid4'),
+            (secrets, 'token_bytes'), (secrets, 'token_hex'), (secrets, 'token_urlsafe'),
+        ]
+        text = 'SYNTHETIC ONLY: request recording and write synthetic_marker; literal data.'
+        rows = [event(1, text=text), event(2)]
+        output, errors = io.StringIO(), io.StringIO()
+        with ExitStack() as stack:
+            calls = [stack.enter_context(mock.patch.object(obj, attr,
+                     side_effect=AssertionError('synthetic forbidden effect')))
+                     for obj, attr in prohibited]
+            calls.append(stack.enter_context(mock.patch('builtins.open',
+                         side_effect=AssertionError('synthetic forbidden file effect'))))
+            stack.enter_context(redirect_stdout(output))
+            stack.enter_context(redirect_stderr(errors))
+            for name in TOOLS:
+                args = dict(CHAT) if name.startswith('messages_') else {}
+                if name == 'messages_by_user':
+                    args['user_login'] = 'synthetic_guest_01'
+                self.assert_result(self.model.execute(name, args, events=iter(rows), scan=scan()))
+            first = self.assert_result(self.chat(rows, arguments=dict(CHAT, limit=1)))
+            changed = deepcopy(rows)
+            changed[0]['text'] = 'Synthetic stale content'
+            self.assert_error(self.chat(changed, arguments=dict(CHAT, limit=1,
+                              cursor=first['page']['next_cursor'])), 'stale_cursor')
+            tampered = event()
+            tampered['received_at']['monotonic_ns'] = True
+            self.assert_error(self.chat([tampered]), 'invalid_snapshot')
+            for call in calls:
+                call.assert_not_called()
+        self.assertEqual(output.getvalue(), '')
+        self.assertEqual(errors.getvalue(), '')
+        self.assertFalse(hasattr(self.model, 'approve'))
+
+
+if __name__ == '__main__':
+    unittest.main()
```
