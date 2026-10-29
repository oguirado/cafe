"""Serialize verified patches and leave commit windows for the operator."""
from __future__ import annotations
import argparse
import contextlib
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SLUG = re.compile(r'[a-z][a-z0-9-]{0,47}\Z')

# Reviewed generated outputs only; hand-written code and validators remain disjoint.
SHARED_GENERATED = frozenset({
    'catalogue/nodes.jsonl',
    'catalogue/router/ID',
    'catalogue/router/cols.json',
    'catalogue/router/digest/0.jsonl',
    'catalogue/router/docs/0.jsonl',
    'catalogue/router/edges.json',
    'catalogue/router/manifest.json',
    'catalogue/router/pfx.json',
    'catalogue/router/phr/ba.json',
    'catalogue/router/phr/br.json',
    'catalogue/router/phr/ca.json',
    'catalogue/router/phr/co.json',
    'catalogue/router/phr/cr.json',
    'catalogue/router/phr/de.json',
    'catalogue/router/phr/en.json',
    'catalogue/router/phr/fr.json',
    'catalogue/router/phr/he.json',
    'catalogue/router/phr/hm.json',
    'catalogue/router/phr/id.json',
    'catalogue/router/phr/jo.json',
    'catalogue/router/phr/le.json',
    'catalogue/router/phr/ma.json',
    'catalogue/router/phr/na.json',
    'catalogue/router/phr/op.json',
    'catalogue/router/phr/ow.json',
    'catalogue/router/phr/pa.json',
    'catalogue/router/phr/pe.json',
    'catalogue/router/phr/re.json',
    'catalogue/router/phr/sa.json',
    'catalogue/router/phr/se.json',
    'catalogue/router/phr/ty.json',
    'catalogue/router/phr/va.json',
    'catalogue/router/phr/vi.json',
    'catalogue/router/phr/wi.json',
    'catalogue/router/phr/wr.json',
    'catalogue/router/post/10.json',
    'catalogue/router/post/11.json',
    'catalogue/router/post/12.json',
    'catalogue/router/post/13.json',
    'catalogue/router/post/14.json',
    'catalogue/router/post/15.json',
    'catalogue/router/post/16.json',
    'catalogue/router/post/17.json',
    'catalogue/router/post/18.json',
    'catalogue/router/post/19.json',
    'catalogue/router/post/ac.json',
    'catalogue/router/post/ad.json',
    'catalogue/router/post/ap.json',
    'catalogue/router/post/ar.json',
    'catalogue/router/post/au.json',
    'catalogue/router/post/av.json',
    'catalogue/router/post/ba.json',
    'catalogue/router/post/bi.json',
    'catalogue/router/post/bo.json',
    'catalogue/router/post/br.json',
    'catalogue/router/post/by.json',
    'catalogue/router/post/ca.json',
    'catalogue/router/post/ch.json',
    'catalogue/router/post/cl.json',
    'catalogue/router/post/co.json',
    'catalogue/router/post/cr.json',
    'catalogue/router/post/cu.json',
    'catalogue/router/post/da.json',
    'catalogue/router/post/de.json',
    'catalogue/router/post/di.json',
    'catalogue/router/post/do.json',
    'catalogue/router/post/en.json',
    'catalogue/router/post/ev.json',
    'catalogue/router/post/ex.json',
    'catalogue/router/post/fe.json',
    'catalogue/router/post/ff.json',
    'catalogue/router/post/fi.json',
    'catalogue/router/post/fr.json',
    'catalogue/router/post/ge.json',
    'catalogue/router/post/gi.json',
    'catalogue/router/post/gr.json',
    'catalogue/router/post/ha.json',
    'catalogue/router/post/he.json',
    'catalogue/router/post/hm.json',
    'catalogue/router/post/id.json',
    'catalogue/router/post/in.json',
    'catalogue/router/post/jo.json',
    'catalogue/router/post/js.json',
    'catalogue/router/post/la.json',
    'catalogue/router/post/le.json',
    'catalogue/router/post/li.json',
    'catalogue/router/post/lo.json',
    'catalogue/router/post/ma.json',
    'catalogue/router/post/md.json',
    'catalogue/router/post/me.json',
    'catalogue/router/post/na.json',
    'catalogue/router/post/ne.json',
    'catalogue/router/post/ob.json',
    'catalogue/router/post/of.json',
    'catalogue/router/post/on.json',
    'catalogue/router/post/op.json',
    'catalogue/router/post/or.json',
    'catalogue/router/post/ou.json',
    'catalogue/router/post/ov.json',
    'catalogue/router/post/ow.json',
    'catalogue/router/post/pa.json',
    'catalogue/router/post/pe.json',
    'catalogue/router/post/pi.json',
    'catalogue/router/post/po.json',
    'catalogue/router/post/pr.json',
    'catalogue/router/post/pu.json',
    'catalogue/router/post/py.json',
    'catalogue/router/post/qu.json',
    'catalogue/router/post/re.json',
    'catalogue/router/post/ro.json',
    'catalogue/router/post/rs.json',
    'catalogue/router/post/ru.json',
    'catalogue/router/post/sa.json',
    'catalogue/router/post/sc.json',
    'catalogue/router/post/se.json',
    'catalogue/router/post/sh.json',
    'catalogue/router/post/sn.json',
    'catalogue/router/post/so.json',
    'catalogue/router/post/sp.json',
    'catalogue/router/post/sr.json',
    'catalogue/router/post/st.json',
    'catalogue/router/post/su.json',
    'catalogue/router/post/ta.json',
    'catalogue/router/post/te.json',
    'catalogue/router/post/ti.json',
    'catalogue/router/post/ty.json',
    'catalogue/router/post/v0.json',
    'catalogue/router/post/va.json',
    'catalogue/router/post/vi.json',
    'catalogue/router/post/wi.json',
    'catalogue/router/post/wr.json',
    'catalogue/router/thes.json',
    'catalogue/router_sources.json',
    'catalogue/routes.tsv',
    'catalogue/source_profile.json',
    'catalogue/symbols.jsonl',
})



