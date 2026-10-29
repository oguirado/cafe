"""Fail-closed admission for shared-host work; settings stay outside the repository."""
import argparse
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt
import json
import math
import os
from pathlib import Path
import tempfile


def natural(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid_count')
    return value


def read_other(path):
    try:
        data = json.loads(Path(path).read_text())
        lanes = data['lanes']
        if not isinstance(lanes, dict):
            raise ValueError('invalid_lanes')
        return sum(natural(value) for value in lanes.values()), False
    except (OSError, ValueError, KeyError, TypeError):
        return 40, True


def capacity(available_gib, load1, load5, other_live, writers, stopped=False):
    for value in (available_gib, load1, load5):
        if not math.isfinite(value) or value < 0:
            raise ValueError('invalid_measurement')
    natural(other_live); natural(writers)
    limit = max(0, min(24, 40 - other_live - 8,
                       math.floor(available_gib - 20 - 1.5 * writers)))
    if available_gib < 25 or load1 > 16 or load5 > 20 or stopped or writers > 6:
        limit = 0
    return limit


def measure():
    rows = Path('/proc/meminfo').read_text().splitlines()
    available = next(float(row.split()[1]) / (1024 ** 2)
                     for row in rows if row.startswith('MemAvailable:'))
    load1, load5, _ = os.getloadavg()
    return available, load1, load5


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as stream:
        if fcntl is not None:
            fcntl.flock(stream, fcntl.LOCK_EX)
        else:
            stream.write(' '); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        yield


def transact(path, action, count, other_path, sample, stopped=False, writer=False):
    natural(count)
    with locked(path):
        if path.exists():
            data = json.loads(path.read_text())
            if data.get('schema') != 'cafe.lanes.v1':
                raise ValueError('corrupt_own_ledger')
            live = natural(data['lanes']); writers = natural(data['writers'])
            if writers > live:
                raise ValueError('corrupt_writer_count')
        else:
            live = writers = 0
        other, reserved = read_other(other_path)
        available, load1, load5 = sample
        proposed_writers = writers + count if writer and action == 'request' else writers
        limit = capacity(available, load1, load5, other, proposed_writers, stopped)
        requested = live + count if action == 'request' else count
        admitted = action not in ('request', 'set') or (requested <= limit and not (action == 'set' and requested < writers))
        if action == 'request' and admitted:
            live = requested; writers = proposed_writers
        elif action == 'set' and admitted:
            live = requested
        elif action == 'release':
            live = max(0, live - count)
            writers = max(0, writers - count) if writer else min(writers, live)
        if action != 'status' and admitted:
            atomic_write(path, {'schema': 'cafe.lanes.v1', 'lanes': live, 'writers': writers})
        return {'admitted': admitted, 'lanes': live, 'writers': writers,
                'capacity': limit, 'other_live': other, 'other_reserved': reserved,
                'mem_available_gib': available, 'load1': load1, 'load5': load5,
                'free': max(0, limit - live)}


def link_hold(camp):
    path = camp / 'LINK_QUEUE.json'
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    if data.get('schema') != 'cafe.link-queue.v1':
        raise ValueError('corrupt_link_queue')
    return natural(data.get('rollbacks_since_green', 0)) >= 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'request', 'release', 'set', 'recover-legacy', 'recover'))
    parser.add_argument('count', nargs='?', type=int, default=0)
    parser.add_argument('--writer', action='store_true')
    parser.add_argument('--watch', type=Path)
    args = parser.parse_args(argv)
    try:
        camp = Path(os.environ['CAMP']).resolve()
        import memory_policy as policy
        if args.action == 'recover-legacy':
            if args.watch is None:
                raise ValueError('watch_evidence_required')
            result = policy.recover_legacy(camp, args.watch)
            print(json.dumps(result, sort_keys=True)); return 0
        if args.action in ('request', 'release', 'set'):
            raise ValueError('use_recorded_capped_runner_not_anonymous_counts')
        with locked(camp / 'LANES_CAFE.json'):
            state = policy.read_state(camp)
            policy.recover_dead(state)
            policy.save(camp, state)
            desktop, bridge = policy.application_memory()
            result = {'admitted': not state['hold'], 'lanes': state['lanes'],
                      'writers': state['writers'], 'memory_max_sum': sum(x['memory_max'] for x in state['leases'].values()),
                      'desktop_current': desktop, 'bridge_current': bridge,
                      'sentinel_events': state['sentinel_events'], 'hold': state['hold']}
        print(json.dumps(result, sort_keys=True)); return 0
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        print(json.dumps({'admitted': False, 'capacity': 0, 'error': 'admission_unavailable'}))
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
