# PRO-07 serial MCP registration and dispatch

Scope: `unverified_proposal`.

## Assumptions

1. Serial integration provides cafe.mcp_read_model, cafe.mcp_statistics and cafe.demo_guide with the frozen signatures. They are absent from the immutable base; contract doubles do not accept real snapshots, cursor algorithms, aggregates or live effects.
2. The explicitly allowed tests/integration/test_mcp_bridge_dispatch.py is absent from the base and is added at that path.
3. The exact fifteen-tool listing applies even with explicit control. Existing native control dispatch stays opt-in; the two old six/ten-tool assertions in tests/integration/test_mcp_control.py require an integration update outside this write set.
4. Guide success and guide argument/resource errors remain flat. Missing, malformed or oversized guide dependencies use the typed unavailable ReadResult exception instead of fabricating guide content.
5. Projected generation and layout_revision values use canonical decimal clock strings. Native provider validation belongs to ReadModel; all live markers remain pending.

## Complete product and test diff against the immutable base

```diff
diff --git a/connectors/cafe/mcp_server.py b/connectors/cafe/mcp_server.py
--- a/connectors/cafe/mcp_server.py
+++ b/connectors/cafe/mcp_server.py
@@ -2,6 +2,7 @@
 import argparse
 from datetime import datetime
 from decimal import Decimal
+import importlib
 import json
 import os
 from pathlib import Path
@@ -45,6 +46,29 @@
 _CONTROL_TOOL_NAMES = frozenset(_CONTROL_ARGUMENTS)
 _CODES = {-32700: 'Parse error', -32600: 'Invalid Request',
           -32601: 'Method not found', -32602: 'Invalid params'}
+_READ_ARGUMENTS = {
+    'list_capsules': {'limit': 'page_limit', 'cursor': 'cursor'},
+    'list_candidate_windows': {'limit': 'page_limit', 'cursor': 'cursor'},
+    'recording_status': {'capsule_id': 'control_id'},
+    'obs_status': {},
+    'messages_by_user': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
+                         'limit': 'page_limit', 'cursor': 'cursor'},
+    'messages_between': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
+                         'limit': 'page_limit', 'cursor': 'cursor'},
+    'chat_statistics': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
+                        'bucket_seconds': 'bucket', 'top_limit': 'top_limit'},
+    'chat_activity': {'channel': 'channel', 'from': 'utc', 'to': 'utc',
+                      'limit': 'page_limit', 'cursor': 'cursor'},
+    'demo_guide': {'step': 'step'},
+}
+_RUNTIME_READS = frozenset({'list_capsules', 'list_candidate_windows',
+                          'recording_status', 'obs_status'})
+_CHAT_READS = frozenset({'messages_by_user', 'messages_between'})
+_STATISTICS_READS = frozenset({'chat_statistics', 'chat_activity'})
+_READ_ERRORS = frozenset({'invalid_arguments', 'provider_unavailable',
+                         'invalid_snapshot', 'stale_cursor', 'query_unavailable',
+                         'scan_limited', 'response_too_large'})
+_GUIDE_URI = 'cafe://demo/guide'
 
 
 class DataError(ValueError):
@@ -324,14 +348,425 @@
     return now_ns
 
 
+def _read_schema(kind):
+    if kind == 'cursor':
+        return {'type': ['string', 'null'], 'maxLength': 512}
+    if kind == 'control_id':
+        return {'type': 'string', 'pattern': '^' + _CONTROL_IDENTIFIER + '$',
+                'minLength': 1, 'maxLength': 96}
+    if kind in {'page_limit', 'top_limit', 'step'}:
+        result = {'type': 'integer', 'minimum': 1,
+                  'maximum': {'page_limit': 200, 'top_limit': 50, 'step': 7}[kind]}
+        if kind == 'page_limit':
+            result['default'] = 50
+        return result
+    result = _argument_schema(kind)
+    if kind == 'bucket':
+        result['default'] = 60
+    return result
+
+
+def _read_definitions():
+    # The frozen registry remains discoverable before serial dependencies land.
+    return [{'name': name, 'description': 'Read-only typed query: ' + name,
+             'inputSchema': {'type': 'object', 'additionalProperties': False,
+                             'properties': {key: _read_schema(kind) for key, kind in fields.items()},
+                             'required': ['channel', 'from', 'to'] if 'channel' in fields else []},
+             'annotations': {'readOnlyHint': True, 'destructiveHint': False,
+                             'idempotentHint': True, 'openWorldHint': False}}
+            for name, fields in _READ_ARGUMENTS.items()]
+
+
+def _optional_module(name):
+    try:
+        return importlib.import_module('.' + name, __package__)
+    except Exception:
+        # Absence or an unusable optional integration cannot disable old queries.
+        return None
+
+
+def _cursor(value):
+    try:
+        return value is None or (type(value) is str and 0 < len(value.encode('utf-8')) <= 512)
+    except UnicodeError:
+        return False
+
+
+def _read_instant(value):
+    # Validation has already excluded offsets and invalid calendar dates.
+    moment = datetime.fromisoformat(value[:19])
+    seconds = ((moment.toordinal() * 24 + moment.hour) * 60 + moment.minute) * 60 + moment.second
+    fraction = value[20:-1] if value[19:20] == '.' else ''
+    return seconds * 1000000000 + int(fraction.ljust(9, '0') or '0')
+
+
+def _validate_read_arguments(name, arguments):
+    fields = _READ_ARGUMENTS[name]
+    required = {'channel', 'from', 'to'} if 'channel' in fields else set()
+    if type(arguments) is not dict or set(arguments) - set(fields) or not required <= set(arguments):
+        raise ValueError('invalid_arguments')
+    for key, value in arguments.items():
+        kind = fields[key]
+        if kind == 'cursor':
+            valid = _cursor(value)
+        elif kind == 'channel':
+            valid = type(value) is str and bool(re.fullmatch(r'[a-z0-9_]{3,25}', value))
+        elif kind == 'control_id':
+            valid = type(value) is str and bool(re.fullmatch(_CONTROL_IDENTIFIER, value))
+        elif kind == 'utc':
+            valid = _utc(value)
+        else:
+            valid = type(value) is int and 1 <= value <= {'page_limit': 200, 'bucket': 3600,
+                                                         'top_limit': 50, 'step': 7}[kind]
+        if not valid:
+            raise ValueError('invalid_arguments')
+    if 'from' in arguments:
+        duration = _read_instant(arguments['to']) - _read_instant(arguments['from'])
+        if duration <= 0:
+            raise ValueError('invalid_arguments')
+        if name in _STATISTICS_READS:
+            bucket = arguments.get('bucket_seconds', 60) * 1000000000
+            if (duration + bucket - 1) // bucket > 1000:
+                raise ValueError('invalid_arguments')
+    return dict(arguments)
+
+
+def _unavailable(error, scan=None):
+    return {'schema': 'cafe.mcp_read.v1', 'availability': 'unavailable', 'data': None,
+            'page': {'next_cursor': None, 'complete': False}, 'scan': scan, 'error': error}
+
+
+def _keys(value, keys):
+    return type(value) is dict and set(value) == set(keys.split())
+
+
+def _identifier(value):
+    return type(value) is str and bool(re.fullmatch(_CONTROL_IDENTIFIER, value))
+
+
+def _runtime_projection(name, data):
+    """Check the frozen projected result, never a native snapshot transport."""
+    def sequence(rows, check):
+        return type(rows) is list and all(check(row) for row in rows)
+
+    def feed(row):
+        return (_keys(row, 'feed_id role generation') and _identifier(row['feed_id']) and
+                type(row['role']) is str and _canonical_clock(row['generation']))
+
+    def capsule(row):
+        return (_keys(row, 'capsule_id slot person_label generation feeds') and
+                _identifier(row['capsule_id']) and type(row['slot']) is str and
+                bool(re.fullmatch(r'slot-[0-9]+', row['slot'])) and type(row['person_label']) is str and
+                _canonical_clock(row['generation']) and sequence(row['feeds'], feed))
+
+    def window(row):
+        return (_keys(row, 'window_token generation layout_revision title executable rect visible minimized') and
+                _identifier(row['window_token']) and _canonical_clock(row['generation']) and
+                _canonical_clock(row['layout_revision']) and type(row['title']) is str and
+                type(row['executable']) is str and _keys(row['rect'], 'width height') and
+                all(type(value) is int and value >= 0 for value in row['rect'].values()) and
+                type(row['visible']) is bool and type(row['minimized']) is bool)
+
+    def recording_feed(row):
+        return (_keys(row, 'feed_id state written_frames backpressure') and _identifier(row['feed_id']) and
+                type(row['state']) is str and
+                all(value is None or _canonical_clock(value)
+                    for value in (row['written_frames'], row['backpressure'])))
+
+    def recording_capsule(row):
+        return (_keys(row, 'capsule_id state feeds') and _identifier(row['capsule_id']) and
+                type(row['state']) is str and sequence(row['feeds'], recording_feed))
+
+    if name == 'list_capsules':
+        return sequence(data, capsule)
+    if name == 'list_candidate_windows':
+        return sequence(data, window)
+    if name == 'recording_status':
+        return (_keys(data, 'capsules recorder_available') and
+                (data['recorder_available'] is None or type(data['recorder_available']) is bool) and
+                sequence(data['capsules'], recording_capsule))
+    return (_keys(data, 'available configured state') and
+            all(value is None or type(value) is bool for value in (data['available'], data['configured'])) and
+            (data['state'] is None or type(data['state']) is str))
+
+
+def _statistics_projection(data):
+    def count(value):
+        return type(value) is int and value >= 0
+
+    def buckets(rows):
+        return (type(rows) is list and len(rows) <= 1000 and
+                all(_keys(row, 'start count') and _utc(row['start']) and count(row['count']) for row in rows))
+
+    return (_keys(data, 'message_count unique_chatters messages_per_bucket top_words peaks top_emotes emotes_status complete') and
+            count(data['message_count']) and count(data['unique_chatters']) and
+            buckets(data['messages_per_bucket']) and buckets(data['peaks']) and
+            type(data['top_words']) is list and len(data['top_words']) <= 50 and
+            all(_keys(row, 'word count') and type(row['word']) is str and row['word'] and count(row['count'])
+                for row in data['top_words']) and data['top_emotes'] is None and
+            data['emotes_status'] == 'metadata_unavailable' and type(data['complete']) is bool)
+
+
+def _chat_projection(value):
+    # Grouping belongs to the read module; embedded event rows retain the
+    # original closed event schema and canonical clock rules.
+    if type(value) is dict:
+        if value.get('schema') == 'cafe.chat_event.v1':
+            return _valid_event(value)
+        return all(_chat_projection(child) for child in value.values())
+    if type(value) is list:
+        return all(_chat_projection(child) for child in value)
+    return True
+
+
+def _checked_read_result(name, value, scan, arguments):
+    # Detach results from dependency-owned mutable state and reject non-JSON data.
+    value = _loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
+    if (not _keys(value, 'schema availability data page scan error') or
+            value['schema'] != 'cafe.mcp_read.v1' or
+            value['availability'] not in ('available', 'unavailable') or
+            value['error'] is not None and value['error'] not in _READ_ERRORS or
+            not _keys(value['page'], 'next_cursor complete') or
+            not _cursor(value['page']['next_cursor']) or type(value['page']['complete']) is not bool or
+            value['data'] is not None and type(value['data']) not in (dict, list) or
+            value['scan'] is not None and type(value['scan']) is not dict):
+        raise ValueError('invalid_snapshot')
+    if value['page']['complete'] and value['page']['next_cursor'] is not None:
+        raise ValueError('invalid_snapshot')
+    if value['error'] not in (None, 'scan_limited'):
+        if (value['availability'] != 'unavailable' or value['data'] is not None or
+                value['page'] != {'next_cursor': None, 'complete': False}):
+            raise ValueError('invalid_snapshot')
+    elif value['error'] is None and (value['availability'] != 'available' or value['data'] is None):
+        raise ValueError('invalid_snapshot')
+    if value['data'] is not None and name in _RUNTIME_READS and not _runtime_projection(name, value['data']):
+        raise ValueError('invalid_snapshot')
+    if (type(value['data']) is list and 'limit' in _READ_ARGUMENTS[name] and
+            len(value['data']) > arguments.get('limit', 50)):
+        raise ValueError('invalid_snapshot')
+    if value['data'] is not None and name in _CHAT_READS and not _chat_projection(value['data']):
+        raise ValueError('invalid_snapshot')
+    if value['data'] is not None and name in _STATISTICS_READS:
+        if name == 'chat_statistics' or type(value['data']) is dict:
+            if not _statistics_projection(value['data']):
+                raise ValueError('invalid_snapshot')
+            if (name == 'chat_activity' and
+                    len(value['data']['messages_per_bucket']) > arguments.get('limit', 50) or
+                    name == 'chat_statistics' and
+                    len(value['data']['top_words']) > arguments.get('top_limit', 50)):
+                raise ValueError('invalid_snapshot')
+        elif (len(value['data']) > arguments.get('limit', 50) or
+              any(not _keys(row, 'start count') or not _utc(row['start']) or
+                  type(row['count']) is not int or row['count'] < 0 for row in value['data'])):
+            raise ValueError('invalid_snapshot')
+    value['scan'] = scan
+    if (value['error'] == 'scan_limited' or
+            scan is not None and (scan['scan_limited'] or scan['corrupt_lines'] or scan['truncated_lines'])):
+        value['page'] = {'next_cursor': None, 'complete': False}
+        if type(value['data']) is dict and 'complete' in value['data']:
+            value['data']['complete'] = False
+        if value['error'] is None or value['error'] == 'scan_limited':
+            value['error'] = 'scan_limited'
+    return value
+
+
+def _guide_error(error):
+    return {'schema': 'cafe.demo_guide.v1', 'steps': [],
+            'control_mode': 'human_confirmed', 'error': error}
+
+
+def _checked_guide(value, step=None):
+    value = _loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
+    if (not _keys(value, 'schema steps control_mode error') or
+            value['schema'] != 'cafe.demo_guide.v1' or value['control_mode'] != 'human_confirmed' or
+            value['error'] not in (None, 'invalid_arguments', 'unknown_resource') or type(value['steps']) is not list):
+        raise ValueError('invalid_snapshot')
+    if value['error'] is not None:
+        if value['steps']:
+            raise ValueError('invalid_snapshot')
+        return value
+    expected = [step] if step is not None else list(range(1, 8))
+    if [row.get('id') if type(row) is dict else None for row in value['steps']] != expected:
+        raise ValueError('invalid_snapshot')
+    for row in value['steps']:
+        if (not _keys(row, 'id instruction expected evidence_required depends_on live_status') or
+                type(row['id']) is not int or row['live_status'] != 'pending' or
+                any(type(row[key]) is not str or not row[key] for key in ('instruction', 'expected', 'evidence_required')) or
+                type(row['depends_on']) is not list or
+                any(type(item) is not int or not 1 <= item < row['id'] for item in row['depends_on'])):
+            raise ValueError('invalid_snapshot')
+    return value
+
+
+def _read_response(identity, value):
+    payload = {'data': value, 'scan': value['scan']}
+    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
+    return _result(identity, {'content': [{'type': 'text', 'text': text}],
+                              'structuredContent': payload, 'isError': value['error'] is not None})
+
+
+def _bounded_read_response(identity, value):
+    response = _read_response(identity, value)
+    if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
+        return _read_response(identity, _unavailable('response_too_large', value['scan']))
+    return response
+
+
+def _dependency_definitions(module, names):
+    if module is None:
+        return None
+    try:
+        definitions = _loads(json.dumps(module.tool_definitions(), allow_nan=False))
+        if type(definitions) is not list or len(definitions) != len(names):
+            return None
+        if {item['name'] for item in definitions} != names:
+            return None
+        for item in definitions:
+            schema = item['inputSchema']
+            fields = _READ_ARGUMENTS[item['name']]
+            required = {'channel', 'from', 'to'} if 'channel' in fields else set()
+            if (schema.get('type') != 'object' or schema.get('additionalProperties') is not False or
+                    type(schema.get('properties')) is not dict or set(schema['properties']) != set(fields) or
+                    type(schema.get('required')) is not list or set(schema['required']) != required or
+                    item['annotations'].get('readOnlyHint') is not True or
+                    item['annotations'].get('destructiveHint') is not False):
+                return None
+        return definitions
+    except Exception:
+        return None
+
+
 class Server:
-    def __init__(self, data, *, query=None, clock=None, control=None):
+    def __init__(self, data, *, query=None, clock=None, control=None, readonly_provider=None):
         self.data = _canonical_root(data)
         self.state = 'new'; self.clock = clock; self.control = control
         if query is None:
             from .mcp_queries import execute
             query = execute
         self.query = query
+        self.readonly_provider = readonly_provider
+        self._read_model = None
+        self._statistics = None
+        self._guide = None
+        definitions = {item['name']: item for item in _read_definitions()}
+        # Integration seams are optional and independent; no runtime discovery,
+        # native snapshot transport, proposal execution, or credential lookup.
+        for module_name, names in [('mcp_read_model', _RUNTIME_READS | _CHAT_READS),
+                                   ('mcp_statistics', _STATISTICS_READS),
+                                   ('demo_guide', {'demo_guide'})]:
+            module = _optional_module(module_name)
+            supplied = _dependency_definitions(module, names)
+            if supplied is None:
+                continue
+            try:
+                if module_name == 'mcp_read_model':
+                    model = module.ReadModel(snapshot_provider=readonly_provider)
+                    if not callable(model.execute):
+                        continue
+                    self._read_model = model
+                elif module_name == 'mcp_statistics':
+                    if not callable(module.execute):
+                        continue
+                    self._statistics = module
+                else:
+                    if not callable(module.demo_guide) or not callable(module.read_resource):
+                        continue
+                    resources = module.resource_definitions()
+                    if (type(resources) is not list or len(resources) != 1 or
+                            type(resources[0]) is not dict or resources[0].get('uri') != _GUIDE_URI):
+                        continue
+                    self._guide = module
+            except Exception:
+                continue
+            # Keep the frozen descriptors, including bounded fixed text: a
+            # dependency cannot widen discovery or leak an arbitrary field.
+        self._read_tools = list(definitions.values())
+
+    def _guide_call(self, identity, arguments):
+        if self._guide is None:
+            return _bounded_read_response(identity, _unavailable('provider_unavailable'))
+        try:
+            value = _checked_guide(self._guide.demo_guide(arguments), arguments.get('step'))
+            if value['error'] == 'unknown_resource':
+                raise ValueError('invalid_snapshot')
+        except Exception:
+            return _bounded_read_response(identity, _unavailable('invalid_snapshot'))
+        return self._guide_response(identity, value)
+
+    def _guide_response(self, identity, value):
+        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
+        response = _result(identity, {'content': [{'type': 'text', 'text': text}],
+                                      'structuredContent': value, 'isError': value['error'] is not None})
+        if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
+            return _bounded_read_response(identity, _unavailable('response_too_large'))
+        return response
+
+    def _resources(self, identity, method, params):
+        if method == 'resources/list':
+            if params != {}:
+                return _error(identity, -32602)
+            return _result(identity, {'resources': [{'uri': _GUIDE_URI, 'name': 'demo_guide',
+                                                    'mimeType': 'application/json'}]})
+        if not _keys(params, 'uri') or type(params['uri']) is not str:
+            return _error(identity, -32602)
+        if params['uri'] != _GUIDE_URI:
+            value = _guide_error('unknown_resource')
+        elif self._guide is None:
+            value = _unavailable('provider_unavailable')
+        else:
+            try:
+                value = _checked_guide(self._guide.read_resource(_GUIDE_URI))
+                if value['error'] is not None:
+                    raise ValueError('invalid_snapshot')
+            except Exception:
+                value = _unavailable('invalid_snapshot')
+        # Only the contracted resource is exposed. Never reflect unknown URIs.
+        response = _result(identity, {'contents': [{'uri': _GUIDE_URI, 'mimeType': 'application/json',
+                                                   'text': json.dumps(value, ensure_ascii=False,
+                                                                      allow_nan=False, separators=(',', ':'))}]})
+        if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_RESPONSE:
+            response['result']['contents'][0]['text'] = json.dumps(_unavailable('response_too_large'), separators=(',', ':'))
+        return response
+
+    def _readonly_call(self, identity, name, arguments):
+        try:
+            arguments = _validate_read_arguments(name, arguments)
+        except (ValueError, TypeError, UnicodeError, OverflowError):
+            if name == 'demo_guide':
+                return self._guide_response(identity, _guide_error('invalid_arguments'))
+            return _bounded_read_response(identity, _unavailable('invalid_arguments'))
+        if name == 'demo_guide':
+            return self._guide_call(identity, arguments)
+        if ((name in _RUNTIME_READS and not callable(self.readonly_provider)) or
+                (name in _RUNTIME_READS | _CHAT_READS and self._read_model is None) or
+                (name in _STATISTICS_READS and self._statistics is None)):
+            return _bounded_read_response(identity, _unavailable('provider_unavailable'))
+        scan = None
+        try:
+            if name in _RUNTIME_READS:
+                value = self._read_model.execute(name, arguments, events=None, scan=None)
+            else:
+                reader = DataReader(self.data)
+                records = reader.events(arguments['channel'])
+                try:
+                    # One bounded validated scan, finalized before aggregation.
+                    # Optional modules receive values only, never file paths.
+                    events = list(records)
+                finally:
+                    records.close()
+                    scan = dict(reader.stats)
+                if name in _CHAT_READS:
+                    value = self._read_model.execute(name, arguments, events=events, scan=dict(scan))
+                else:
+                    value = self._statistics.execute(name, arguments, events, scan=dict(scan))
+        except Exception:
+            error = 'provider_unavailable' if name in _RUNTIME_READS else 'query_unavailable'
+            return _bounded_read_response(identity, _unavailable(error, scan))
+        try:
+            value = _checked_read_result(name, value, scan, arguments)
+            return _bounded_read_response(identity, value)
+        except Exception:
+            return _bounded_read_response(identity, _unavailable('invalid_snapshot', scan))
 
     def handle(self, request):
         if type(request) is not dict or request.get('jsonrpc') != '2.0' or type(request.get('method')) is not str or set(request) - {'jsonrpc', 'id', 'method', 'params'}:
@@ -352,16 +787,21 @@
                 return _error(identity, -32602)
             self.state = 'initializing'
             return _result(identity, {'protocolVersion': PROTOCOL_VERSION,
-                                     'capabilities': {'tools': {'listChanged': False}},
+                                     'capabilities': {'tools': {'listChanged': False},
+                                                      'resources': {'subscribe': False, 'listChanged': False}},
                                      'serverInfo': {'name': 'cafe', 'version': '0.1.0'}})
         if self.state != 'ready':
             return _error(identity, -32600)
         if method == 'tools/list':
-            return _result(identity, {'tools': tools(control_enabled=self.control is not None)}) if params == {} else _error(identity, -32602)
+            return _result(identity, {'tools': tools() + self._read_tools}) if params == {} else _error(identity, -32602)
+        if method in {'resources/list', 'resources/read'}:
+            return self._resources(identity, method, params)
         if method != 'tools/call':
             return _error(identity, -32601)
         if type(params) is not dict or set(params) - {'name', 'arguments'} or type(params.get('name')) is not str:
             return _error(identity, -32602)
+        if params['name'] in _READ_ARGUMENTS:
+            return self._readonly_call(identity, params['name'], params.get('arguments', {}))
         try:
             args = validate_arguments(params['name'], params.get('arguments', {}))
         except ValueError:
diff --git a/tests/integration/test_mcp_server.py b/tests/integration/test_mcp_server.py
--- a/tests/integration/test_mcp_server.py
+++ b/tests/integration/test_mcp_server.py
@@ -18,6 +18,15 @@
 from cafe import mcp_server as mcp
 
 
+CONTRACTED_TOOLS = frozenset({
+    'list_channels_and_days', 'recent_messages', 'search_messages',
+    'top_chatters', 'message_rate', 'session_summary',
+    'list_capsules', 'list_candidate_windows', 'recording_status', 'obs_status',
+    'messages_by_user', 'messages_between', 'chat_statistics', 'chat_activity',
+    'demo_guide',
+})
+
+
 def event(text='hello', login='alice', second=0, day='2000-01-01'):
     return {'schema': 'cafe.chat_event.v1', 'channel': 'test_channel',
             'received_at': {'monotonic_ns': str(second), 'utc': day + 'T00:00:' + str(second).zfill(2) + 'Z'},
@@ -73,16 +82,53 @@
         self.assertEqual(result.stderr, b'')
         rows = [json.loads(x) for x in result.stdout.splitlines()]
         self.assertEqual([row['id'] for row in rows], [1, 2, 3, 4])
-        self.assertEqual(len(rows[1]['result']['tools']), 6)
+        listed = rows[1]['result']['tools']
+        self.assertEqual({tool['name'] for tool in listed}, CONTRACTED_TOOLS)
+        self.assertEqual(len(listed), 15)
         self.assertEqual(rows[2]['result'], {})
         self.assertEqual([x['text'] for x in rows[3]['result']['structuredContent']['data']], ['second literal', 'third literal'])
 
-    def test_six_tool_schemas_are_closed_and_readonly(self):
+    def test_fifteen_tool_schemas_are_closed_and_readonly(self):
         tools = self.server().handle(request('tools/list'))['result']['tools']
-        self.assertEqual({x['name'] for x in tools}, set(mcp._TOOL_ARGUMENTS))
+        self.assertEqual({x['name'] for x in tools}, CONTRACTED_TOOLS)
+        self.assertEqual(len(tools), 15)
         for tool in tools:
             self.assertIs(tool['inputSchema']['additionalProperties'], False)
             self.assertIs(tool['annotations']['readOnlyHint'], True)
+
+    def test_original_descriptors_and_protocol_limits_are_unchanged(self):
+        channel = {'type': 'string', 'pattern': '^[a-z0-9_]{3,25}$'}
+        limit = {'type': 'integer', 'minimum': 1, 'maximum': 200}
+        definitions = [
+            ('list_channels_and_days', {}, []),
+            ('recent_messages', {'channel': channel, 'limit': limit,
+                                 'since': {'type': 'string', 'maxLength': 30}}, ['channel']),
+            ('search_messages', {'channel': channel,
+                                 'query': {'type': 'string', 'minLength': 1, 'maxLength': 512},
+                                 'limit': limit}, ['channel', 'query']),
+            ('top_chatters', {'channel': channel,
+                             'window_minutes': {'type': 'integer', 'minimum': 1,
+                                                'maximum': 1440}}, ['channel', 'window_minutes']),
+            ('message_rate', {'channel': channel,
+                             'bucket_seconds': {'type': 'integer', 'minimum': 1,
+                                                'maximum': 3600}}, ['bucket_seconds', 'channel']),
+            ('session_summary', {'channel': channel,
+                                 'day': {'type': 'string', 'maxLength': 10}}, ['channel', 'day']),
+        ]
+        expected = [
+            {'name': name, 'description': 'Read-only chat query: ' + name,
+             'inputSchema': {'type': 'object', 'additionalProperties': False,
+                             'properties': properties, 'required': required},
+             'annotations': {'readOnlyHint': True, 'destructiveHint': False,
+                             'idempotentHint': True, 'openWorldHint': False}}
+            for name, properties, required in definitions
+        ]
+        listed = self.server().handle(request('tools/list'))['result']['tools']
+        self.assertEqual(listed[:6], expected)
+        self.assertEqual(mcp.PROTOCOL_VERSION, '2025-11-25')
+        self.assertEqual((mcp.MAX_REQUEST, mcp.MAX_RESPONSE, mcp.MAX_EVENT_LINE,
+                          mcp.MAX_SCAN_BYTES, mcp.MAX_FILES),
+                         (65536, 65536, 131072, 33554432, 4096))
 
     def test_queries_match_handwritten_expected_operations(self):
         data = lambda name, args: self.call(name, args)['result']['structuredContent']['data']
diff --git a/tests/integration/test_mcp_bridge_dispatch.py b/tests/integration/test_mcp_bridge_dispatch.py
new file mode 100644
--- /dev/null
+++ b/tests/integration/test_mcp_bridge_dispatch.py
@@ -0,0 +1,761 @@
+"""Synthetic dispatcher contract tests; no live native or dependency acceptance.
+
+The optional modules are strict in-process contract doubles. Cursor tests prove
+delegation and preservation of closed errors, not the absent modules' algorithms.
+Every guide live marker stays pending and every identifier below is synthetic.
+"""
+from contextlib import ExitStack, redirect_stderr, redirect_stdout
+from copy import deepcopy
+import inspect
+import io
+import json
+from pathlib import Path
+import socket
+import subprocess
+import sys
+import tempfile
+from types import ModuleType
+import unittest
+from unittest import mock
+
+ROOT = Path(__file__).resolve().parents[2]
+sys.path.insert(0, str(ROOT / 'connectors'))
+from cafe import mcp_server as mcp
+
+ORIGINAL_NAMES = {
+    'list_channels_and_days', 'recent_messages', 'search_messages',
+    'top_chatters', 'message_rate', 'session_summary',
+}
+RUNTIME_NAMES = {'list_capsules', 'list_candidate_windows', 'recording_status', 'obs_status'}
+CHAT_NAMES = {'messages_by_user', 'messages_between'}
+STATISTICS_NAMES = {'chat_statistics', 'chat_activity'}
+READ_NAMES = RUNTIME_NAMES | CHAT_NAMES | STATISTICS_NAMES
+ALL_NAMES = ORIGINAL_NAMES | READ_NAMES | {'demo_guide'}
+MODULE_NAMES = ('cafe.mcp_read_model', 'cafe.mcp_statistics', 'cafe.demo_guide')
+GUIDE_URI = 'cafe://demo/guide'
+INTERVAL = {'channel': 'synthetic_channel', 'from': '2000-01-01T00:00:00Z',
+            'to': '2000-01-01T00:01:00Z'}
+SCAN_FIELDS = {'corrupt_lines', 'truncated_lines', 'bytes_scanned', 'files_scanned', 'scan_limited'}
+
+
+def request(method, params=None, identity=1):
+    result = {'jsonrpc': '2.0', 'id': identity, 'method': method}
+    if params is not None:
+        result['params'] = params
+    return result
+
+
+def initialize(server):
+    result = server.handle(request('initialize', {
+        'protocolVersion': '2025-11-25', 'capabilities': {},
+        'clientInfo': {'name': 'synthetic-peer', 'version': '1'},
+    }))
+    assert result['result']['protocolVersion'] == '2025-11-25'
+    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
+    return result
+
+
+def read_result(data=None, *, error=None, scan=None, cursor=None, complete=True):
+    return {'schema': 'cafe.mcp_read.v1',
+            'availability': 'available' if error in (None, 'scan_limited') else 'unavailable',
+            'data': data, 'page': {'next_cursor': cursor, 'complete': complete},
+            'scan': scan, 'error': error}
+
+
+def synthetic_event(second=0, text='synthetic literal'):
+    return {'schema': 'cafe.chat_event.v1', 'channel': 'synthetic_channel',
+            'received_at': {'monotonic_ns': str(second),
+                            'utc': '2000-01-01T00:00:' + str(second).zfill(2) + 'Z'},
+            'sent_at': None, 'message_id': 'synthetic-message-' + str(second),
+            'user_login': 'synthetic_user', 'display_name': 'Synthetic',
+            'badges': [], 'text': text, 'kind': 'message', 'session_id': 'synthetic-session'}
+
+
+def runtime_data(name):
+    if name == 'list_capsules':
+        return [{'capsule_id': 'synthetic-capsule', 'slot': 'slot-1',
+                 'person_label': 'Synthetic', 'generation': '1',
+                 'feeds': [{'feed_id': 'synthetic-feed', 'role': 'stream', 'generation': '1'}]}]
+    if name == 'list_candidate_windows':
+        return [{'window_token': 'synthetic-window', 'generation': '1', 'layout_revision': '1',
+                 'title': 'Synthetic window', 'executable': 'synthetic.exe',
+                 'rect': {'width': 640, 'height': 480}, 'visible': True, 'minimized': False}]
+    if name == 'recording_status':
+        return {'capsules': [{'capsule_id': 'synthetic-capsule', 'state': 'idle',
+                             'feeds': [{'feed_id': 'synthetic-feed', 'state': 'idle',
+                                        'written_frames': None, 'backpressure': '0'}]}],
+                'recorder_available': None}
+    return {'available': None, 'configured': None, 'state': None}
+
+
+def aggregate():
+    return {'message_count': 3, 'unique_chatters': 1,
+            'messages_per_bucket': [{'start': INTERVAL['from'], 'count': 3}],
+            'top_words': [{'word': 'synthetic', 'count': 3}],
+            'peaks': [{'start': INTERVAL['from'], 'count': 3}],
+            'top_emotes': None, 'emotes_status': 'metadata_unavailable', 'complete': True}
+
+
+def guide(step=None, error=None):
+    ids = list(range(1, 8)) if step is None else [step]
+    steps = [{'id': i, 'instruction': 'Synthetic instruction',
+              'expected': 'Synthetic expected result', 'evidence_required': 'Synthetic evidence',
+              'depends_on': [] if i == 1 else [i - 1], 'live_status': 'pending'} for i in ids]
+    return {'schema': 'cafe.demo_guide.v1', 'steps': [] if error else steps,
+            'control_mode': 'human_confirmed', 'error': error}
+
+
+def definitions(names):
+    page = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
+            'cursor': {'type': ['string', 'null'], 'maxLength': 512}}
+    interval = {'channel': {'type': 'string', 'pattern': '^[a-z0-9_]{3,25}$'},
+                'from': {'type': 'string', 'maxLength': 30},
+                'to': {'type': 'string', 'maxLength': 30}}
+    result = []
+    for name in sorted(names):
+        required = []
+        if name in {'list_capsules', 'list_candidate_windows'}:
+            fields = deepcopy(page)
+        elif name in CHAT_NAMES or name == 'chat_activity':
+            fields = {**deepcopy(interval), **deepcopy(page)}
+            required = ['channel', 'from', 'to']
+        elif name == 'chat_statistics':
+            fields = {**deepcopy(interval),
+                      'bucket_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 60},
+                      'top_limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
+            required = ['channel', 'from', 'to']
+        elif name == 'recording_status':
+            fields = {'capsule_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$'}}
+        elif name == 'demo_guide':
+            fields = {'step': {'type': 'integer', 'minimum': 1, 'maximum': 7}}
+        else:
+            fields = {}
+        result.append({'name': name, 'description': 'Synthetic contract: ' + name,
+                       'inputSchema': {'type': 'object', 'properties': fields,
+                                       'additionalProperties': False, 'required': required},
+                       'annotations': {'readOnlyHint': True, 'destructiveHint': False,
+                                       'idempotentHint': True, 'openWorldHint': False}})
+    return result
+
+
+class ContractModules:
+    """Frozen call signatures only; no native snapshot or aggregate implementation."""
+
+    def __init__(self):
+        owner = self
+        self.instances = []
+        self.providers = []
+        self.read_execute = mock.Mock(side_effect=self.default_read)
+        self.stats_execute = mock.Mock(side_effect=self.default_stats)
+        self.guide_call = mock.Mock(side_effect=lambda arguments: guide(arguments.get('step')))
+        self.resource_call = mock.Mock(side_effect=lambda uri: guide() if uri == GUIDE_URI else guide(error='unknown_resource'))
+        read = ModuleType(MODULE_NAMES[0])
+        stats = ModuleType(MODULE_NAMES[1])
+        demo = ModuleType(MODULE_NAMES[2])
+
+        class ReadModel:
+            def __init__(self, snapshot_provider=None):
+                owner.providers.append(snapshot_provider)
+                owner.instances.append(self)
+
+            def execute(self, name, arguments, *, events=None, scan=None):
+                return owner.read_execute(name, arguments, events=events, scan=scan)
+
+        def execute(name, arguments, events, *, scan):
+            return owner.stats_execute(name, arguments, events, scan=scan)
+
+        read.ReadModel = ReadModel
+        read.tool_definitions = mock.Mock(return_value=definitions(RUNTIME_NAMES | CHAT_NAMES))
+        stats.execute = execute
+        stats.tool_definitions = mock.Mock(return_value=definitions(STATISTICS_NAMES))
+        demo.tool_definitions = mock.Mock(return_value=definitions({'demo_guide'}))
+        demo.demo_guide = self.guide_call
+        demo.resource_definitions = mock.Mock(return_value=[{
+            'uri': GUIDE_URI, 'name': 'demo_guide', 'mimeType': 'application/json',
+        }])
+        demo.read_resource = self.resource_call
+        self.modules = dict(zip(MODULE_NAMES, (read, stats, demo)))
+
+    def default_read(self, name, arguments, *, events=None, scan=None):
+        if name in CHAT_NAMES:
+            return read_result(list(events), scan=scan)
+        return read_result(runtime_data(name))
+
+    def default_stats(self, name, arguments, events, *, scan):
+        list(events)
+        return read_result(aggregate(), scan=scan)
+
+
+class McpBridgeDispatch(unittest.TestCase):
+    def setUp(self):
+        temporary = tempfile.TemporaryDirectory(prefix='cafe-bridge-synthetic-')
+        self.addCleanup(temporary.cleanup)
+        self.root = Path(temporary.name).resolve()
+        self.contracts = ContractModules()
+        self.provider = mock.Mock(return_value=None)
+
+    def server(self, *, modules=None, provider=True, control=None, query=None, clock=None):
+        installed = self.contracts.modules if modules is None else modules
+        with mock.patch.dict(sys.modules, installed):
+            server = mcp.Server(self.root, readonly_provider=self.provider if provider else None,
+                                control=control, query=query, clock=clock)
+        initialize(server)
+        return server
+
+    def call(self, server, name, arguments=None):
+        return server.handle(request('tools/call', {'name': name,
+                                                  'arguments': {} if arguments is None else arguments}))
+
+    def typed(self, response, error=None):
+        self.assertNotIn('error', response)
+        result = response['result']
+        payload = result['structuredContent']
+        self.assertEqual(set(payload), {'data', 'scan'})
+        value = payload['data']
+        self.assertEqual(set(value), {'schema', 'availability', 'data', 'page', 'scan', 'error'})
+        self.assertEqual(value['schema'], 'cafe.mcp_read.v1')
+        self.assertEqual(set(value['page']), {'next_cursor', 'complete'})
+        self.assertEqual(value['scan'], payload['scan'])
+        self.assertEqual(value['error'], error)
+        self.assertEqual(json.loads(result['content'][0]['text']), payload)
+        self.assertEqual(result['isError'], error is not None)
+        return value
+
+    def write_events(self, rows, tail=b''):
+        folder = self.root / 'twitch' / 'synthetic_channel'
+        folder.mkdir(parents=True, exist_ok=True)
+        target = folder / '2000-01-01.jsonl'
+        target.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n'
+                                   for row in rows) + tail)
+        return target
+
+    def test_constructor_adds_only_optional_readonly_provider(self):
+        signature = inspect.signature(mcp.Server)
+        self.assertEqual(list(signature.parameters), ['data', 'query', 'clock', 'control', 'readonly_provider'])
+        for name in ['query', 'clock', 'control', 'readonly_provider']:
+            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
+            self.assertIsNone(signature.parameters[name].default)
+
+    def test_exact_listing_stays_closed_even_with_explicit_control(self):
+        control = mock.Mock()
+        server = self.server(control=control)
+        listed = server.handle(request('tools/list'))['result']['tools']
+        self.assertEqual(len(listed), 15)
+        self.assertEqual({item['name'] for item in listed}, ALL_NAMES)
+        for item in listed:
+            self.assertIs(item['inputSchema']['additionalProperties'], False)
+            self.assertIs(item['annotations']['readOnlyHint'], True)
+        control.execute.assert_not_called()
+        control.approve.assert_not_called()
+
+    def test_absent_modules_fail_closed_without_breaking_original_queries(self):
+        server = self.server(modules=dict.fromkeys(MODULE_NAMES))
+        for name in sorted(READ_NAMES | {'demo_guide'}):
+            arguments = INTERVAL if name in CHAT_NAMES | STATISTICS_NAMES else {}
+            with self.subTest(name=name):
+                value = self.typed(self.call(server, name, arguments), 'provider_unavailable')
+                self.assertEqual(value['availability'], 'unavailable')
+                self.assertIsNone(value['data'])
+                self.assertFalse(value['page']['complete'])
+        original = self.call(server, 'list_channels_and_days')['result']
+        self.assertFalse(original['isError'])
+        self.assertEqual(original['structuredContent']['data'], [])
+
+    def test_missing_group_does_not_disable_available_groups(self):
+        modules = {**self.contracts.modules, MODULE_NAMES[0]: None}
+        server = self.server(modules=modules)
+        self.typed(self.call(server, 'list_capsules'), 'provider_unavailable')
+        self.typed(self.call(server, 'chat_statistics', INTERVAL))
+        self.assertEqual(self.call(server, 'demo_guide')['result']['structuredContent'], guide())
+
+    def test_runtime_routes_only_to_read_model_and_never_reads_chat_or_controls(self):
+        control = mock.Mock()
+        query = mock.Mock(side_effect=AssertionError('unexpected synthetic query'))
+        clock = mock.Mock(side_effect=AssertionError('unexpected synthetic clock'))
+        server = self.server(control=control, query=query, clock=clock)
+        stdout, stderr = io.StringIO(), io.StringIO()
+        with ExitStack() as stack:
+            stack.enter_context(redirect_stdout(stdout))
+            stack.enter_context(redirect_stderr(stderr))
+            forbidden = [stack.enter_context(mock.patch.object(mcp, 'DataReader')),
+                         stack.enter_context(mock.patch('builtins.open')),
+                         stack.enter_context(mock.patch.object(mcp.os, 'open')),
+                         stack.enter_context(mock.patch.object(socket, 'socket')),
+                         stack.enter_context(mock.patch.object(subprocess, 'Popen')),
+                         stack.enter_context(mock.patch.object(mcp.os, 'system'))]
+            for name in sorted(RUNTIME_NAMES):
+                self.assertEqual(self.typed(self.call(server, name))['data'], runtime_data(name))
+            for effect in forbidden:
+                effect.assert_not_called()
+        self.assertEqual(self.contracts.providers, [self.provider])
+        self.assertEqual(len(self.contracts.instances), 1)
+        for call in self.contracts.read_execute.call_args_list:
+            self.assertEqual(call.kwargs, {'events': None, 'scan': None})
+        query.assert_not_called()
+        clock.assert_not_called()
+        control.execute.assert_not_called()
+        control.approve.assert_not_called()
+        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
+
+    def test_no_provider_never_claims_an_empty_runtime_success(self):
+        server = self.server(provider=False)
+        for name in sorted(RUNTIME_NAMES):
+            with self.subTest(name=name):
+                value = self.typed(self.call(server, name), 'provider_unavailable')
+                self.assertEqual(value['availability'], 'unavailable')
+                self.assertIsNone(value['data'])
+                self.assertFalse(value['page']['complete'])
+        self.contracts.read_execute.assert_not_called()
+
+    def test_chat_readers_use_single_validated_scan_and_preserve_unknown_clocks(self):
+        rows = [synthetic_event(i) for i in range(3)]
+        invalid = synthetic_event(4)
+        invalid['received_at']['monotonic_ns'] = '04'
+        target = self.write_events(rows + [invalid])
+        original_reader = mcp.DataReader
+        for name in sorted(CHAT_NAMES):
+            with self.subTest(name=name):
+                server = self.server()
+                with mock.patch.object(mcp, 'DataReader', wraps=original_reader) as reader:
+                    value = self.typed(self.call(server, name, INTERVAL), 'scan_limited')
+                reader.assert_called_once_with(self.root)
+                self.assertEqual(value['data'], rows)
+                self.assertEqual(set(value['scan']), SCAN_FIELDS)
+                self.assertEqual(value['scan']['files_scanned'], 1)
+                self.assertEqual(value['scan']['bytes_scanned'], target.stat().st_size)
+                self.assertEqual(value['scan']['corrupt_lines'], 1)
+                self.assertFalse(value['page']['complete'])
+                self.assertIsNone(value['data'][0]['sent_at'])
+
+    def test_statistics_signature_gets_one_materialized_validated_iterable(self):
+        rows = [synthetic_event(i) for i in range(3)]
+        target = self.write_events(rows)
+        original_reader = mcp.DataReader
+        server = self.server()
+        for name in sorted(STATISTICS_NAMES):
+            with self.subTest(name=name):
+                with mock.patch.object(mcp, 'DataReader', wraps=original_reader) as reader:
+                    value = self.typed(self.call(server, name, INTERVAL))
+                reader.assert_called_once_with(self.root)
+                passed = self.contracts.stats_execute.call_args
+                self.assertEqual(passed.args[:2], (name, INTERVAL))
+                self.assertEqual(list(passed.args[2]), rows)
+                self.assertEqual(passed.kwargs['scan'], value['scan'])
+                self.assertEqual(value['scan']['bytes_scanned'], target.stat().st_size)
+                self.assertEqual(value['data'], aggregate())
+
+    def test_extra_parameters_and_invalid_page_arguments_fail_before_dispatch(self):
+        cases = [
+            ('list_capsules', {'extra': True}), ('list_capsules', {'limit': True}),
+            ('list_capsules', {'limit': 0}), ('list_capsules', {'limit': 201}),
+            ('list_candidate_windows', {'cursor': 3}), ('list_candidate_windows', {'cursor': []}),
+            ('list_candidate_windows', {'cursor': 'x' * 513}),
+            ('list_candidate_windows', {'cursor': '\u00e9' * 257}),
+            ('list_candidate_windows', {'cursor': '\ud800'}),
+            ('recording_status', {'capsule_id': '../synthetic'}),
+            ('recording_status', {'capsule_id': 's' * 97}),
+            ('obs_status', {'extra': 'synthetic'}),
+            ('messages_by_user', {**INTERVAL, 'user_login': 'synthetic_user'}),
+            ('messages_between', {**INTERVAL, 'limit': False}),
+            ('messages_between', {**INTERVAL, 'cursor': {}}),
+            ('chat_statistics', {**INTERVAL, 'top_limit': 51}),
+            ('chat_statistics', {**INTERVAL, 'top_limit': True}),
+            ('chat_statistics', {**INTERVAL, 'bucket_seconds': 0}),
+            ('chat_statistics', {**INTERVAL, 'bucket_seconds': 3601}),
+            ('chat_statistics', {**INTERVAL, 'bucket_seconds': False}),
+            ('chat_activity', {**INTERVAL, 'bucket_seconds': 60}),
+            ('chat_activity', {**INTERVAL, 'limit': 201}),
+        ]
+        server = self.server()
+        with mock.patch.object(mcp, 'DataReader') as reader:
+            for name, arguments in cases:
+                with self.subTest(name=name, arguments=arguments):
+                    value = self.typed(self.call(server, name, arguments), 'invalid_arguments')
+                    self.assertIsNone(value['data'])
+            reader.assert_not_called()
+        self.contracts.read_execute.assert_not_called()
+        self.contracts.stats_execute.assert_not_called()
+
+    def test_utc_calendar_offset_and_reversed_interval_rejected(self):
+        server = self.server()
+        invalid = [
+            {**INTERVAL, 'from': '2000-02-31T00:00:00Z'},
+            {**INTERVAL, 'from': '2000-01-01T00:00:00+00:00'},
+            {**INTERVAL, 'from': '2000-01-01T00:02:00Z'},
+            {**INTERVAL, 'to': '2000-01-01T24:00:00Z'},
+            {**INTERVAL, 'to': '2000-01-01T00:01:00.0000000001Z'},
+        ]
+        for arguments in invalid:
+            self.typed(self.call(server, 'messages_between', arguments), 'invalid_arguments')
+        self.contracts.read_execute.assert_not_called()
+
+    def test_unknown_tools_and_extra_rpc_parameters_keep_original_codes(self):
+        server = self.server()
+        for name in ['unknown_tool', 'approve', 'approve_recording', 'start_recording']:
+            self.assertEqual(self.call(server, name)['error']['code'], -32602)
+        response = server.handle(request('tools/call', {'name': 'list_capsules',
+                                                       'arguments': {}, 'extra': True}))
+        self.assertEqual(response['error']['code'], -32602)
+        self.contracts.read_execute.assert_not_called()
+
+    def test_interval_order_uses_exact_nanoseconds_not_strings_or_microseconds(self):
+        server = self.server()
+        equal = {**INTERVAL, 'from': '2000-01-01T00:00:00Z',
+                 'to': '2000-01-01T00:00:00.000000000Z'}
+        reverse = {**INTERVAL, 'from': '2000-01-01T00:00:00.000000002Z',
+                   'to': '2000-01-01T00:00:00.000000001Z'}
+        forward = {**INTERVAL, 'from': '2000-01-01T00:00:00.000000001Z',
+                   'to': '2000-01-01T00:00:00.000000002Z'}
+        self.typed(self.call(server, 'messages_between', equal), 'invalid_arguments')
+        self.typed(self.call(server, 'messages_between', reverse), 'invalid_arguments')
+        self.contracts.read_execute.assert_not_called()
+        self.typed(self.call(server, 'messages_between', forward))
+        self.assertEqual(self.contracts.read_execute.call_args.args,
+                         ('messages_between', forward))
+
+    def test_opaque_cursor_and_epoch_errors_are_delegated_without_rebuilding_model(self):
+        self.contracts.read_execute.side_effect = [
+            read_result(runtime_data('list_capsules'), cursor='synthetic-cursor-a', complete=False),
+            read_result(error='stale_cursor', complete=False),
+            read_result(error='stale_cursor', complete=False),
+        ]
+        server = self.server()
+        first = self.typed(self.call(server, 'list_capsules', {'limit': 1}))
+        cursor = first['page']['next_cursor']
+        self.typed(self.call(server, 'list_capsules', {'limit': 1, 'cursor': cursor}), 'stale_cursor')
+        self.typed(self.call(server, 'list_candidate_windows', {'limit': 1, 'cursor': cursor}), 'stale_cursor')
+        self.assertEqual(len(self.contracts.instances), 1)
+        self.assertEqual(self.contracts.read_execute.call_args_list[1].args,
+                         ('list_capsules', {'limit': 1, 'cursor': cursor}))
+        self.assertEqual(self.contracts.read_execute.call_args_list[2].args,
+                         ('list_candidate_windows', {'limit': 1, 'cursor': cursor}))
+
+    def test_cross_filter_and_invalid_cursor_errors_preserved(self):
+        self.contracts.read_execute.side_effect = [
+            read_result(error='stale_cursor', complete=False),
+            read_result(error='invalid_arguments', complete=False),
+        ]
+        server = self.server()
+        changed = {**INTERVAL, 'to': '2000-01-01T00:02:00Z', 'cursor': 'synthetic-prior-query'}
+        self.typed(self.call(server, 'messages_between', changed), 'stale_cursor')
+        self.typed(self.call(server, 'list_capsules', {'cursor': 'synthetic-invalid'}), 'invalid_arguments')
+        self.assertEqual(self.contracts.read_execute.call_args_list[0].args,
+                         ('messages_between', changed))
+
+    def test_malformed_result_is_closed_and_exception_text_is_absent(self):
+        server = self.server()
+        malformed = [None, [], {}, {**read_result({}), 'error': 'synthetic-private-error'},
+                     {**read_result({}), 'availability': 'perhaps'},
+                     {**read_result({}), 'page': {'next_cursor': None, 'complete': 'yes'}},
+                     {**read_result({}), 'extra': 'synthetic-private-marker'}]
+        for result in malformed:
+            with self.subTest(kind=type(result).__name__):
+                self.contracts.read_execute.side_effect = None
+                self.contracts.read_execute.return_value = result
+                response = self.call(server, 'obs_status')
+                self.typed(response, 'invalid_snapshot')
+                self.assertNotIn('synthetic-private', json.dumps(response))
+
+    def test_paginated_output_cannot_exceed_requested_or_default_limit(self):
+        server = self.server()
+        self.contracts.read_execute.side_effect = None
+        for arguments, count in [({}, 51), ({'limit': 2}, 3)]:
+            rows = []
+            for index in range(count):
+                row = runtime_data('list_capsules')[0]
+                row['capsule_id'] = 'synthetic-capsule-' + str(index)
+                row['slot'] = 'slot-' + str(index + 1)
+                rows.append(row)
+            self.contracts.read_execute.return_value = read_result(rows)
+            self.typed(self.call(server, 'list_capsules', arguments), 'invalid_snapshot')
+        for name in sorted(CHAT_NAMES):
+            self.contracts.read_execute.return_value = read_result([synthetic_event(0), synthetic_event(1)])
+            self.typed(self.call(server, name, {**INTERVAL, 'limit': 1}), 'invalid_snapshot')
+        activity = aggregate()
+        activity['messages_per_bucket'].append({'start': '2000-01-01T00:01:00Z', 'count': 1})
+        self.contracts.stats_execute.side_effect = None
+        self.contracts.stats_execute.return_value = read_result(activity)
+        self.typed(self.call(server, 'chat_activity', {
+            **INTERVAL, 'to': '2000-01-01T00:02:00Z', 'limit': 1,
+        }), 'invalid_snapshot')
+
+    def test_page_cannot_claim_complete_while_emitting_a_next_cursor(self):
+        server = self.server()
+        self.contracts.read_execute.side_effect = None
+        self.contracts.read_execute.return_value = read_result(
+            runtime_data('list_capsules'), cursor='synthetic-next-page', complete=True)
+        self.typed(self.call(server, 'list_capsules'), 'invalid_snapshot')
+
+    def test_closed_dependency_errors_never_become_empty_success(self):
+        server = self.server()
+        for error in ['provider_unavailable', 'invalid_snapshot', 'stale_cursor', 'query_unavailable']:
+            self.contracts.read_execute.side_effect = None
+            self.contracts.read_execute.return_value = read_result(error=error, complete=False)
+            value = self.typed(self.call(server, 'list_capsules'), error)
+            self.assertEqual(value['availability'], 'unavailable')
+            self.assertIsNone(value['data'])
+            self.assertFalse(value['page']['complete'])
+
+    def test_runtime_projection_rejects_extra_private_fields_and_non_json_values(self):
+        server = self.server()
+        self.contracts.read_execute.side_effect = None
+        private = {**runtime_data('obs_status'), 'confirmation_token': 'synthetic-private-marker'}
+        invalid = [private, {'available': float('nan'), 'configured': None, 'state': None},
+                   {'available': None, 'configured': None, 'state': float('inf')}]
+        for data in invalid:
+            self.contracts.read_execute.return_value = read_result(data)
+            response = self.call(server, 'obs_status')
+            self.typed(response, 'invalid_snapshot')
+            self.assertNotIn('synthetic-private-marker', json.dumps(response))
+        for name in ['list_capsules', 'list_candidate_windows']:
+            for generation in [1, True, '01', '18446744073709551616']:
+                data = runtime_data(name)
+                data[0]['generation'] = generation
+                self.contracts.read_execute.return_value = read_result(data)
+                self.typed(self.call(server, name), 'invalid_snapshot')
+
+    def test_statistics_rejects_invalid_counts_metadata_and_private_extra_fields(self):
+        server = self.server()
+        self.contracts.stats_execute.side_effect = None
+        invalid = [{**aggregate(), 'message_count': True},
+                   {**aggregate(), 'message_count': -1},
+                   {**aggregate(), 'unique_chatters': None},
+                   {**aggregate(), 'top_emotes': []},
+                   {**aggregate(), 'emotes_status': 'available'},
+                   {**aggregate(), 'confirmation_token': 'synthetic-private-marker'}]
+        for data in invalid:
+            self.contracts.stats_execute.return_value = read_result(data)
+            response = self.call(server, 'chat_statistics', INTERVAL)
+            self.typed(response, 'invalid_snapshot')
+            self.assertNotIn('synthetic-private-marker', json.dumps(response))
+        excessive_words = aggregate()
+        excessive_words['top_words'].append({'word': 'synthetic_second', 'count': 1})
+        self.contracts.stats_execute.return_value = read_result(excessive_words)
+        self.typed(self.call(server, 'chat_statistics', {**INTERVAL, 'top_limit': 1}), 'invalid_snapshot')
+
+    def test_dependency_reported_incomplete_statistics_cannot_claim_completeness(self):
+        target = self.write_events([synthetic_event(i) for i in range(3)])
+        server = self.server()
+        self.contracts.stats_execute.side_effect = None
+        for availability in ['available', 'unavailable']:
+            partial = read_result(aggregate(), error='scan_limited', complete=True)
+            partial['availability'] = availability
+            self.contracts.stats_execute.return_value = partial
+            result = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
+            self.assertEqual(result['availability'], availability)
+            self.assertFalse(result['page']['complete'])
+            self.assertFalse(result['data']['complete'])
+            self.assertEqual(result['data']['message_count'], 3)
+            self.assertEqual(result['scan']['files_scanned'], 1)
+            self.assertEqual(result['scan']['bytes_scanned'], target.stat().st_size)
+        private = {**aggregate(), 'confirmation_token': 'synthetic-private-marker'}
+        partial = read_result(private, error='scan_limited', complete=True)
+        partial['availability'] = 'unavailable'
+        self.contracts.stats_execute.return_value = partial
+        response = self.call(server, 'chat_statistics', INTERVAL)
+        self.typed(response, 'invalid_snapshot')
+        self.assertNotIn('synthetic-private-marker', json.dumps(response))
+
+    def test_recognized_chat_event_cannot_bypass_canonical_clock_validation(self):
+        server = self.server()
+        malformed = synthetic_event()
+        malformed['received_at']['monotonic_ns'] = '01'
+        self.contracts.read_execute.side_effect = None
+        self.contracts.read_execute.return_value = read_result([malformed])
+        self.typed(self.call(server, 'messages_between', INTERVAL), 'invalid_snapshot')
+
+    def test_recording_clock_strings_remain_canonical_and_null_is_unknown(self):
+        server = self.server()
+        self.contracts.read_execute.side_effect = None
+        for clock in ['0', '18446744073709551615', None]:
+            data = runtime_data('recording_status')
+            data['capsules'][0]['feeds'][0]['written_frames'] = clock
+            self.contracts.read_execute.return_value = read_result(data)
+            result = self.typed(self.call(server, 'recording_status'))
+            self.assertEqual(result['data']['capsules'][0]['feeds'][0]['written_frames'], clock)
+        for clock in ['00', '01', '-1', '1e2', '18446744073709551616', 0, True, 1.0]:
+            data = runtime_data('recording_status')
+            data['capsules'][0]['feeds'][0]['written_frames'] = clock
+            self.contracts.read_execute.return_value = read_result(data)
+            self.typed(self.call(server, 'recording_status'), 'invalid_snapshot')
+
+    def test_corrupt_and_torn_scan_keeps_statistics_but_never_exact_completeness(self):
+        target = self.write_events([synthetic_event(i) for i in range(3)], b'not json\n{"torn":')
+        original = target.read_bytes()
+        server = self.server()
+        value = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
+        self.assertEqual(value['data']['message_count'], 3)
+        self.assertFalse(value['data']['complete'])
+        self.assertFalse(value['page']['complete'])
+        self.assertEqual(value['scan']['corrupt_lines'], 1)
+        self.assertEqual(value['scan']['truncated_lines'], 1)
+        self.assertEqual(value['scan']['bytes_scanned'], len(original))
+        self.assertEqual(target.read_bytes(), original)
+
+    def test_dependency_cannot_mutate_authoritative_scan_or_hide_corruption(self):
+        target = self.write_events([synthetic_event()], b'not json\n')
+        size = target.stat().st_size
+
+        def altered(name, arguments, events, *, scan):
+            list(events)
+            scan.update({'bytes_scanned': 0, 'corrupt_lines': 0, 'scan_limited': False,
+                         'confirmation_token': 'synthetic-private-marker'})
+            return read_result(aggregate(), scan=scan)
+
+        self.contracts.stats_execute.side_effect = altered
+        server = self.server()
+        response = self.call(server, 'chat_statistics', INTERVAL)
+        value = self.typed(response, 'scan_limited')
+        self.assertEqual(set(value['scan']), SCAN_FIELDS)
+        self.assertEqual(value['scan']['bytes_scanned'], size)
+        self.assertEqual(value['scan']['corrupt_lines'], 1)
+        self.assertFalse(value['data']['complete'])
+        self.assertNotIn('synthetic-private-marker', json.dumps(response))
+
+    def test_scan_byte_and_file_caps_cannot_be_reported_complete(self):
+        self.write_events([synthetic_event()])
+        server = self.server()
+        for constant in ['MAX_SCAN_BYTES', 'MAX_FILES']:
+            with self.subTest(constant=constant), mock.patch.object(mcp, constant, 0):
+                value = self.typed(self.call(server, 'chat_statistics', INTERVAL), 'scan_limited')
+                self.assertTrue(value['scan']['scan_limited'])
+                self.assertFalse(value['data']['complete'])
+                self.assertFalse(value['page']['complete'])
+
+    def test_scan_closes_on_failure_and_does_not_expose_private_exception(self):
+        self.write_events([synthetic_event()])
+        original_reader = mcp.DataReader
+        closed = []
+
+        class ObservedReader(original_reader):
+            def events(self, channel=None):
+                try:
+                    yield from super().events(channel)
+                finally:
+                    closed.append(True)
+
+        self.contracts.stats_execute.side_effect = ValueError('synthetic-private-exception')
+        server = self.server()
+        with mock.patch.object(mcp, 'DataReader', ObservedReader):
+            response = self.call(server, 'chat_statistics', INTERVAL)
+        self.typed(response, 'query_unavailable')
+        self.assertEqual(closed, [True])
+        self.assertNotIn('synthetic-private-exception', json.dumps(response))
+
+    def test_oversized_read_result_preserves_cap_and_scan_with_typed_error(self):
+        self.write_events([synthetic_event(i, text='x' * 16000) for i in range(4)])
+        server = self.server()
+        response = self.call(server, 'messages_between', INTERVAL)
+        value = self.typed(response, 'response_too_large')
+        self.assertIsNone(value['data'])
+        self.assertFalse(value['page']['complete'])
+        self.assertEqual(value['scan']['files_scanned'], 1)
+        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1, 65536)
+
+    def test_duplicate_keys_and_non_json_numbers_are_parse_errors_then_recover(self):
+        server = self.server()
+        raws = [b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":1,"limit":2}}}\n',
+                b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":NaN}}}\n',
+                b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_capsules","arguments":{"limit":Infinity}}}\n']
+        for raw in raws:
+            output = io.BytesIO()
+            mcp.serve(server, io.BytesIO(raw + json.dumps(request('ping', identity=2)).encode() + b'\n'), output)
+            rows = [json.loads(line) for line in output.getvalue().splitlines()]
+            self.assertEqual(rows[0]['error'], {'code': -32700, 'message': 'Parse error'})
+            self.assertEqual(rows[1]['result'], {})
+        self.contracts.read_execute.assert_not_called()
+
+    def test_guide_tool_and_resource_roundtrip_have_same_flat_shape(self):
+        server = self.server()
+        listed = server.handle(request('resources/list'))['result']['resources']
+        self.assertEqual([item['uri'] for item in listed], [GUIDE_URI])
+        tool = self.call(server, 'demo_guide')['result']
+        resource = server.handle(request('resources/read', {'uri': GUIDE_URI}))['result']
+        self.assertEqual(set(tool['structuredContent']), {'schema', 'steps', 'control_mode', 'error'})
+        self.assertEqual(tool['structuredContent'], guide())
+        self.assertEqual(json.loads(tool['content'][0]['text']), guide())
+        self.assertEqual(len(resource['contents']), 1)
+        self.assertEqual(resource['contents'][0]['uri'], GUIDE_URI)
+        self.assertEqual(resource['contents'][0]['mimeType'], 'application/json')
+        self.assertEqual(json.loads(resource['contents'][0]['text']), tool['structuredContent'])
+        self.assertTrue(all(step['live_status'] == 'pending' for step in tool['structuredContent']['steps']))
+        self.assertEqual(self.call(server, 'demo_guide', {'step': 7})['result']['structuredContent'], guide(7))
+
+    def test_guide_step_errors_and_unknown_resource_keep_flat_closed_shape(self):
+        server = self.server()
+        for arguments in [{'step': 0}, {'step': 8}, {'step': True}, {'step': '1'}, {'extra': 1}]:
+            result = self.call(server, 'demo_guide', arguments)['result']
+            self.assertTrue(result['isError'])
+            self.assertEqual(result['structuredContent'], guide(error='invalid_arguments'))
+        self.contracts.guide_call.assert_not_called()
+        resource = server.handle(request('resources/read', {'uri': 'cafe://demo/unknown'}))['result']
+        self.assertEqual(json.loads(resource['contents'][0]['text']), guide(error='unknown_resource'))
+        for method, arguments in [('resources/list', {'extra': True}),
+                                  ('resources/read', {}),
+                                  ('resources/read', {'uri': GUIDE_URI, 'extra': True})]:
+            self.assertEqual(server.handle(request(method, arguments))['error']['code'], -32602)
+
+    def test_guide_paths_do_not_read_chat_open_network_or_invoke_effects(self):
+        control = mock.Mock()
+        server = self.server(control=control)
+        stdout, stderr = io.StringIO(), io.StringIO()
+        with ExitStack() as stack:
+            stack.enter_context(redirect_stdout(stdout))
+            stack.enter_context(redirect_stderr(stderr))
+            forbidden = [stack.enter_context(mock.patch.object(mcp, 'DataReader')),
+                         stack.enter_context(mock.patch('builtins.open')),
+                         stack.enter_context(mock.patch.object(mcp.os, 'open')),
+                         stack.enter_context(mock.patch.object(socket, 'socket')),
+                         stack.enter_context(mock.patch.object(subprocess, 'Popen')),
+                         stack.enter_context(mock.patch.object(mcp.os, 'system'))]
+            self.call(server, 'demo_guide')
+            server.handle(request('resources/read', {'uri': GUIDE_URI}))
+            for effect in forbidden:
+                effect.assert_not_called()
+        control.execute.assert_not_called()
+        control.approve.assert_not_called()
+        self.provider.assert_not_called()
+        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
+
+    def test_malformed_guide_cannot_close_live_markers_or_emit_private_extra_fields(self):
+        server = self.server()
+        changed = guide()
+        changed['steps'][0]['live_status'] = 'passed'
+        private = guide()
+        private['synthetic_private_field'] = 'synthetic-private-marker'
+        for result in [changed, private]:
+            self.contracts.guide_call.side_effect = None
+            self.contracts.guide_call.return_value = result
+            response = self.call(server, 'demo_guide')
+            self.typed(response, 'invalid_snapshot')
+            self.assertNotIn('synthetic-private-marker', json.dumps(response))
+
+    def test_oversized_guide_keeps_original_transport_cap(self):
+        server = self.server()
+        oversized = guide()
+        oversized['steps'][0]['instruction'] = 'x' * 65536
+        self.contracts.guide_call.side_effect = None
+        self.contracts.guide_call.return_value = oversized
+        response = self.call(server, 'demo_guide')
+        self.typed(response, 'response_too_large')
+        self.assertLessEqual(len(json.dumps(response).encode('utf-8')) + 1, 65536)
+
+    def test_lone_surrogate_guide_content_fails_closed_for_tool_and_resource(self):
+        server = self.server()
+        malformed = guide()
+        malformed['steps'][0]['instruction'] = '\ud800'
+        self.contracts.guide_call.side_effect = None
+        self.contracts.guide_call.return_value = malformed
+        self.contracts.resource_call.side_effect = None
+        self.contracts.resource_call.return_value = malformed
+        self.typed(self.call(server, 'demo_guide'), 'invalid_snapshot')
+        response = server.handle(request('resources/read', {'uri': GUIDE_URI}))
+        result = json.loads(response['result']['contents'][0]['text'])
+        self.assertEqual(result['schema'], 'cafe.mcp_read.v1')
+        self.assertEqual(result['error'], 'invalid_snapshot')
+        self.assertEqual(result['availability'], 'unavailable')
+        self.assertIsNone(result['data'])
+
+
+if __name__ == '__main__':
+    unittest.main()
```