def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout


def safe_path(repo, name):
    repo = Path(repo).resolve(strict=True)
    path = Path(name)
    if not name or '\\' in name or ':' in name or '\x00' in name or path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('unsafe_patch_path')
    if path.parts[0] in ('.git', 'local', 'node_modules', 'target', '_scratch', '_target') or any(p.startswith('.env') or p in ('operator.token', 'launch.token', 'auth.json') or p.endswith('.key') for p in path.parts):
        raise ValueError('forbidden_patch_path')
    target = repo / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != repo.parent):
        raise ValueError('symlink_patch_path')
    if repo not in target.resolve().parents:
        raise ValueError('escaping_patch_path')
    return target


def patch_paths(repo, patch):
    summary = git(repo, 'apply', '--summary', str(patch)).decode('utf-8')
    if re.search(r'\b(?:120000|160000)\b|\b(?:rename|copy)\b', summary):
        raise ValueError('symlink_submodule_or_rename_patch_forbidden')
    raw = git(repo, 'apply', '--numstat', '-z', str(patch))
    names = []
    for row in raw.split(b'\0'):
        if not row:
            continue
        fields = row.decode('utf-8').split('\t', 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError('binary_or_renamed_patch_not_supported')
        safe_path(repo, fields[2])
        names.append(fields[2])
    if not names or len(names) != len(set(names)):
        raise ValueError('empty_or_duplicate_patch')
    return sorted(names)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.queue-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(camp):
    camp = Path(camp).resolve(strict=True)
    path = camp / 'LINK_QUEUE.json'
    with (camp / 'link_queue.lock').open('a+') as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            lock.seek(0); lock.write(' '); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        if path.exists():
            state = json.loads(path.read_text())
            if state.get('schema') != 'cafe.link-queue.v1' or not isinstance(state.get('units'), list) or not isinstance(state.get('reds'), dict):
                raise ValueError('corrupt_link_queue')
        else:
            state = {'schema': 'cafe.link-queue.v1', 'units': [], 'reds': {}}
        count = state.setdefault('rollbacks_since_green', 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError('corrupt_link_rollback_count')
        state['window_due'] = window_due(state)
        yield state
        state['window_due'] = window_due(state)
        atomic_json(path, state)



def window_due(state, now=None):
    rows = [row for row in state['units'] if row['status'] == 'linked' and not row.get('window')]
    if not rows:
        return False
    oldest = min(row.get('linked_at', 0) for row in rows)
    return len(rows) >= 4 or (time.time() if now is None else now) - oldest >= 90 * 60


def append(repo, camp, unit, surface, patch, evidence, message):
    if not SLUG.fullmatch(unit) or not SLUG.fullmatch(surface) or not message.strip() or '\n' in message or '\x00' in message:
        raise ValueError('invalid_unit_metadata')
    patch = Path(patch).resolve(strict=True)
    body = patch.read_bytes()
    proof = json.loads(Path(evidence).read_text())
    if proof.get('verdict') != 'PASS' or proof.get('patch_sha256') != digest(body):
        raise ValueError('patch_not_bound_to_pass_evidence')
    paths = patch_paths(repo, patch)
    # Bind the patch to the exact preimages it was reviewed against.
    preimages = {name: snapshot(safe_path(repo, name)) for name in paths}
    if proof.get('preimages') != preimages:
        raise ValueError('reviewed_current_preimages_required')
    with locked(camp) as state:
        retire_committed(repo, state)
        if any(row['unit'] == unit for row in state['units']):
            raise ValueError('duplicate_unit')
        if any(set(paths) & set(row['paths']) for row in state['units'] if row['status'] == 'queued'):
            raise ValueError('overlapping_queued_unit_paths')
        overlap = set().union(*(set(paths) & set(row['paths']) for row in state['units'] if row['status'] == 'linked'))
        if overlap - SHARED_GENERATED:
            raise ValueError('overlapping_uncommitted_unit_paths')
        if overlap and proof.get('preimages') != preimages:
            raise ValueError('shared_metadata_requires_current_preimages_in_pass_evidence')
        if state['reds'].get(surface, 0) >= 2:
            raise ValueError('surface_parked')
        stored = Path(camp) / 'out' / 'link' / 'patches' / (unit + '.patch')
        stored.parent.mkdir(parents=True, exist_ok=True)
        if stored.exists():
            raise ValueError('patch_evidence_already_exists')
        stored.write_bytes(body)
        row = {'unit': unit, 'surface': surface, 'message': message, 'patch': str(stored), 'sha256': digest(body), 'paths': paths, 'preimages': preimages, 'status': 'queued', 'gate': None}
        state['units'].append(row)
    return row



def retire_committed(repo, state):
    """Recognize operator commits by exact content; never manufacture a receipt."""
    for row in state['units']:
        if row['status'] != 'linked':
            continue
        try:
            expected_images = dict(row['postimages'])
            expected_images.update(row.get('window_shared_postimages', {}))
            committed = all(digest(git(repo, 'show', 'HEAD:' + name)) == expected['sha256']
                            for name, expected in expected_images.items() if expected is not None)
            removed = all(subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', 'HEAD:' + name], capture_output=True).returncode != 0
                          for name, expected in expected_images.items() if expected is None)
            unchanged = all(snapshot(safe_path(repo, name)) == expected for name, expected in expected_images.items())
            if committed and removed and unchanged:
                row['status'] = 'committed-by-operator'
        except subprocess.SubprocessError:
            continue


def snapshot(path):
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError('patch_target_not_regular')
    return {'sha256': digest(path.read_bytes()), 'mode': stat.S_IMODE(path.stat().st_mode)}


def full_gate(repo, camp, unit):
    target = Path(camp) / 'out' / 'link' / unit / 'target-integration'
    env = dict(os.environ, CARGO_TARGET_DIR=str(target), CARGO_HOME=str(Path(camp) / 'out' / 'P09' / 'cargo-home'))
    config_path = Path(repo) / 'local' / 'sprint.json'
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    node_bin = env.get('CAFE_NODE_BIN') or config.get('node_bin')
    if not isinstance(node_bin, str) or not Path(node_bin).is_absolute() or not (Path(node_bin) / 'node').is_file():
        raise ValueError('explicit_node_runtime_directory_required')
    env['PATH'] = node_bin + os.pathsep + env.get('PATH', '')
    python = str(Path(camp) / 'out' / 'P15' / 'deps' / 'venv' / 'bin' / 'python')
    if not Path(python).is_file():
        raise ValueError('approved_sprint_python_absent')
    argv = [python, '-B', str(Path(repo) / 'ctl' / 'run_capped.py'), '--kind', 'build', '--', 'timeout', '900', python, '-B', 'tools/run_ci.py']
    result = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True)
    evidence = Path(camp) / 'out' / 'link' / unit
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'gate.stdout').write_text(result.stdout)
    (evidence / 'gate.stderr').write_text(result.stderr)
    if result.returncode == 0:
        try:
            report = json.loads(result.stdout)
            expected = ['rust', 'python', 'node', 'checkjs', 'integration', 'router', 'publication', 'claims']
            if report.get('status') != 'pass' or [row['stage'] for row in report['stages']] != expected or any(row['status'] != 'pass' for row in report['stages']):
                return 1
        except (ValueError, TypeError, KeyError):
            return 1
    return result.returncode


