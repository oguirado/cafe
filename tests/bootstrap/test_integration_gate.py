"""One-command integration evidence and exceptional cleanup regressions."""
import ast
import inspect
import types
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('cafe_integration_runner', ROOT / 'tools/run_integration_gate.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
QSPEC = importlib.util.spec_from_file_location('cafe_integration_gate_qualification', ROOT / 'tools/qualify_cafe_router.py')
q = importlib.util.module_from_spec(QSPEC)
sys.modules[QSPEC.name] = q
QSPEC.loader.exec_module(q)


def fixture_binary(target):
    # Production currently declares the path inline, not through a helper.
    # Reuse that exact expression; do not copy its platform/suffix rule here.
    tree = ast.parse(inspect.getsource(gate.run_gate))
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'binary' for t in node.targets)]
    if len(assignments) != 1:
        raise AssertionError('gate fixture declaration must be unique')
    expression = ast.Expression(assignments[0].value)
    return eval(compile(expression, '<gate-fixture-expression>', 'eval'),
                {'__builtins__': {}, 'target': target, 'os': gate.os})


class TestIntegrationGate(unittest.TestCase):
    def test_service_suite_uses_owned_platform_fixture_and_cleans(self):
        selected = []
        def run(command, root, env):
            if 'build' in command:
                binary = fixture_binary(Path(env['CARGO_TARGET_DIR']))
                binary.parent.mkdir(parents=True)
                binary.touch()
                return subprocess.CompletedProcess(command, 0, '', '')
            self.assertTrue(Path(env['CAFE_AUTHORITY_FIXTURE']).is_file())
            selected.append(command[-1])
            return subprocess.CompletedProcess(command, 0, '', 'Ran 23 tests in 0.01s\n\nOK\n')
        with mock.patch.object(gate, 'configuration', return_value=('cargo', {})), mock.patch.object(gate, 'run_owned', side_effect=run), mock.patch('sys.stderr', io.StringIO()):
            report = gate.run_gate(ROOT, services=True)
        self.assertEqual(selected, ['test_runtime_service*.py'])
        self.assertEqual(report['status'], 'pass')
        self.assertTrue(report['cleanup'])

    def test_target_owned_and_fixture_selected_without_caller_export(self):
        observed = []
        def run(command, root, env):
            observed.append(dict(env))
            if 'build' in command:
                binary = fixture_binary(Path(env['CARGO_TARGET_DIR']))
                binary.parent.mkdir(parents=True)
                binary.touch()
                return subprocess.CompletedProcess(command, 0, '', '')
            self.assertTrue(Path(env['CAFE_AUTHORITY_FIXTURE']).is_file())
            return subprocess.CompletedProcess(command, 0, '', 'Ran 11 tests in 0.01s\n\nOK\n')
        with mock.patch.object(gate, 'configuration', return_value=('cargo', {})), mock.patch.object(gate, 'run_owned', side_effect=run), mock.patch('sys.stderr', io.StringIO()):
            report = gate.run_gate(ROOT)
        self.assertEqual(report, {'schema': 'cafe.integration-gate.v1', 'status': 'pass', 'build_rc': 0, 'test_rc': 0, 'tests': 11, 'cleanup': True})
        self.assertEqual(observed[0]['CARGO_TARGET_DIR'], observed[1]['CARGO_TARGET_DIR'])
        self.assertFalse(Path(observed[0]['CARGO_TARGET_DIR']).exists())

    def test_build_failure_exception_and_interrupt_clean_owned_paths(self):
        for outcome in ('build_fail', 'exception', 'interrupt'):
            paths = []
            def run(command, root, env):
                paths.append(Path(env['CARGO_TARGET_DIR']).parent)
                if outcome == 'exception':
                    raise OSError('synthetic failure')
                if outcome == 'interrupt':
                    raise KeyboardInterrupt
                return subprocess.CompletedProcess(command, 1, '', 'failed build')
            with self.subTest(outcome=outcome), mock.patch.object(gate, 'configuration', return_value=('cargo', {})), mock.patch.object(gate, 'run_owned', side_effect=run), mock.patch('sys.stderr', io.StringIO()):
                if outcome == 'interrupt':
                    with self.assertRaises(KeyboardInterrupt):
                        gate.run_gate(ROOT)
                else:
                    report = gate.run_gate(ROOT)
                    self.assertEqual(report['status'], 'fail')
                    self.assertTrue(report['cleanup'])
            self.assertTrue(paths)
            self.assertTrue(all(not path.exists() for path in paths))

    def test_failed_empty_skipped_and_unverified_suite_never_pass(self):
        for rc, text in [(1, 'Ran 1 test in 0.01s\nFAILED (failures=1)\n'), (0, 'Ran 0 tests in 0.01s\nOK\n'), (0, 'Ran 1 test in 0.01s\nOK (skipped=1)\n'), (0, 'OK\n')]:
            paths = []
            def run(command, root, env):
                paths.append(Path(env['CARGO_TARGET_DIR']).parent)
                if 'build' in command:
                    binary = fixture_binary(Path(env['CARGO_TARGET_DIR']))
                    binary.parent.mkdir(parents=True)
                    binary.touch()
                    return subprocess.CompletedProcess(command, 0, '', '')
                return subprocess.CompletedProcess(command, rc, '', text)
            with self.subTest(text=text), mock.patch.object(gate, 'configuration', return_value=('cargo', {})), mock.patch.object(gate, 'run_owned', side_effect=run), mock.patch('sys.stderr', io.StringIO()):
                report = gate.run_gate(ROOT)
            self.assertEqual(report['status'], 'fail')
            self.assertTrue(report['cleanup'])
            self.assertTrue(all(not path.exists() for path in paths))

    def test_configuration_clears_caller_fixture_and_shared_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cargo = root / 'cargo'
            cargo.touch()
            config = root / 'config.json'
            config.write_text(json.dumps({'cargo': str(cargo)}))
            with mock.patch.dict(os.environ, {'CAFE_AUTHORITY_FIXTURE': 'wrong', 'CARGO_TARGET_DIR': 'shared'}):
                selected, env = gate.configuration(root, config)
            self.assertEqual(selected, str(cargo))
            self.assertNotIn('CAFE_AUTHORITY_FIXTURE', env)
            self.assertNotIn('CARGO_TARGET_DIR', env)
            self.assertEqual(env['CARGO_NET_OFFLINE'], 'true')

    def test_qualifier_calls_one_command_and_requires_cleanup_evidence(self):
        summary = {'schema': 'cafe.integration-gate.v1', 'status': 'pass', 'build_rc': 0, 'test_rc': 0, 'tests': 11, 'cleanup': True}
        for fields, rc, expected in [({}, 0, True), ({'cleanup': False}, 0, False), ({'status': 'fail'}, 0, False), ({'tests': 0}, 0, False), ({}, 1, False)]:
            result = subprocess.CompletedProcess([], rc, json.dumps(dict(summary, **fields)) + '\n', 'Ran 11 tests in 0.1s\n\nOK\n')
            with mock.patch.object(q.subprocess, 'run', return_value=result) as invoked:
                check = q.run_selection(ROOT, 'tests/integration', 'test_authority_channel.py')
            self.assertEqual(check.passed, expected)
            self.assertEqual(invoked.call_args.args[0], [sys.executable, '-B', 'tools/run_integration_gate.py'])

    def test_integration_setup_failure_runs_registered_temporary_cleanup(self):
        spec = importlib.util.spec_from_file_location('cafe_failing_integration_setup', ROOT / 'tests/integration/test_authority_channel.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(tempfile, 'tempdir', folder), mock.patch.dict(os.environ, {'CAFE_AUTHORITY_FIXTURE': ''}):
            result = unittest.TestResult()
            module.TestAuthorityChannel('test_real_child_and_independent_broker_positive').run(result)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(list(Path(folder).iterdir()), [])


    def test_signal_guards_reject_unsafe_or_changed_child_ids(self):
        for pid, changed in [(-1, None), (0, None), (1, None), (os.getpid(), None), (900000001, 900000002)]:
            child = mock.Mock(pid=pid)
            def interrupted(*args, **kwargs):
                if changed is not None:
                    child.pid = changed
                raise KeyboardInterrupt
            child.communicate.side_effect = interrupted
            # All signal boundaries mocked: no unsafe PID reaches the kernel.
            with self.subTest(pid=pid), mock.patch.object(gate.subprocess, 'Popen', return_value=child), mock.patch.object(gate.os, 'killpg', create=True) as killed, mock.patch.object(gate.os, 'getpgid', create=True) as pgid, mock.patch.object(gate.os, 'getsid', create=True):
                with self.assertRaisesRegex(ValueError, 'not_owned_child'):
                    gate.run_owned(['fixture'], ROOT, {})
                killed.assert_not_called()
                pgid.assert_not_called()
                child.kill.assert_not_called()
                child.terminate.assert_not_called()

    def test_group_signal_requires_child_leading_own_new_session(self):
        for pgid, sid in [(900000002, 900000001), (900000001, 900000002)]:
            child = mock.Mock(pid=900000001)
            child.communicate.side_effect = KeyboardInterrupt
            with self.subTest(pgid=pgid, sid=sid), mock.patch.object(gate.subprocess, 'Popen', return_value=child), mock.patch.object(gate.os, 'name', 'posix'), mock.patch.object(gate.os, 'getpgid', return_value=pgid, create=True), mock.patch.object(gate.os, 'getsid', return_value=sid, create=True), mock.patch.object(gate.os, 'killpg', create=True) as killed:
                with self.assertRaisesRegex(ValueError, 'not_owned_session'):
                    gate.run_owned(['fixture'], ROOT, {})
                killed.assert_not_called()
                child.kill.assert_not_called()
                child.terminate.assert_not_called()

    def test_timeout_signals_only_the_captured_child_group(self):
        for hard in [False, True]:
            child = mock.Mock(pid=900000001)
            timeout = subprocess.TimeoutExpired(['fixture'], 1)
            child.communicate.side_effect = ([timeout, timeout, ('', '')] if hard else [timeout, ('', '')])
            with self.subTest(hard=hard), mock.patch.object(gate.signal, 'SIGKILL', 9, create=True), mock.patch.object(gate.subprocess, 'Popen', return_value=child) as spawn, mock.patch.object(gate.os, 'name', 'posix'), mock.patch.object(gate.os, 'getpgid', return_value=child.pid, create=True), mock.patch.object(gate.os, 'getsid', return_value=child.pid, create=True), mock.patch.object(gate.os, 'killpg', create=True) as killed:
                with self.assertRaises(subprocess.TimeoutExpired):
                    gate.run_owned(['fixture'], ROOT, {})
                self.assertIs(spawn.call_args.kwargs['start_new_session'], True)
                expected = [mock.call(child.pid, gate.signal.SIGTERM)]
                if hard:
                    expected.append(mock.call(child.pid, gate.signal.SIGKILL))
                self.assertEqual(killed.call_args_list, expected)
                child.kill.assert_not_called()
                child.terminate.assert_not_called()


    def test_windows_fixture_selection_from_gate(self):
        # A private OS proxy changes only the gate's selector, never pathlib or
        # the host os module. The existing positive oracle must still pass.
        with mock.patch.object(gate, 'os', types.SimpleNamespace(name='nt')):
            self.test_target_owned_and_fixture_selected_without_caller_export()
            self.test_failed_empty_skipped_and_unverified_suite_never_pass()

    def test_signal_tests_without_posix_apis(self):
        # Windows lacks these APIs. Never borrow a real kernel signal function.
        # Execute all guard/timeout cases, including forced POSIX-session paths,
        # against an initially empty API surface and mocked child handles only.
        isolated = types.SimpleNamespace(name='nt', getpid=os.getpid)
        with mock.patch.object(gate, 'os', isolated):
            self.test_signal_guards_reject_unsafe_or_changed_child_ids()
            self.test_group_signal_requires_child_leading_own_new_session()
            self.test_timeout_signals_only_the_captured_child_group()
        self.assertFalse(any(hasattr(isolated, key) for key in ('killpg', 'getpgid', 'getsid')))


    def test_owned_child_utf8_is_independent_of_platform_text_default(self):
        # Real child emits Node's UTF-8 summary bytes; emulate a Windows ANSI default.
        text = '\u2139 tests 1\n\u2139 pass 1\n\u2139 fail 0\n\u2139 cancelled 0\n\u2139 skipped 0\n'
        original = gate.subprocess.Popen
        observed = []
        def platform_default(*args, **kwargs):
            observed.append(kwargs.get('encoding'))
            kwargs.setdefault('encoding', 'cp1252')
            return original(*args, **kwargs)
        program = 'import os; os.write(1, ' + repr(text.encode('utf-8')) + ')'
        with mock.patch.object(gate.subprocess, 'Popen', side_effect=platform_default):
            result = gate.run_owned([sys.executable, '-B', '-c', program], ROOT, dict(os.environ), 10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(observed, ['utf-8'])
        self.assertEqual(result.stdout, text)
        self.assertEqual(result.stderr, '')
        sys.path.insert(0, str(ROOT / 'tools'))
        import run_ci
        self.assertEqual(run_ci.node_test_evidence(result.stdout)['pass'], 1)


if __name__ == '__main__':
    unittest.main()
