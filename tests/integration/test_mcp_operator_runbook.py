"""Offline contract checks for the static guide, never native acceptance.

Only the harness reads source and submission files. The guide itself is tested
with I/O and execution entry points disabled. No JSONL aggregation is exercised.
Run: python3 -B tests/integration/test_mcp_operator_runbook.py
"""
import ast
import builtins
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import inspect
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import mcp_operator_runbook as guide

BASE = 'f1e874050e305645a90e64267e50f8625daae9bc'
BASE_URL = 'https://github.com/oguirado/cafe/blob/' + BASE + '/'
# These six exact paths were read at BASE before this proposal was written.
BASE_PATHS = {
    'README.md', 'docs/acceptance/README.md', 'docs/schemas/chat_event.v1.json',
    'connectors/cafe/mcp_server.py', 'connectors/cafe/mcp_control.py',
    'core/src/runtime_main.rs',
}
MARKERS = [f'[PENDING-LIVE:S{number:02d}]' for number in range(1, 8)]
RESPONSE_KEYS = {'schema', 'steps', 'control_mode', 'error'}
STEP_KEYS = {'id', 'instruction', 'expected', 'evidence_required',
             'depends_on', 'live_status'}


def _deny_effect(*args, **kwargs):
    raise AssertionError('guide_effect_attempted')


