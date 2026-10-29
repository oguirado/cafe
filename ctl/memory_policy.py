"""Owned scopes, declared-memory admission and durable recovery evidence."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from cafe_lanes import atomic_write, locked, read_other, measure, link_hold

GIB = 1024 ** 3
BUDGET = 40 * GIB
SCOPE = re.compile(r'run-cafe-[a-f0-9]{32}\.scope\Z')


def high_limit(maximum):
    page = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
    return (maximum * 3 // 4) // page * page


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def pid_start(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        return None


def application_memory():
    root = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice')
    apps = list(root.glob('app-gnome-chatgpt-*.scope'))
    if not apps:
        raise ValueError('desktop_memory_unavailable')
    # The local command bridge may live outside the Desktop cgroup. Charge it
    # conservatively as well; never alter either application cgroup.
    bridges = list(root.glob('app-org.chromium.Chromium-*.scope'))
    desktop = sum(int((p / 'memory.current').read_text()) for p in apps)
    bridge = sum(int((p / 'memory.current').read_text()) for p in bridges)
    return desktop, bridge


def read_state(camp):
    path = camp / 'LANES_CAFE.json'
    if not path.exists():
        raise ValueError('explicit_ledger_initialization_required')
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('ledger_too_large')
    state = json.loads(path.read_text())
    if state.get('schema') != 'cafe.lanes.v2':
        raise ValueError('legacy_ledger_requires_recorded_recovery')
    state.setdefault('m0_approved', False)
    if not isinstance(state.get('leases'), dict) or not isinstance(state.get('history'), list):
        raise ValueError('corrupt_ledger')
    for key, lease in state['leases'].items():
        if lease.get('id') != key or not SCOPE.fullmatch(lease.get('scope', '')) or lease['scope'] != f'run-cafe-{key}.scope':
            raise ValueError('unowned_scope')
        if type(lease.get('memory_max')) is not int or not 0 < lease['memory_max'] <= 16 * GIB:
            raise ValueError('unbounded_lease')
    return state


def save(camp, state):
    state['lanes'] = sum(x['kind'] != 'sentinel' for x in state['leases'].values())
    state['writers'] = sum(bool(x['writer']) for x in state['leases'].values())
    atomic_write(camp / 'LANES_CAFE.json', state)


def scope_properties(unit):
    if not SCOPE.fullmatch(unit):
        raise ValueError('unowned_scope')
    result = subprocess.run(['systemctl', '--user', 'show', unit,
        '--property=LoadState', '--property=ActiveState', '--property=Description', '--property=MemoryMax',
        '--property=MemoryHigh', '--property=MemorySwapMax'], capture_output=True, text=True, timeout=10)
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode != 0 or props.get('LoadState') not in ('loaded', 'not-found') or props.get('ActiveState') not in ('active', 'activating', 'deactivating', 'inactive', 'failed'):
        raise ValueError('scope_state_unavailable')
    return props


def recover_dead(state, properties=scope_properties, start=pid_start, current_boot=None):
    current_boot = current_boot or boot_id()
    for key, lease in list(state['leases'].items()):
        same_boot = lease['boot_id'] == current_boot
        process = lease.get('worker_pid', lease['owner_pid'])
        expected = lease.get('worker_start', lease['owner_start'])
        if same_boot and start(process) == expected:
            continue
        if same_boot:
            try:
                active = properties(lease['scope']).get('ActiveState')
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if active not in ('inactive', 'failed'):
                continue  # Missing, unknown and live scope evidence remains charged.
        state['history'].append(dict(event='recovered_dead_lease', lease=dict(lease), reason='prior_boot' if not same_boot else 'pid_absent_or_reused'))
        del state['leases'][key]


def recover_legacy(camp, watch):
    """Convert the single recorded pre-reboot card only with its dead PID proof."""
    with locked(camp / 'LANES_CAFE.json'):
        path = camp / 'LANES_CAFE.json'
        raw = path.read_bytes()
        previous = json.loads(raw)
        if previous != {'schema': 'cafe.lanes.v1', 'lanes': 1, 'writers': 1}:
            raise ValueError('legacy_recovery_shape_not_proven')
        evidence = json.loads(watch.read_text())
        if evidence.get('job') != 'cf_s1_g4d_write_w1' or evidence.get('wave') != 'cafe' or type(evidence.get('pid')) is not int or pid_start(evidence['pid']) is not None:
            raise ValueError('legacy_dead_pid_not_proven')
        event = dict(event='recovered_legacy_dead_lease', previous=previous,
                     previous_sha256=hashlib.sha256(raw).hexdigest(), pid=evidence['pid'],
                     watch_sha256=hashlib.sha256(watch.read_bytes()).hexdigest(), job=evidence['job'],
                     reason='recorded_worker_pid_absent', old_scope='AUSENTE')
        archive = camp / 'out' / 'M0' / 'legacy_lanes_v1.json'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists() and archive.read_bytes() != raw:
            raise ValueError('legacy_evidence_exists')
        archive.write_bytes(raw)
        state = dict(schema='cafe.lanes.v2', leases={}, history=[event], hold=False, m0_approved=False,
                     recovery_since=None, clean_seconds=0, last_sample=None,
                     telemetry_boot=boot_id(), sentinel_events=0, sequence=0)
        save(camp, state)
        return event


def reserve(camp, settings, memory_max, kind, writer=False, sample=None, app=None, now=None):
    if type(memory_max) is not int or not 0 < memory_max <= 16 * GIB:
        raise ValueError('memory_limit_rejected')
    sample = sample or measure()
    available, load1, load5 = sample
    if any(not math.isfinite(v) or v < 0 for v in sample):
        raise ValueError('measurement_unavailable')
    with locked(camp / 'LANES_CAFE.json'):
        state = read_state(camp)
        recover_dead(state)
        if not state['m0_approved'] and kind not in ('sentinel', 'm0'):
            save(camp, state); raise ValueError('M0_not_approved')
        other, unavailable = read_other(settings['other_lanes'])
        jobs = [x for x in state['leases'].values() if x['kind'] != 'sentinel']
        writers = sum(bool(x['writer']) for x in jobs) + int(writer)
        ceiling = 24
        new_count = len(jobs) + int(kind != 'sentinel')
        total = sum(x['memory_max'] for x in state['leases'].values()) + memory_max
        if kind != 'sentinel' and (state['hold'] or (camp / 'PARADA.txt').read_text().strip() != 'LIBRE' or link_hold(camp)):
            save(camp, state); raise ValueError('admission_hold')
        if kind != 'sentinel' and (unavailable or available < 25 or load1 > 16 or load5 > 20 or writers > 6 or new_count > min(ceiling, 40 - other - 8)):
            save(camp, state); raise ValueError('L1_admission_rejected')
        if total > BUDGET or (kind != 'sentinel' and total > (available - 20 - 1.5 * writers) * GIB):
            save(camp, state); raise ValueError('declared_memory_budget_exceeded')
        key = uuid.uuid4().hex
        state['sequence'] += 1
        lease = dict(id=key, scope=f'run-cafe-{key}.scope', memory_max=memory_max,
                     memory_high=high_limit(memory_max), memory_swap_max=0,
                     kind=kind, writer=bool(writer), owner_pid=os.getpid(),
                     owner_start=pid_start(os.getpid()), boot_id=boot_id(),
                     order=state['sequence'], admitted_at=time.time() if now is None else now)
        if lease['owner_start'] is None:
            raise ValueError('lease_owner_unproven')
        state['leases'][key] = lease
        save(camp, state)
        return lease


def verify_current(camp, key):
    with locked(camp / 'LANES_CAFE.json'):
        state = read_state(camp); lease = state['leases'][key]
        group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
        if Path(group).name != lease['scope'] or 'newton-ia.slice' not in Path(group).parts:
            raise ValueError('worker_scope_mismatch')
        location = Path('/sys/fs/cgroup') / group.lstrip('/')
        if (int((location / 'memory.max').read_text()) != lease['memory_max'] or
            int((location / 'memory.high').read_text()) != lease['memory_high'] or
            (location / 'memory.swap.max').read_text().strip() != '0'):
            raise ValueError('kernel_memory_limits_mismatch')


def register_worker(camp, key):
    verify_current(camp, key)
    with locked(camp / 'LANES_CAFE.json'):
        state = read_state(camp); lease = state['leases'][key]
        lease.update(worker_pid=os.getpid(), worker_start=pid_start(os.getpid()))
        save(camp, state)


def release(camp, key):
    with locked(camp / 'LANES_CAFE.json'):
        state = read_state(camp)
        lease = state['leases'].get(key)
        if lease is None:
            return
        if lease['owner_pid'] != os.getpid() or lease['owner_start'] != pid_start(os.getpid()):
            raise ValueError('lease_owner_mismatch')
        # systemd-run can return before its scope finishes teardown. Keep the
        # charge while waiting briefly; unknown or still-live state never frees it.
        for _ in range(21):
            active = scope_properties(lease['scope']).get('ActiveState')
            if active in ('inactive', 'failed'):
                break
            if active not in ('active', 'activating', 'deactivating'):
                raise ValueError('scope_state_unavailable')
            time.sleep(0.05)
        else:
            raise ValueError('scope_still_live')
        state['history'].append(dict(event='released', lease=dict(lease)))
        del state['leases'][key]; save(camp, state)


def systemd_argv(lease, command):
    if not SCOPE.fullmatch(lease['scope']) or lease['scope'] != f"run-cafe-{lease['id']}.scope":
        raise ValueError('unowned_scope')
    if (type(lease['memory_max']) is not int or not 0 < lease['memory_max'] <= 16 * GIB or
        lease['memory_high'] != high_limit(lease['memory_max']) or lease['memory_swap_max'] != 0):
        raise ValueError('memory_limit_rejected')
    return ['systemd-run', '--user', '--scope', '--quiet', '--slice=newton-ia.slice', '--unit=' + lease['scope'],
        '--description=Cafe owned lease ' + lease['id'], '-p', f"MemoryMax={lease['memory_max']}",
        '-p', f"MemoryHigh={lease['memory_high']}", '-p', 'MemorySwapMax=0', '--', *command]


def stop_owned(lease, properties=scope_properties, runner=subprocess.run):
    props = properties(lease['scope'])
    if props.get('ActiveState') != 'active':
        return False
    if (props.get('Description') != 'Cafe owned lease ' + lease['id'] or
        props.get('MemoryMax') != str(lease['memory_max']) or
        props.get('MemoryHigh') != str(lease['memory_high']) or props.get('MemorySwapMax') != '0'):
        raise ValueError('scope_ownership_or_limit_mismatch')
    runner(['systemctl', '--user', 'stop', lease['scope']], check=True, timeout=15,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