def apply_next(repo, camp, runner=full_gate):
    if (Path(camp) / 'PARADA.txt').read_text().strip() != 'LIBRE':
        raise ValueError('operator_stop_active')
    repo = Path(repo).resolve(strict=True)
    with locked(camp) as state:
        retire_committed(repo, state)
        if any(r['status'] == 'linked' and r.get('window') for r in state['units']):
            raise ValueError('operator_window_commit_pending')
        row = next((r for r in state['units'] if r['status'] == 'queued' and state['reds'].get(r['surface'], 0) < 2), None)
        if row is None:
            raise ValueError('no_admissible_queued_unit')
        patch = Path(row['patch'])
        if digest(patch.read_bytes()) != row['sha256']:
            raise ValueError('queued_patch_changed')
        backups = {}
        for name in row['paths']:
            path = safe_path(repo, name)
            if snapshot(path) != row['preimages'][name]:
                raise ValueError('patch_preimage_changed')
            backups[name] = None if not path.exists() else (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        # git apply never touches the index; rollback restores exactly the owned preimages.
        git(repo, 'apply', '--check', str(patch))
        git(repo, 'apply', str(patch))
        try:
            rc = runner(repo, Path(camp), row['unit'])
            if not isinstance(rc, int) or isinstance(rc, bool):
                raise ValueError('gate_result_invalid')
        except BaseException:
            restore(repo, backups)
            row['status'] = 'failed'
            row['gate'] = 'unavailable'
            state['reds'][row['surface']] = state['reds'].get(row['surface'], 0) + 1
            state['rollbacks_since_green'] += 1
            atomic_json(Path(camp) / 'LINK_QUEUE.json', state)
            raise
        row['gate'] = rc
        if rc:
            restore(repo, backups)
            row['status'] = 'failed'
            state['reds'][row['surface']] = state['reds'].get(row['surface'], 0) + 1
            state['rollbacks_since_green'] += 1
        else:
            row['status'] = 'linked'
            row['linked_at'] = time.time()
            row['postimages'] = {name: snapshot(safe_path(repo, name)) for name in row['paths']}
        row['window_due'] = window_due(state)
        return row


def restore(repo, backups):
    for name, data in backups.items():
        path = safe_path(repo, name)
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data[0])
            path.chmod(data[1])


