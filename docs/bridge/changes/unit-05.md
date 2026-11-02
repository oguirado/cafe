# PRO-05 complete proposed product patch

Base commit: `f1e874050e305645a90e64267e50f8625daae9bc`.

## Assumptions

1. The complete product patch covers the four frozen new paths; the two explicitly authorized transport files package it and are excluded from their own payload to avoid recursive patching.
2. Invalid guide arguments always return empty steps with invalid_arguments; only the exact resource URI succeeds.
3. All prior demo steps are prerequisites for the next checkpoint; every live status and all seven documentary markers remain pending.
4. Server/resource registration, native providers, CI test selection and inherited gate/catalogue changes require a separate integration outside this closed write set.
5. The timed narration uses synthetic identifiers and an unfilled session placeholder; no fixtures or script establish live acceptance.

## Unified diff

```diff
diff --git a/connectors/cafe/mcp_demo_guide.py b/connectors/cafe/mcp_demo_guide.py
new file mode 100644
index 0000000..81ba7cc
--- /dev/null
+++ b/connectors/cafe/mcp_demo_guide.py
@@ -0,0 +1,145 @@
+"""Static demo contract proposed for later MCP registration; no live effects.
+
+This module does not connect the recorder, native confirmation provider, or
+runtime data to MCP. All observations remain pending. The unchanged server owns
+initialization, notifications, JSON-RPC framing, and response limits; a future
+integration must frame these flat objects there, without an inner envelope.
+"""
+
+_STEPS = (
+    (
+        "Invite a guest to a Discord call and silence music before isolation.",
+        "A quiet, clean call is ready to isolate; live verification is pending.",
+        "The operator sees the invited guest in the call and checks the audio "
+        "is quiet with music silenced; retain redacted acceptance evidence.",
+    ),
+    (
+        "Pop out the call window and select it through the host selector.",
+        "The host holds the selected window token; a title or executable name "
+        "alone is not its identity. Live verification is pending.",
+        "The operator sees the intended pop-out selected in the host selector; "
+        "keep the window token inside the host and omit private window lists.",
+    ),
+    (
+        "Establish the capsule identity and label game and camera feeds only "
+        "where those feeds are real.",
+        "The capsule identifier and generation are known, with real feeds "
+        "labelled game and camera. Live verification is pending.",
+        "The operator sees the capsule tile, its identifier and generation, "
+        "and the corresponding game or camera feed labels.",
+    ),
+    (
+        "When the native recording path is available, start recording, stop "
+        "recording, and verify the file in the operator-chosen folder. An AI "
+        "request is only a proposal requiring Confirm inside Café.",
+        "An operator-confirmed recording produces a verifiable file; this "
+        "guide supplies no recorder connection or completed effect.",
+        "The operator sees the file entry after stopping and opens it to "
+        "verify the intended content. File and native recording acceptance "
+        "remain pending; a queued proposal is not a completed recording.",
+    ),
+    (
+        "When native OBS integration is available, send the same capsule "
+        "window to OBS twice and inspect the source list. An AI request "
+        "requires Confirm inside Café before any effect.",
+        "A single Window Capture target is reused with no duplicate source; "
+        "live verification is pending.",
+        "The operator sees one intended Window Capture source before and "
+        "after the second send and verifies its pixels in OBS. Configuration "
+        "readback or a fixture alone does not establish native acceptance.",
+    ),
+    (
+        "During a separate live check, receive a Twitch message and inspect "
+        "its stored JSONL row using the existing chat event validator.",
+        "A typed chat row is validated against cafe.chat_event.v1; live "
+        "arrival and durable storage remain pending.",
+        "The operator sees the corresponding JSONL line and its schema "
+        "validation result, preserving canonical clock strings and unknown "
+        "null fields. This guide never reads or aggregates JSONL.",
+    ),
+    (
+        "Initialize the existing MCP session, send notifications/initialized, "
+        "list tools with tools/list, and perform typed read calls using the "
+        "six existing chat tools. Native control remains disabled without "
+        "a native provider.",
+        "The assistant receives tools/list and typed read results within "
+        "existing bounds. Live data access remains pending; this static "
+        "guide is not registered in the unchanged server.",
+        "The operator sees the tool list and bounded typed read result with "
+        "any scan limitations or closed error shown honestly. Tests cannot "
+        "establish live capture, recording, OBS, broadcast, or MCP control.",
+    ),
+)
+
+
+def _reply(step_ids, error):
+    return {
+        "schema": "cafe.demo_guide.v1",
+        "steps": [
+            {
+                "id": step_id,
+                "instruction": _STEPS[step_id - 1][0],
+                "expected": _STEPS[step_id - 1][1],
+                "evidence_required": _STEPS[step_id - 1][2],
+                "depends_on": list(range(1, step_id)),
+                "live_status": "pending",
+            }
+            for step_id in step_ids
+        ],
+        "control_mode": "human_confirmed",
+        "error": error,
+    }
+
+
+def tool_definitions() -> list[dict]:
+    """Describe only the proposed read-only demo_guide tool, without registering it."""
+    return [{
+        "name": "demo_guide",
+        "description": "Static seven-step demo checklist; all live evidence is pending.",
+        "inputSchema": {
+            "type": "object",
+            "properties": {"step": {"type": "integer", "minimum": 1, "maximum": 7}},
+            "required": [],
+            "additionalProperties": False,
+        },
+        "annotations": {
+            "readOnlyHint": True,
+            "destructiveHint": False,
+            "idempotentHint": True,
+            "openWorldHint": False,
+        },
+    }]
+
+
+def demo_guide(arguments: dict) -> dict:
+    """Return all steps or one integer step from 1 through 7.
+
+    Invalid input always returns the full flat shape with empty steps and
+    error="invalid_arguments"; it never returns a partial guide or success.
+    Only a plain dict with the optional plain integer step is accepted.
+    """
+    if type(arguments) is not dict or set(arguments) - {"step"}:
+        return _reply((), "invalid_arguments")
+    if "step" not in arguments:
+        return _reply(range(1, 8), None)
+    step = arguments["step"]
+    if type(step) is not int or not 1 <= step <= 7:
+        return _reply((), "invalid_arguments")
+    return _reply((step,), None)
+
+
+def resource_definitions() -> list[dict]:
+    """Describe the proposed static resource; this does not add a live endpoint."""
+    return [{
+        "uri": "cafe://demo/guide",
+        "name": "demo_guide",
+        "description": "Static demo guide with human confirmation and pending live checks.",
+        "mimeType": "application/json",
+    }]
+
+
+def read_resource(uri: str) -> dict:
+    """Return the full guide only for the exact URI, otherwise empty unknown_resource."""
+    if type(uri) is not str or uri != "cafe://demo/guide":
+        return _reply((), "unknown_resource")
+    return demo_guide({})
diff --git a/tests/integration/test_mcp_demo_guide.py b/tests/integration/test_mcp_demo_guide.py
new file mode 100644
index 0000000..0c55f9e
--- /dev/null
+++ b/tests/integration/test_mcp_demo_guide.py
@@ -0,0 +1,358 @@
+"""Offline contract checks for the static guide, never native acceptance.
+
+Only the harness reads source and submission files. The guide itself is tested
+with I/O and execution entry points disabled. No JSONL aggregation is exercised.
+Run: python3 -B tests/integration/test_mcp_demo_guide.py
+"""
+import ast
+import builtins
+from contextlib import ExitStack, redirect_stderr, redirect_stdout
+import copy
+import inspect
+import io
+import json
+import os
+from pathlib import Path
+import re
+import socket
+import subprocess
+import sys
+import unittest
+from unittest import mock
+import urllib.request
+
+ROOT = Path(__file__).resolve().parents[2]
+sys.path.insert(0, str(ROOT / 'connectors'))
+from cafe import mcp_demo_guide as guide
+
+BASE = 'f1e874050e305645a90e64267e50f8625daae9bc'
+BASE_URL = 'https://github.com/oguirado/cafe/blob/' + BASE + '/'
+# These six exact paths were read at BASE before this proposal was written.
+BASE_PATHS = {
+    'README.md', 'docs/acceptance/README.md', 'docs/schemas/chat_event.v1.json',
+    'connectors/cafe/mcp_server.py', 'connectors/cafe/mcp_control.py',
+    'core/src/runtime_main.rs',
+}
+MARKERS = [f'[PENDING-LIVE:S{number:02d}]' for number in range(1, 8)]
+RESPONSE_KEYS = {'schema', 'steps', 'control_mode', 'error'}
+STEP_KEYS = {'id', 'instruction', 'expected', 'evidence_required',
+             'depends_on', 'live_status'}
+
+
+def _deny_effect(*args, **kwargs):
+    raise AssertionError('guide_effect_attempted')
+
+
+class DemoGuide(unittest.TestCase):
+    def assert_payload(self, value, ids, error=None):
+        self.assertIs(type(value), dict)
+        self.assertEqual(set(value), RESPONSE_KEYS)
+        self.assertEqual(value['schema'], 'cafe.demo_guide.v1')
+        self.assertEqual(value['control_mode'], 'human_confirmed')
+        self.assertEqual(value['error'], error)
+        self.assertIs(type(value['steps']), list)
+        self.assertEqual([step['id'] for step in value['steps']], ids)
+        for step in value['steps']:
+            self.assertIs(type(step), dict)
+            self.assertEqual(set(step), STEP_KEYS)
+            self.assertIs(type(step['id']), int)
+            self.assertEqual(step['live_status'], 'pending')
+            self.assertIs(type(step['depends_on']), list)
+            self.assertEqual(step['depends_on'], list(range(1, step['id'])))
+            self.assertTrue(all(type(prior) is int for prior in step['depends_on']))
+            for name in ('instruction', 'expected', 'evidence_required'):
+                self.assertIs(type(step[name]), str)
+                self.assertTrue(step[name].strip())
+        # The payload is ordinary JSON data, without protocol envelopes or NaN.
+        self.assertEqual(json.loads(json.dumps(value, allow_nan=False)), value)
+
+    def test_exact_public_functions_and_frozen_signatures(self):
+        expected = {
+            'tool_definitions': ((), list[dict]),
+            'demo_guide': ((('arguments', dict),), dict),
+            'resource_definitions': ((), list[dict]),
+            'read_resource': ((('uri', str),), dict),
+        }
+        public = {name for name, value in vars(guide).items()
+                  if inspect.isfunction(value) and not name.startswith('_')}
+        self.assertEqual(public, set(expected))
+        for name, (parameters, result_type) in expected.items():
+            with self.subTest(function=name):
+                signature = inspect.signature(getattr(guide, name))
+                self.assertEqual(tuple(signature.parameters),
+                                 tuple(key for key, _ in parameters))
+                self.assertEqual(signature.return_annotation, result_type)
+                for key, annotation in parameters:
+                    parameter = signature.parameters[key]
+                    self.assertEqual(parameter.annotation, annotation)
+                    self.assertIs(parameter.default, inspect.Parameter.empty)
+                    self.assertEqual(parameter.kind,
+                                     inspect.Parameter.POSITIONAL_OR_KEYWORD)
+
+    def test_full_guide_has_closed_pending_shape_and_dependency_chain(self):
+        self.assert_payload(guide.demo_guide({}), list(range(1, 8)))
+
+    def test_each_single_step_matches_its_full_definition(self):
+        complete = guide.demo_guide({})
+        for number in range(1, 8):
+            with self.subTest(step=number):
+                selected = guide.demo_guide({'step': number})
+                self.assert_payload(selected, [number])
+                self.assertEqual(selected['steps'], [complete['steps'][number - 1]])
+
+    def test_steps_describe_the_required_operator_checks(self):
+        required_topics = (
+            ('discord', 'guest', 'music'),
+            ('window', 'host', 'token'),
+            ('capsule', 'generation', 'game', 'camera'),
+            ('start', 'stop', 'record', 'file', 'confirm'),
+            ('obs', 'twice', 'window capture', 'duplicate'),
+            ('twitch', 'jsonl', 'cafe.chat_event.v1'),
+            ('tools/list', 'typed', 'bounded'),
+        )
+        for step, topics in zip(guide.demo_guide({})['steps'], required_topics):
+            with self.subTest(step=step['id']):
+                text = ' '.join(step[name] for name in
+                                ('instruction', 'expected', 'evidence_required')).lower()
+                for topic in topics:
+                    self.assertIn(topic, text)
+                self.assertIn('operator', step['evidence_required'].lower())
+
+    def test_invalid_step_values_have_empty_closed_error(self):
+        class SyntheticInteger(int):
+            pass
+
+        invalid = (0, 8, -1, 10**100, True, False, None, '1', '01', 1.0,
+                   float('nan'), float('inf'), [], {}, (), SyntheticInteger(1))
+        for index, step in enumerate(invalid):
+            with self.subTest(case=index):
+                self.assert_payload(guide.demo_guide({'step': step}), [],
+                                    'invalid_arguments')
+
+    def test_non_objects_and_extra_fields_are_rejected(self):
+        class SyntheticObject(dict):
+            pass
+
+        invalid = (None, True, False, 1, 1.0, 'synthetic', [], (),
+                   SyntheticObject(), {'extra': 1}, {'step': 1, 'extra': 1},
+                   {'step': 1, 'from': None}, {1: 1})
+        for index, arguments in enumerate(invalid):
+            with self.subTest(case=index):
+                self.assert_payload(guide.demo_guide(arguments), [],
+                                    'invalid_arguments')
+        self.assertIn('empty steps', guide.demo_guide.__doc__)
+
+    def test_inputs_are_not_mutated_or_reflected_in_errors(self):
+        valid = {'step': 3}
+        invalid = {'step': 'synthetic_untrusted_value', 'extra': ['synthetic']}
+        for arguments in (valid, invalid):
+            before = copy.deepcopy(arguments)
+            result = guide.demo_guide(arguments)
+            self.assertEqual(arguments, before)
+            self.assertNotIn('synthetic_untrusted_value', json.dumps(result))
+
+    def test_resource_accepts_only_the_exact_string_uri(self):
+        self.assertEqual(guide.read_resource('cafe://demo/guide'),
+                         guide.demo_guide({}))
+        invalid = ('', 'cafe://demo/guide/', 'CAFE://demo/guide',
+                   'cafe://demo/Guide', 'cafe://demo/guide?step=1',
+                   'cafe://demo/guide#step', ' cafe://demo/guide',
+                   'cafe://demo/guide\n', 'cafe://demo/%67uide',
+                   None, True, 1, [], {}, b'cafe://demo/guide')
+        for index, uri in enumerate(invalid):
+            with self.subTest(case=index):
+                self.assert_payload(guide.read_resource(uri), [], 'unknown_resource')
+
+    def test_tool_metadata_has_one_closed_readonly_tool(self):
+        definitions = guide.tool_definitions()
+        self.assertIs(type(definitions), list)
+        self.assertEqual(len(definitions), 1)
+        definition = definitions[0]
+        self.assertEqual(definition['name'], 'demo_guide')
+        self.assertTrue(definition['description'].strip())
+        schema = definition['inputSchema']
+        self.assertEqual(schema['type'], 'object')
+        self.assertIs(schema['additionalProperties'], False)
+        self.assertEqual(schema.get('required', []), [])
+        self.assertEqual(set(schema['properties']), {'step'})
+        step = schema['properties']['step']
+        self.assertEqual(step['type'], 'integer')
+        self.assertEqual((step['minimum'], step['maximum']), (1, 7))
+        for key, value in {'readOnlyHint': True, 'destructiveHint': False,
+                           'idempotentHint': True, 'openWorldHint': False}.items():
+            self.assertIs(definition['annotations'][key], value)
+
+    def test_resource_metadata_has_one_static_json_resource(self):
+        definitions = guide.resource_definitions()
+        self.assertIs(type(definitions), list)
+        self.assertEqual(len(definitions), 1)
+        self.assertEqual(definitions[0]['uri'], 'cafe://demo/guide')
+        self.assertEqual(definitions[0]['name'], 'demo_guide')
+        self.assertEqual(definitions[0]['mimeType'], 'application/json')
+        self.assertTrue(definitions[0]['description'].strip())
+
+    def test_guide_and_resource_results_have_no_shared_mutable_state(self):
+        baseline = guide.demo_guide({})
+        changed = guide.demo_guide({})
+        changed['steps'][0]['instruction'] = 'synthetic mutation'
+        changed['steps'][1]['depends_on'].append(7)
+        changed['steps'].pop()
+        changed['control_mode'] = 'synthetic mutation'
+        resource = guide.read_resource('cafe://demo/guide')
+        resource['steps'][0]['live_status'] = 'synthetic mutation'
+        selected = guide.demo_guide({'step': 7})
+        selected['steps'][0]['depends_on'].clear()
+        self.assertEqual(guide.demo_guide({}), baseline)
+        self.assertEqual(guide.read_resource('cafe://demo/guide'), baseline)
+        failed = guide.demo_guide(None)
+        failed['steps'].append({'synthetic': True})
+        self.assert_payload(guide.demo_guide(None), [], 'invalid_arguments')
+
+    def test_metadata_returns_independent_nested_containers(self):
+        tools = guide.tool_definitions()
+        resources = guide.resource_definitions()
+        baseline_tools, baseline_resources = copy.deepcopy((tools, resources))
+        tools[0]['inputSchema']['properties']['step']['maximum'] = 99
+        tools[0]['inputSchema']['required'].append('synthetic')
+        tools[0]['annotations']['readOnlyHint'] = False
+        resources[0]['uri'] = 'synthetic mutation'
+        self.assertEqual(guide.tool_definitions(), baseline_tools)
+        self.assertEqual(guide.resource_definitions(), baseline_resources)
+
+    def test_source_has_no_imports_dynamic_execution_or_import_time_effects(self):
+        tree = ast.parse((ROOT / 'connectors/cafe/mcp_demo_guide.py').read_text(
+            encoding='utf-8'))
+        forbidden = {'open', 'exec', 'eval', 'compile', '__import__', 'print',
+                     'input', 'breakpoint', 'system', 'popen', 'spawn', 'Popen'}
+        for node in ast.walk(tree):
+            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom))
+            if isinstance(node, ast.Call):
+                name = (node.func.id if isinstance(node.func, ast.Name) else
+                        node.func.attr if isinstance(node.func, ast.Attribute) else None)
+                self.assertNotIn(name, forbidden)
+        for node in tree.body:
+            if isinstance(node, ast.FunctionDef):
+                self.assertEqual(node.decorator_list, [])
+                for default in node.args.defaults + [value for value in
+                        node.args.kw_defaults if value is not None]:
+                    ast.literal_eval(default)
+            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
+                ast.literal_eval(node.value)
+            else:
+                self.assertIsInstance(node, ast.Expr)
+                self.assertIsInstance(node.value, ast.Constant)
+                self.assertIs(type(node.value.value), str)
+
+    def test_import_and_all_function_paths_perform_no_effects(self):
+        source = (ROOT / 'connectors/cafe/mcp_demo_guide.py').read_text(encoding='utf-8')
+        compiled = compile(source, '<synthetic-guide-import>', 'exec')
+        execute_module = exec
+        namespace = {'__name__': 'synthetic_guide'}
+        stdout, stderr = io.StringIO(), io.StringIO()
+        targets = (
+            (builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'listdir'),
+            (os, 'scandir'), (os, 'system'), (os, 'popen'),
+            (Path, 'open'), (Path, 'iterdir'), (Path, 'glob'), (Path, 'rglob'),
+            (Path, 'read_text'), (Path, 'read_bytes'), (Path, 'write_text'),
+            (Path, 'write_bytes'), (socket, 'socket'), (socket, 'create_connection'),
+            (urllib.request, 'urlopen'), (subprocess, 'Popen'), (subprocess, 'run'),
+            (subprocess, 'call'), (subprocess, 'check_call'),
+            (subprocess, 'check_output'), (builtins, 'print'), (builtins, 'input'),
+            (builtins, 'eval'), (builtins, 'exec'), (builtins, 'compile'),
+            (builtins, '__import__'),
+        )
+        patches = [mock.patch.object(owner, name, _deny_effect) for owner, name in targets]
+        with ExitStack() as stack:
+            stack.enter_context(redirect_stdout(stdout))
+            stack.enter_context(redirect_stderr(stderr))
+            for patch in patches:
+                stack.enter_context(patch)
+            # Loading compiled source is a harness operation, not a guide API.
+            execute_module(compiled, namespace)
+            for functions in (vars(guide), namespace):
+                functions['tool_definitions']()
+                functions['resource_definitions']()
+                for arguments in ({}, *({'step': step} for step in range(1, 8)),
+                                  {'step': True}, {'step': 0}, {'step': 8},
+                                  {'step': 'synthetic'}, {'extra': None}, None, []):
+                    functions['demo_guide'](arguments)
+                for uri in ('cafe://demo/guide', 'cafe://demo/guide/', None, []):
+                    functions['read_resource'](uri)
+        self.assertEqual(stdout.getvalue(), '')
+        self.assertEqual(stderr.getvalue(), '')
+
+
+class SubmissionContract(unittest.TestCase):
+    @classmethod
+    def setUpClass(cls):
+        cls.documents = {
+            name: (ROOT / 'submission' / name).read_text(encoding='utf-8')
+            for name in ('DEVPOST.md', 'VIDEO_SCRIPT.md')
+        }
+
+    def test_all_seven_live_markers_remain_open_in_both_documents(self):
+        for name, text in self.documents.items():
+            with self.subTest(document=name):
+                markers = re.findall(r'\[[A-Z-]*LIVE:S[0-9]+\]', text)
+                self.assertEqual(markers, MARKERS)
+                self.assertNotRegex(text, r'~~\[PENDING-LIVE:S[0-9]+\]~~')
+                self.assertNotRegex(text, r'(?im)^.*\[[xX]\].*\[PENDING-LIVE:S[0-9]+\]')
+                self.assertNotRegex(text, r'(?i)live_status\s*[:=]\s*["\x27`]?'
+                                         r'(?:passed|verified|complete)')
+
+    def test_devpost_has_seven_aligned_pending_evidence_sections(self):
+        text = self.documents['DEVPOST.md']
+        sections = list(re.finditer(r'(?ms)^## S([0-9]{2})\b[^\n]*\n.*?(?=^## |\Z)', text))
+        self.assertEqual([int(section[1]) for section in sections], list(range(1, 8)))
+        for number, section in enumerate(sections, 1):
+            with self.subTest(step=number):
+                body = ' '.join(section[0].lower().split())
+                self.assertIn(MARKERS[number - 1], section[0])
+                self.assertIn('live status: pending', body)
+                self.assertIn('expected:', body)
+                self.assertRegex(body, r'required(?: operator-visible)? check:')
+
+    def test_script_segments_cover_exactly_180_seconds_in_step_order(self):
+        text = self.documents['VIDEO_SCRIPT.md']
+        segments = re.findall(
+            r'(?m)^## ([0-9]{1,3})-([0-9]{1,3}) seconds[^\n]*'
+            r'(\[PENDING-LIVE:S[0-9]{2}\])', text)
+        self.assertEqual(len(segments), 7)
+        cursor = 0
+        for number, (start, end, marker) in enumerate(segments, 1):
+            self.assertEqual(int(start), cursor)
+            self.assertGreater(int(end), int(start))
+            self.assertEqual(marker, MARKERS[number - 1])
+            cursor = int(end)
+        self.assertEqual(cursor, 180)
+        self.assertIn('demo_channel', text)
+        self.assertIn('<CODEX_SESSION_ID>', text)
+        self.assertIn('synthetic', text.lower())
+
+    def test_submission_preserves_confirmation_and_integration_boundaries(self):
+        for name, text in self.documents.items():
+            with self.subTest(document=name):
+                normalized = ' '.join(text.replace('**', '').replace('> ', '').split()).lower()
+                for phrase in ('proposal', 'confirm inside café',
+                               'control remains disabled without a native provider',
+                               'not registered', 'pending'):
+                    self.assertIn(phrase, normalized)
+
+    def test_all_links_resolve_to_reviewed_paths_at_the_immutable_base(self):
+        observed = set()
+        for name, text in self.documents.items():
+            # Check ordinary Markdown links and bare/autolink HTTP URLs.
+            links = re.findall(r'\[[^\]\n]+\]\(([^)\s]+)\)', text)
+            links += re.findall(r'https?://[^\s)>`]+', text)
+            for link in links:
+                with self.subTest(document=name, link=link):
+                    self.assertTrue(link.startswith(BASE_URL))
+                    path = link.removeprefix(BASE_URL)
+                    self.assertIn(path, BASE_PATHS)
+                    self.assertTrue((ROOT / path).is_file())
+                    observed.add(path)
+        self.assertEqual(observed, BASE_PATHS)
+
+
+if __name__ == '__main__':
+    unittest.main()
diff --git a/submission/DEVPOST.md b/submission/DEVPOST.md
new file mode 100644
index 0000000..50e5339
--- /dev/null
+++ b/submission/DEVPOST.md
@@ -0,0 +1,160 @@
+# Café — a human-confirmed demonstration guide
+
+## Inspiration and purpose
+
+Streamers need to keep a guest's call, separate feeds, recording, scene setup and
+chat context understandable while staying in control. Café's intended workflow
+groups host-selected call windows into capsules and makes typed chat reads
+available to assistants. This submission supplies a static, inspectable guide to
+that workflow. Its seven live checkpoints remain pending.
+
+## What is available in the fixed base
+
+This account describes commit `f1e874050e305645a90e64267e50f8625daae9bc`.
+The [project status](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/README.md)
+describes a Windows application with an Electron shell, Rust authority and Python
+connectors, with development coverage and production acceptance still pending.
+The [acceptance record](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/docs/acceptance/README.md)
+distinguishes portable fixtures, hosted synthetic windows and actual native
+observations. It records no live Discord acceptance receipt.
+
+The existing [MCP server](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/connectors/cafe/mcp_server.py)
+defines exactly these six chat tools:
+
+| Tool | Typed input and existing bound |
+| --- | --- |
+| `list_channels_and_days` | Empty object. |
+| `recent_messages` | Required channel; optional limit 1–200 and UTC since. |
+| `search_messages` | Required channel and literal query of 1–512 characters; optional limit 1–200. |
+| `top_chatters` | Required channel and window_minutes 1–1440. |
+| `message_rate` | Required channel and bucket_seconds 1–3600. |
+| `session_summary` | Required channel and calendar-valid day. |
+
+The server uses newline-delimited JSON-RPC over stdio, protocol version
+`2025-11-25`, an initialize request followed by notifications/initialized, and
+then tools/list and tools/call. Notifications receive no response. Existing
+request and response caps are 65,536 bytes; an event line is bounded at 131,072
+bytes, a scan at 33,554,432 bytes and a file traversal at 4,096 entries.
+JSON-RPC errors remain -32700 for parse errors, -32600 for invalid requests,
+-32601 for unknown methods and -32602 for invalid parameters. A data failure or
+oversized tool result uses the closed `data_unavailable_or_result_too_large`
+tool error. This proposal changes none of those rules or limits.
+
+The [control implementation](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/connectors/cafe/mcp_control.py)
+defines `select_capsule`, `start_recording`, `stop_recording` and `send_to_obs`.
+These are existing conditional control names, not four new guide tools. The
+default server does not advertise them without a supplied control plane.
+The [runtime projection](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/core/src/runtime_main.rs)
+sets `mcp_control_available` to false. Recording branches and connector fixtures
+exist, but the recorder, native confirmation and live runtime data are not
+connected through MCP in this base. Injecting a provider in a test does not
+establish that product connection.
+
+## What this submission adds
+
+The proposed `demo_guide` tool and `cafe://demo/guide` resource return static
+definitions. They are **not registered in the unchanged base server**.
+`tool_definitions()` and `resource_definitions()` describe this proposed surface;
+`demo_guide(arguments)` and `read_resource(uri)` return its flat data object.
+The schema is `cafe.demo_guide.v1`, the control mode is `human_confirmed`, and
+every step has `live_status: "pending"`. A successful full read contains seven
+ordered steps. An optional integer step selects one definition. Invalid tool
+arguments return empty steps and `invalid_arguments`; an unrecognized resource
+returns empty steps and `unknown_resource`. Neither error executes anything.
+
+The guide performs no disk reads, directory scans, JSONL parsing, network access,
+process launch or control effects. A future, separately reviewed server hook
+would register its metadata, dispatch its functions and apply the existing
+outer MCP framing and response cap. The guide's own payload remains flat.
+Resource registration and that dispatcher hook are outside this submission.
+
+## Human control and data boundaries
+
+Any recording or OBS action requested by an AI is only a proposal that runs after
+the operator clicks **Confirm inside Café**. **Control remains disabled without
+a native provider.** The required confirmation boundary is not a claim that the
+native bridge is already connected. A queued proposal is not a completed effect.
+Confirmation material stays outside model, renderer, file and log outputs.
+The renderer only renders and submits intents over closed typed IPC; chat text
+is untrusted data and never executable instructions.
+
+The [chat event schema](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/docs/schemas/chat_event.v1.json)
+is a closed object tagged `cafe.chat_event.v1`. Its received_at object holds a
+canonical decimal-string monotonic_ns and a calendar-valid UTC timestamp.
+UTC uses `YYYY-MM-DDTHH:MM:SS[.fraction]Z`, with one through nine fractional digits
+when present. Decimal u64 clocks match `0|[1-9][0-9]{0,19}` and cannot exceed
+18446744073709551615. Monotonic readings compare only within the same session.
+Unknown optional values remain null; unknown data is never replaced with a
+fabricated total. Extra fields and wrong types are rejected, including booleans
+where integers are required. Any future explicit time-range contract must keep
+the half-open interval `[from,to)`. The guide accepts no timestamps or ranges.
+
+Demonstration identifiers are synthetic: `demo_channel` is a synthetic channel
+label, and `<CODEX_SESSION_ID>` is an unfilled session placeholder. No private
+captures, account details, user paths or credentials belong in this submission.
+
+## S01 — Prepare a quiet call [PENDING-LIVE:S01]
+
+**Live status: pending.** Invite a guest to a Discord call and silence music.
+Expected: a quiet, clean call ready to isolate. Required operator-visible check:
+hear the guest clearly and confirm that background music is silent. This is a
+future acceptance observation, not a fixture result.
+
+## S02 — Select the popped-out window [PENDING-LIVE:S02]
+
+**Live status: pending.** After S01, pop out the call window and choose it through
+the host selector. Expected: the host holds the window token; a title or
+executable name is not its identity. Required check: see the intended selection
+in the host selector without publishing raw handles or window lists.
+
+## S03 — Establish the capsule [PENDING-LIVE:S03]
+
+**Live status: pending.** After S02, establish the capsule identifier and
+generation. Label actual feeds game and camera only where those feeds exist.
+Expected: stable capsule identity with the intended feed assignments. Required
+check: see the capsule tile, its identifier and generation, and the real feed
+labels. Missing feeds remain missing.
+
+## S04 — Record, stop and verify the file [PENDING-LIVE:S04]
+
+**Live status: pending.** After S03 and only when the native prerequisites are
+available, confirm start recording, then confirm stop recording, and inspect
+the resulting file in the operator-chosen folder. Expected: operator-confirmed
+recording produces a verifiable file. Required check: see the file entry and
+open the file to verify the intended content. A queued request, fixture artifact
+or guide response is not acceptance of recording or file publication.
+
+## S05 — Reuse one OBS target [PENDING-LIVE:S05]
+
+**Live status: pending.** After S04 and native configuration, send the same
+capsule to OBS twice through operator-confirmed actions. Expected: one Window
+Capture target is reused without a duplicate source. Required check: inspect
+the OBS source list after both attempts and verify the intended pixels.
+Configuration readback alone does not establish capture or broadcast acceptance.
+
+## S06 — Observe a typed chat row [PENDING-LIVE:S06]
+
+**Live status: pending.** After S05, observe a live Twitch message and its durable
+JSONL row in an authorized demonstration. Expected: a typed row valid against
+the existing chat event schema. Required check: see the received message and
+the corresponding schema-validated JSONL line with private details redacted.
+This static guide does not parse, aggregate, store or fabricate a chat row.
+
+## S07 — Inspect bounded assistant reads [PENDING-LIVE:S07]
+
+**Live status: pending.** After S06, complete MCP initialization, inspect
+tools/list, and perform typed reads such as `recent_messages` with the synthetic
+channel label and a limit of 5. Expected: the existing tools return bounded typed
+results from authorized data. Required check: see the tool list, typed arguments,
+result and scan metadata, or the honest closed error. An unavailable data source
+must not be described as successful live runtime integration.
+
+## What remains to demonstrate
+
+All seven checkpoints are unresolved. The narration and guide define the
+operator-visible evidence required; neither is a live receipt. Contract tests
+can establish shapes, rejections, static content and absence of effects. They
+cannot accept native capture, recording, file publication, OBS pixels,
+broadcasting or MCP control. Future work must connect the reviewed native
+providers and record actual redacted Windows acceptance before making those
+claims. Test execution and CI outcomes are reported separately from this pitch.
diff --git a/submission/VIDEO_SCRIPT.md b/submission/VIDEO_SCRIPT.md
new file mode 100644
index 0000000..5a992fa
--- /dev/null
+++ b/submission/VIDEO_SCRIPT.md
@@ -0,0 +1,88 @@
+# Café — 180-second narration
+
+Production direction: seven consecutive segments cover exactly 180 seconds.
+Read only the quoted narration; allow short pauses within each segment. The
+visual plan uses Café text cards and synthetic labels only, with no private
+captures, real account or channel names, partner branding, or simulated recording.
+`demo_channel` is explicitly synthetic. `<CODEX_SESSION_ID>` remains an unfilled
+placeholder. Café is the sole project identity; named applications are workflow
+references. Keep each pending marker visible throughout its segment.
+
+Source context: the fixed-base [project status](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/README.md)
+and [acceptance boundaries](https://github.com/oguirado/cafe/blob/f1e874050e305645a90e64267e50f8625daae9bc/docs/acceptance/README.md).
+This script is a planned narration, not a recording or live acceptance receipt.
+
+## 000-025 seconds — S01: Quiet call [PENDING-LIVE:S01]
+
+Visual: Café title, "Synthetic demonstration plan", and S01 pending card.
+
+> Café is a Windows desktop project for keeping guest feeds and chat context
+> understandable. This is its demonstration plan, with every live checkpoint
+> still pending. First, invite a guest into a Discord call and silence music.
+> The operator must hear a clean, quiet call before continuing. Our text cards
+> show the checks to perform; they are not captured call footage.
+
+## 025-050 seconds — S02: Host selection [PENDING-LIVE:S02]
+
+Visual: S02 pending card with the words "Pop out", "Host selector", "Host token".
+
+> Next, pop out the call window and select it through the host selector. The
+> intended identity is a window token held by the host, not a title or an
+> executable name. The live check is to see the correct host selection. We do
+> not show private window lists or raw handles, and this selection checkpoint
+> remains pending until an operator observes it.
+
+## 050-075 seconds — S03: Capsule identity [PENDING-LIVE:S03]
+
+Visual: S03 pending card listing "Identifier", "Generation", "Actual feed labels".
+
+> The selected window belongs to a capsule with a known identifier and
+> generation. Label feeds game and camera only when those feeds really exist.
+> The operator's evidence is the capsule tile and its actual assignments.
+> Missing information stays unknown. Every label shown here is synthetic,
+> and the session field remains an unfilled placeholder. This card does not
+> establish live capsule acceptance.
+
+## 075-105 seconds — S04: Confirm recording [PENDING-LIVE:S04]
+
+Visual: S04 pending card; "Proposal", "Confirm inside Café", "Verify file".
+
+> Recording requires a real native path. Any AI recording or OBS request is
+> only a proposal that runs after the operator clicks Confirm inside Café.
+> Control remains disabled without a native provider. The recorder and native
+> confirmation are not connected through MCP in this base. The required live
+> check is to start, stop, see the file entry and open the resulting file.
+> A queued proposal is not a completed effect.
+
+## 105-130 seconds — S05: OBS reuse [PENDING-LIVE:S05]
+
+Visual: S05 pending card reading "Two requests; one intended Window Capture".
+
+> With the required native configuration, send the same capsule to OBS twice.
+> The expected observation is a single Window Capture target reused without
+> a duplicate source. Inspect the source list after both attempts and verify
+> the intended pixels. Existing connector fixtures exercise reconciliation,
+> but this narration proves no live OBS action, recording, capture or broadcast.
+> The operator's evidence remains pending.
+
+## 130-155 seconds — S06: Typed chat [PENDING-LIVE:S06]
+
+Visual: S06 pending card, synthetic `demo_channel`, literal `<CODEX_SESSION_ID>`.
+
+> Next, the operator must observe a live Twitch message and its stored JSONL
+> line. The expected row follows the existing chat event schema, with strict
+> UTC time and canonical decimal clocks. Unknown optional values remain null.
+> Our channel label is synthetic, and the session placeholder is unfilled.
+> No chat row is fabricated by the guide, and this live checkpoint remains
+> pending.
+
+## 155-180 seconds — S07: Bounded reads [PENDING-LIVE:S07]
+
+Visual: S07 pending card; tools/list, `recent_messages`, "Bounded typed result".
+
+> Finally, initialize MCP, list its tools and request bounded typed chat reads.
+> The base defines six chat tools. The operator must inspect their arguments,
+> results and scan metadata, including honest errors when data is unavailable.
+> The proposed demo guide is static and not registered in that server.
+> These seven checkpoints remain pending. This video plan is not evidence
+> that live capture or control works.
```