class DemoGuide(unittest.TestCase):
    def assert_payload(self, value, ids, error=None):
        self.assertIs(type(value), dict)
        self.assertEqual(set(value), RESPONSE_KEYS)
        self.assertEqual(value['schema'], 'cafe.demo_guide.v1')
        self.assertEqual(value['control_mode'], 'human_confirmed')
        self.assertEqual(value['error'], error)
        self.assertIs(type(value['steps']), list)
        self.assertEqual([step['id'] for step in value['steps']], ids)
        for step in value['steps']:
            self.assertIs(type(step), dict)
            self.assertEqual(set(step), STEP_KEYS)
            self.assertIs(type(step['id']), int)
            self.assertEqual(step['live_status'], 'pending')
            self.assertIs(type(step['depends_on']), list)
            self.assertEqual(step['depends_on'], list(range(1, step['id'])))
            self.assertTrue(all(type(prior) is int for prior in step['depends_on']))
            for name in ('instruction', 'expected', 'evidence_required'):
                self.assertIs(type(step[name]), str)
                self.assertTrue(step[name].strip())
        # The payload is ordinary JSON data, without protocol envelopes or NaN.
        self.assertEqual(json.loads(json.dumps(value, allow_nan=False)), value)

    def test_exact_public_functions_and_frozen_signatures(self):
        expected = {
            'tool_definitions': ((), list[dict]),
            'demo_guide': ((('arguments', dict),), dict),
            'resource_definitions': ((), list[dict]),
            'read_resource': ((('uri', str),), dict),
        }
        public = {name for name, value in vars(guide).items()
                  if inspect.isfunction(value) and not name.startswith('_')}
        self.assertEqual(public, set(expected))
        for name, (parameters, result_type) in expected.items():
            with self.subTest(function=name):
                signature = inspect.signature(getattr(guide, name))
                self.assertEqual(tuple(signature.parameters),
                                 tuple(key for key, _ in parameters))
                self.assertEqual(signature.return_annotation, result_type)
                for key, annotation in parameters:
                    parameter = signature.parameters[key]
                    self.assertEqual(parameter.annotation, annotation)
                    self.assertIs(parameter.default, inspect.Parameter.empty)
                    self.assertEqual(parameter.kind,
                                     inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_full_guide_has_closed_pending_shape_and_dependency_chain(self):
        self.assert_payload(guide.demo_guide({}), list(range(1, 8)))

    def test_each_single_step_matches_its_full_definition(self):
        complete = guide.demo_guide({})
        for number in range(1, 8):
            with self.subTest(step=number):
                selected = guide.demo_guide({'step': number})
                self.assert_payload(selected, [number])
                self.assertEqual(selected['steps'], [complete['steps'][number - 1]])

    def test_steps_describe_the_required_operator_checks(self):
        required_topics = (
            ('discord', 'guest', 'music'),
            ('window', 'host', 'token'),
            ('capsule', 'generation', 'game', 'camera'),
            ('start', 'stop', 'record', 'file', 'confirm'),
            ('obs', 'twice', 'window capture', 'duplicate'),
            ('twitch', 'jsonl', 'cafe.chat_event.v1'),
            ('tools/list', 'typed', 'bounded'),
        )
        for step, topics in zip(guide.demo_guide({})['steps'], required_topics):
            with self.subTest(step=step['id']):
                text = ' '.join(step[name] for name in
                                ('instruction', 'expected', 'evidence_required')).lower()
                for topic in topics:
                    self.assertIn(topic, text)
                self.assertIn('operator', step['evidence_required'].lower())

    def test_invalid_step_values_have_empty_closed_error(self):
        class SyntheticInteger(int):
            pass

        invalid = (0, 8, -1, 10**100, True, False, None, '1', '01', 1.0,
                   float('nan'), float('inf'), [], {}, (), SyntheticInteger(1))
        for index, step in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_payload(guide.demo_guide({'step': step}), [],
                                    'invalid_arguments')

    def test_non_objects_and_extra_fields_are_rejected(self):
        class SyntheticObject(dict):
            pass

        invalid = (None, True, False, 1, 1.0, 'synthetic', [], (),
                   SyntheticObject(), {'extra': 1}, {'step': 1, 'extra': 1},
                   {'step': 1, 'from': None}, {1: 1})
        for index, arguments in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_payload(guide.demo_guide(arguments), [],
                                    'invalid_arguments')
        self.assertIn('empty steps', guide.demo_guide.__doc__)

    def test_inputs_are_not_mutated_or_reflected_in_errors(self):
        valid = {'step': 3}
        invalid = {'step': 'synthetic_untrusted_value', 'extra': ['synthetic']}
        for arguments in (valid, invalid):
            before = copy.deepcopy(arguments)
            result = guide.demo_guide(arguments)
            self.assertEqual(arguments, before)
            self.assertNotIn('synthetic_untrusted_value', json.dumps(result))

    def test_resource_accepts_only_the_exact_string_uri(self):
        self.assertEqual(guide.read_resource('cafe://demo/guide'),
                         guide.demo_guide({}))
        invalid = ('', 'cafe://demo/guide/', 'CAFE://demo/guide',
                   'cafe://demo/Guide', 'cafe://demo/guide?step=1',
                   'cafe://demo/guide#step', ' cafe://demo/guide',
                   'cafe://demo/guide\n', 'cafe://demo/%67uide',
                   None, True, 1, [], {}, b'cafe://demo/guide')
        for index, uri in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_payload(guide.read_resource(uri), [], 'unknown_resource')

    def test_tool_metadata_has_one_closed_readonly_tool(self):
        definitions = guide.tool_definitions()
        self.assertIs(type(definitions), list)
        self.assertEqual(len(definitions), 1)
        definition = definitions[0]
        self.assertEqual(definition['name'], 'demo_guide')
        self.assertTrue(definition['description'].strip())
        schema = definition['inputSchema']
        self.assertEqual(schema['type'], 'object')
        self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(schema.get('required', []), [])
        self.assertEqual(set(schema['properties']), {'step'})
        step = schema['properties']['step']
        self.assertEqual(step['type'], 'integer')
        self.assertEqual((step['minimum'], step['maximum']), (1, 7))
        for key, value in {'readOnlyHint': True, 'destructiveHint': False,
                           'idempotentHint': True, 'openWorldHint': False}.items():
            self.assertIs(definition['annotations'][key], value)

    def test_resource_metadata_has_one_static_json_resource(self):
        definitions = guide.resource_definitions()
        self.assertIs(type(definitions), list)
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0]['uri'], 'cafe://demo/guide')
        self.assertEqual(definitions[0]['name'], 'demo_guide')
        self.assertEqual(definitions[0]['mimeType'], 'application/json')
        self.assertTrue(definitions[0]['description'].strip())

    def test_guide_and_resource_results_have_no_shared_mutable_state(self):
        baseline = guide.demo_guide({})
        changed = guide.demo_guide({})
        changed['steps'][0]['instruction'] = 'synthetic mutation'
        changed['steps'][1]['depends_on'].append(7)
        changed['steps'].pop()
        changed['control_mode'] = 'synthetic mutation'
        resource = guide.read_resource('cafe://demo/guide')
        resource['steps'][0]['live_status'] = 'synthetic mutation'
        selected = guide.demo_guide({'step': 7})
        selected['steps'][0]['depends_on'].clear()
        self.assertEqual(guide.demo_guide({}), baseline)
        self.assertEqual(guide.read_resource('cafe://demo/guide'), baseline)
        failed = guide.demo_guide(None)
        failed['steps'].append({'synthetic': True})
        self.assert_payload(guide.demo_guide(None), [], 'invalid_arguments')

    def test_metadata_returns_independent_nested_containers(self):
        tools = guide.tool_definitions()
        resources = guide.resource_definitions()
        baseline_tools, baseline_resources = copy.deepcopy((tools, resources))
        tools[0]['inputSchema']['properties']['step']['maximum'] = 99
        tools[0]['inputSchema']['required'].append('synthetic')
        tools[0]['annotations']['readOnlyHint'] = False
        resources[0]['uri'] = 'synthetic mutation'
        self.assertEqual(guide.tool_definitions(), baseline_tools)
        self.assertEqual(guide.resource_definitions(), baseline_resources)

    def test_source_has_no_imports_dynamic_execution_or_import_time_effects(self):
        tree = ast.parse((ROOT / 'connectors/cafe/mcp_operator_runbook.py').read_text(
            encoding='utf-8'))
        forbidden = {'open', 'exec', 'eval', 'compile', '__import__', 'print',
                     'input', 'breakpoint', 'system', 'popen', 'spawn', 'Popen'}
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom))
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name) else
                        node.func.attr if isinstance(node.func, ast.Attribute) else None)
                self.assertNotIn(name, forbidden)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.assertEqual(node.decorator_list, [])
                for default in node.args.defaults + [value for value in
                        node.args.kw_defaults if value is not None]:
                    ast.literal_eval(default)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                ast.literal_eval(node.value)
            else:
                self.assertIsInstance(node, ast.Expr)
                self.assertIsInstance(node.value, ast.Constant)
                self.assertIs(type(node.value.value), str)

    def test_import_and_all_function_paths_perform_no_effects(self):
        source = (ROOT / 'connectors/cafe/mcp_operator_runbook.py').read_text(encoding='utf-8')
        compiled = compile(source, '<synthetic-guide-import>', 'exec')
        execute_module = exec
        namespace = {'__name__': 'synthetic_guide'}
        stdout, stderr = io.StringIO(), io.StringIO()
        targets = (
            (builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'listdir'),
            (os, 'scandir'), (os, 'system'), (os, 'popen'),
            (Path, 'open'), (Path, 'iterdir'), (Path, 'glob'), (Path, 'rglob'),
            (Path, 'read_text'), (Path, 'read_bytes'), (Path, 'write_text'),
            (Path, 'write_bytes'), (socket, 'socket'), (socket, 'create_connection'),
            (urllib.request, 'urlopen'), (subprocess, 'Popen'), (subprocess, 'run'),
            (subprocess, 'call'), (subprocess, 'check_call'),
            (subprocess, 'check_output'), (builtins, 'print'), (builtins, 'input'),
            (builtins, 'eval'), (builtins, 'exec'), (builtins, 'compile'),
            (builtins, '__import__'),
        )
        patches = [mock.patch.object(owner, name, _deny_effect) for owner, name in targets]
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            for patch in patches:
                stack.enter_context(patch)
            # Loading compiled source is a harness operation, not a guide API.
            execute_module(compiled, namespace)
            for functions in (vars(guide), namespace):
                functions['tool_definitions']()
                functions['resource_definitions']()
                for arguments in ({}, *({'step': step} for step in range(1, 8)),
                                  {'step': True}, {'step': 0}, {'step': 8},
                                  {'step': 'synthetic'}, {'extra': None}, None, []):
                    functions['demo_guide'](arguments)
                for uri in ('cafe://demo/guide', 'cafe://demo/guide/', None, []):
                    functions['read_resource'](uri)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')


class SubmissionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {
            name: (ROOT / 'submission' / name).read_text(encoding='utf-8')
            for name in ('DEVPOST.md', 'VIDEO_SCRIPT.md')
        }

    def test_documents_keep_the_operator_confirmation_boundary(self):
        for name, text in self.documents.items():
            with self.subTest(document=name):
                normalized = ' '.join(text.replace('**', '').replace('> ', '').split()).lower()
                self.assertIn('confirm', normalized)
                self.assertNotIn('pending-live', normalized)

    def test_devpost_separates_verified_from_known_limits(self):
        text = ' '.join(self.documents['DEVPOST.md'].lower().split())
        self.assertIn('verified', text)
        self.assertIn('known limits', text)
        self.assertIn('not fully covered', text)

    def test_repository_links_point_at_existing_paths(self):
        prefix = 'https://github.com/oguirado/cafe/blob/'
        for name, text in self.documents.items():
            for link in re.findall(r'https://github\.com/oguirado/cafe/blob/[^\s)>`]+', text):
                with self.subTest(document=name, link=link):
                    path = link.removeprefix(prefix).split('/', 1)[1]
                    self.assertTrue((ROOT / path).is_file())


if __name__ == '__main__':
    unittest.main()