def window(camp, repo=ROOT):
    repo = Path(repo).resolve(strict=True)
    with locked(camp) as state:
        retire_committed(repo, state)
        if any(row['status'] == 'linked' and row.get('window') for row in state['units']):
            raise ValueError('operator_window_commit_pending')
        rows = [r for r in state['units'] if r['status'] == 'linked']
        if not rows:
            raise ValueError('no_linked_units_for_window')
        final_shared = {}
        for row in rows:
            for name, expected in row['postimages'].items():
                if name in SHARED_GENERATED:
                    final_shared[name] = expected
                elif snapshot(safe_path(repo, name)) != expected:
                    raise ValueError('linked_files_changed_before_window')
        if any(snapshot(safe_path(repo, name)) != expected for name, expected in final_shared.items()):
            raise ValueError('linked_metadata_changed_before_window')
        directory = Path(camp) / 'out' / 'link'
        directory.mkdir(parents=True, exist_ok=True)
        number = 1
        while (directory / ('window_' + str(number) + '.md')).exists():
            number += 1
        lines = ['# Operator link window', '', 'Run from the integration repository after reviewing every diff. Agents did not commit or push.', '', 'Commit each unit code path first, then the single final generated-metadata change. Intermediate commits are not clean-clone candidates; run the receipt only after all commits.', '', '```bash']
        for row in rows:
            owned = sorted(set(row['paths']) - SHARED_GENERATED)
            if owned:
                pathspec = ' '.join(shlex.quote(p) for p in owned)
                lines.append('git add -- ' + pathspec)
                lines.append('git commit -m ' + shlex.quote(row['message']) + ' -- ' + pathspec)
            row['window'] = number
            row['window_code_paths'] = owned
            row['window_shared_postimages'] = {name: final_shared[name] for name in row['paths'] if name in final_shared}
        if final_shared:
            pathspec = ' '.join(shlex.quote(p) for p in sorted(final_shared))
            lines.append('git add -- ' + pathspec)
            lines.append('git commit -m ' + shlex.quote('Refresh generated router metadata for linked units') + ' -- ' + pathspec)
        lines += ['CARGO_HOME="$CAMP/out/P09/cargo-home" python3 -B tools/verify_clean_clone.py', 'git push origin main', '```', '']
        path = directory / ('window_' + str(number) + '.md')
        path.write_text('\n'.join(lines))
        return {'window': number, 'units': [r['unit'] for r in rows], 'shared_paths': sorted(final_shared), 'path': str(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--camp', type=Path, default=os.environ.get('CAMP'))
    sub = parser.add_subparsers(dest='action', required=True)
    add = sub.add_parser('append')
    for name in ('unit', 'surface', 'patch', 'evidence', 'message'):
        add.add_argument('--' + name, required=True)
    sub.add_parser('list'); sub.add_parser('apply-next'); sub.add_parser('window')
    args = parser.parse_args(argv)
    try:
        if args.camp is None:
            raise ValueError('campaign_directory_required')
        if args.action == 'append':
            result = append(args.repo.resolve(strict=True), args.camp, args.unit, args.surface, args.patch, args.evidence, args.message)
        elif args.action == 'apply-next':
            result = apply_next(args.repo, args.camp)
        elif args.action == 'window':
            result = window(args.camp, args.repo)
        else:
            with locked(args.camp) as state:
                result = dict(state)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get('status') == 'failed' else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'status': 'error', 'reason': 'link_queue_precondition_or_gate_failed'}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
