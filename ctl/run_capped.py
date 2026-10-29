"""Execute one leased command; no unbounded fallback or detached scope escape."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import memory_policy as policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['gate', 'build', 'clone', 'muse', 'argon', 'judge', 'preparation', 'electron', 'm0'], default='gate')
    parser.add_argument('--memory-gib', type=int)
    parser.add_argument('--writer', action='store_true')
    parser.add_argument('--inside')
    parser.add_argument('--verify-active')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    os.environ.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
    os.environ.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + os.environ['XDG_RUNTIME_DIR'] + '/bus')
    camp = Path(os.environ['CAMP']).resolve(strict=True)
    if args.verify_active:
        policy.verify_current(camp, args.verify_active)
        return 0
    if not command:
        parser.error('command required')
    if args.inside:
        policy.register_worker(camp, args.inside)
        os.environ['CAFE_CAPPED_ACTIVE'] = args.inside
        # Graphical dispatch opens a separate app scope; prohibit that escape.
        os.environ['OBRERO_VIA'] = 'directo'
        os.execvp(command[0], command)
    settings = json.loads((Path(__file__).resolve().parents[1] / 'local' / 'sprint.json').read_text())
    default = 16 if args.kind in ('muse', 'argon', 'judge') else (10 if args.kind in ('build', 'clone', 'electron') else 6)
    maximum = (args.memory_gib if args.memory_gib is not None else default) * policy.GIB
    lease = policy.reserve(camp, settings, maximum, args.kind, args.writer)
    try:
        inner = [sys.executable, str(Path(__file__).resolve()), '--inside', lease['id'], '--', *command]
        return subprocess.run(policy.systemd_argv(lease, inner)).returncode
    finally:
        policy.release(camp, lease['id'])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print(json.dumps({'status': 'blocked', 'reason': 'capped_admission_or_execution_failed'}), file=sys.stderr)
        raise SystemExit(75)
