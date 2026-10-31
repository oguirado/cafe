"""Execute Linux-host wrapper contracts with explicit Git Bash on Windows."""
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile


def select_bash(*, windows=None, which=None, exists=None, environ=None):
    windows = sys.platform == 'win32' if windows is None else windows
    which = shutil.which if which is None else which
    exists = (lambda name: Path(name).is_file()) if exists is None else exists
    environ = os.environ if environ is None else environ
    if not windows:
        selected = which('bash')
        if selected and exists(selected):
            return selected
    else:
        candidates = []
        git = which('git')
        if git:
            candidates.append(str(PureWindowsPath(git).parent.parent / 'bin/bash.exe'))
        for key in ('ProgramFiles', 'ProgramFiles(x86)'):
            if environ.get(key):
                candidates.append(str(PureWindowsPath(environ[key]) / 'Git/bin/bash.exe'))
        for candidate in candidates:
            parts = [part.casefold() for part in PureWindowsPath(candidate).parts]
            if 'system32' not in parts and exists(candidate):
                return candidate
    raise RuntimeError('BLOCKED: required host Bash / Git Bash is unavailable')


def posix_argument(value):
    """Translate a native drive path to one argv item, without invoking a shell."""
    value = str(value)
    path = PureWindowsPath(value)
    if not path.is_absolute():
        return value
    if len(path.drive) != 2 or path.drive[1] != ':':
        raise RuntimeError('BLOCKED: unsupported Git Bash path root')
    return '/' + path.drive[0].lower() + '/' + '/'.join(path.parts[1:])


PYTHON_ALIAS = b'#!/bin/sh\nexec "$CAFE_PYTHON" "$@"\n'


def run_bash(arguments, *, python3_alias=False, **kwargs):
    """Run arguments under the selected Bash.

    With python3_alias, bare `python3`/`python` inside the child resolve to the
    current interpreter (CAFE_PYTHON) through a per-call directory prepended to
    the child PATH only; no global PATH, git config or interpreter is changed.
    """
    windows = sys.platform == 'win32'
    selected = select_bash(windows=windows)
    env = dict(kwargs.pop('env', os.environ))
    env.pop('BASH_ENV', None)
    env.pop('ENV', None)
    env['CAFE_PYTHON'] = posix_argument(sys.executable) if windows else sys.executable
    arguments = [posix_argument(arg) if windows else str(arg) for arg in arguments]
    if not python3_alias:
        return subprocess.run([selected, *arguments], env=env, **kwargs)
    with tempfile.TemporaryDirectory(prefix='cafe-python-alias-') as alias:
        for name in ('python3', 'python'):
            shim = Path(alias) / name
            shim.write_bytes(PYTHON_ALIAS)
            shim.chmod(0o755)
        key = next((k for k in env if k.upper() == 'PATH'), 'PATH')
        env[key] = alias + os.pathsep + env[key] if env.get(key) else alias
        return subprocess.run([selected, *arguments], env=env, **kwargs)
