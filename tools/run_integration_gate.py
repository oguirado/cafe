"""Build the locked offline test authority, run real integration tests, and clean.

Optional machine settings belong in ignored local/integration_gate.json or the
explicit CAFE_INTEGRATION_CONFIG file. No compiler, crate or package is installed.
Each invocation owns an isolated target and fixture temporary root.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def configuration(root, config=None):
    path = config or os.environ.get('CAFE_INTEGRATION_CONFIG')
    if path is None:
        candidate = root / 'local/integration_gate.json'
        path = candidate if candidate.is_file() else None
    data = {} if path is None else json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) - {'cargo', 'rustc', 'cargo_home'}:
        raise ValueError('invalid_integration_configuration')
    for value in data.values():
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError('invalid_integration_configuration')
    cargo = data.get('cargo') or shutil.which('cargo')
    if cargo is None or not Path(cargo).is_file():
        raise ValueError('cargo_unavailable')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', CARGO_NET_OFFLINE='true')
    for key in ('CAFE_AUTHORITY_FIXTURE', 'CARGO_TARGET_DIR', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER'):
        env.pop(key, None)
    if 'rustc' in data:
        if not Path(data['rustc']).is_file():
            raise ValueError('rustc_unavailable')
        env['RUSTC'] = data['rustc']
    if 'cargo_home' in data:
        if not Path(data['cargo_home']).is_dir():
            raise ValueError('cargo_cache_unavailable')
        env['CARGO_HOME'] = data['cargo_home']
    return cargo, env


def run_owned(command, root, env, timeout=900):
    """Signal only this invocation's known child or its newly created session."""
    new_session = os.name == 'posix'
    process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               start_new_session=new_session)
    known_child = process
    child_pid = process.pid

    def signal_owned(sig):
        pid = process.pid
        if (process is not known_child or not isinstance(pid, int) or
                pid <= 1 or pid == os.getpid() or pid != child_pid):
            raise ValueError('signal_target_not_owned_child')
        if new_session:
            # No inherited/ambient PGID: the child must still lead our session.
            if os.getpgid(pid) != pid or os.getsid(pid) != pid:
                raise ValueError('signal_target_not_owned_session')
            os.killpg(pid, sig)
        elif process.poll() is None:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        try:
            signal_owned(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                signal_owned(signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        raise


def run_gate(root=ROOT, config=None):
    report = {'schema': 'cafe.integration-gate.v1', 'status': 'fail',
              'build_rc': None, 'test_rc': None, 'tests': None, 'cleanup': False}
    owned = None
    try:
        cargo, env = configuration(root, config)
        with tempfile.TemporaryDirectory(prefix='cafe-integration-') as directory:
            owned = Path(directory)
            target = owned / 'target'
            temporary = owned / 'tmp'
            temporary.mkdir()
            env.update(CARGO_TARGET_DIR=str(target), TMPDIR=str(temporary))
            build = run_owned([cargo, 'build', '--bin', 'cafe-authority-fixture', '--offline', '--locked'], root, env)
            report['build_rc'] = build.returncode
            if build.returncode:
                sys.stderr.write(build.stdout + build.stderr)
            else:
                binary = target / 'debug' / ('cafe-authority-fixture.exe' if os.name == 'nt' else 'cafe-authority-fixture')
                if not binary.is_file():
                    raise ValueError('test_authority_binary_absent')
                env['CAFE_AUTHORITY_FIXTURE'] = str(binary)
                test = run_owned([sys.executable, '-B', '-m', 'unittest', 'discover',
                                  '-s', 'tests/integration', '-p', 'test_authority_channel.py'], root, env)
                report['test_rc'] = test.returncode
                text = test.stdout + test.stderr
                sys.stderr.write(text)
                counts = re.findall(r'(?m)^Ran (\d+) tests? in [^\n]+$', text)
                count = int(counts[0]) if len(counts) == 1 else None
                report['tests'] = count
                if (test.returncode == 0 and count is not None and count > 0 and
                        re.search(r'(?m)^OK\s*$', text) and
                        not re.search(r'(?m)^(?:FAILED|ERROR:|FAIL:|OK \()', text)):
                    report['status'] = 'pass'
    except (OSError, ValueError, subprocess.SubprocessError):
        report['error'] = 'integration_gate_unavailable_or_failed'
    finally:
        report['cleanup'] = owned is None or not owned.exists()
        if not report['cleanup']:
            report['status'] = 'fail'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='explicit external machine configuration')
    args = parser.parse_args(argv)
    report = run_gate(config=args.config)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'pass' and report['cleanup'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
