"""Memory-policy oracles use isolated ledgers and no real process signals."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ctl'))
import memory_policy as P
import run_capped as R


class MemoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.camp = Path(self.tmp.name).resolve(strict=True)
        boot = mock.patch.object(P, 'boot_id', return_value='fixture-boot'); boot.start(); self.addCleanup(boot.stop)
        start = mock.patch.object(P, 'pid_start', side_effect=lambda pid: 'fixture-start' if pid == os.getpid() else None); start.start(); self.addCleanup(start.stop)
        recover = P.recover_dead
        def portable_recover(state, *args, **kwargs):
            kwargs.setdefault('start', P.pid_start)
            return recover(state, *args, **kwargs)
        patch = mock.patch.object(P, 'recover_dead', side_effect=portable_recover); patch.start(); self.addCleanup(patch.stop)
        (self.camp / 'PARADA.txt').write_text('LIBRE')
        self.other = self.camp / 'other.json'; self.other.write_text('{"lanes":{}}')
        self.state = dict(schema='cafe.lanes.v2', leases={}, history=[], hold=False,
            m0_approved=True, recovery_since=None, clean_seconds=0, last_sample=None,
            telemetry_boot=P.boot_id(), sentinel_events=0, sequence=0)
        self.save()

    def save(self): P.save(self.camp, self.state)
    def read(self): return P.read_state(self.camp)
    def request(self, size=6, kind='gate', **kw):
        return P.reserve(self.camp, {'other_lanes': str(self.other)}, size * P.GIB,
                         kind, sample=(80, 1, 1), app=(2 * P.GIB, 0), **kw)

    def test_declared_sum_not_lane_estimate(self):
        for _ in range(6): self.request()
        with self.assertRaisesRegex(ValueError, 'budget'): self.request()
        self.assertEqual(sum(x['memory_max'] for x in self.read()['leases'].values()), 36 * P.GIB)

    def test_large_builds_use_declared_ten_gib(self):
        for _ in range(4): self.request(10, 'build')
        with self.assertRaisesRegex(ValueError, 'budget'): self.request(10, 'build')

    def test_m0_blocks_all_ordinary_admission(self):
        self.state['m0_approved'] = False; self.save()
        with self.assertRaisesRegex(ValueError, 'M0_not_approved'): self.request()
        self.assertEqual(self.request(1, 'm0')['memory_max'], P.GIB)

    def test_workers_charge_sixteen_each(self):
        self.request(16, 'muse'); self.request(16, 'judge')
        self.request(6, 'gate')
        with self.assertRaisesRegex(ValueError, 'budget'): self.request(16, 'argon')
        self.assertEqual(sum(x['memory_max'] for x in self.read()['leases'].values()), 38 * P.GIB)

    def test_load_external_reserve_and_stop_still_bind(self):
        for sample in [(80, 17, 1), (80, 1, 21), (24, 1, 1)]:
            with self.assertRaisesRegex(ValueError, 'L1'):
                P.reserve(self.camp, {'other_lanes':str(self.other)}, P.GIB, 'gate', sample=sample, app=(0, 0))
        self.other.write_text('broken')
        with self.assertRaisesRegex(ValueError, 'L1'): self.request(1)

    def test_limits_complete_and_exact(self):
        lease = self.request(6)
        argv = P.systemd_argv(lease, ['python3', '-c', 'pass'])
        self.assertIn('MemoryMax=' + str(6 * P.GIB), argv)
        self.assertIn('MemoryHigh=' + str(P.high_limit(6 * P.GIB)), argv)
        self.assertIn('MemorySwapMax=0', argv)
        self.assertIn('--slice=newton-ia.slice', argv)
        self.assertEqual(P.high_limit(16 * P.GIB), 12 * P.GIB)
        for field in ('memory_max', 'memory_swap_max'):
            missing = dict(lease); del missing[field]
            with self.assertRaises((KeyError, ValueError)): P.systemd_argv(missing, ['true'])
        for key in ['memory_max', 'memory_high', 'memory_swap_max']:
            bad = copy.deepcopy(lease); bad[key] = 17 * P.GIB
            with self.assertRaisesRegex(ValueError, 'memory_limit'): P.systemd_argv(bad, ['true'])
        with self.assertRaises(ValueError): self.request(17)

    def test_scope_name_and_manager_ownership_not_just_ledger_label(self):
        lease = self.request(1); run = mock.Mock()
        fake = dict(ActiveState='active', Description='unrelated', MemoryMax=str(P.GIB), MemoryHigh=str(P.GIB * 4 // 5), MemorySwapMax='0')
        with self.assertRaisesRegex(ValueError, 'ownership'): P.stop_owned(lease, lambda _:fake, run)
        run.assert_not_called()
        forged = dict(lease, scope='app-com.anthropic.Claude.scope')
        with self.assertRaises(ValueError): P.systemd_argv(forged, ['true'])

    def test_dead_lease_recovery_is_recorded_not_deleted_silently(self):
        lease = self.request(1); state = self.read()
        lease = state['leases'][lease['id']]; lease.update(owner_pid=999999999, owner_start='absent')
        P.recover_dead(state, properties=lambda _: {'ActiveState':'inactive'}, start=lambda _:None)
        self.assertEqual(state['leases'], {})
        self.assertEqual(state['history'][-1]['event'], 'recovered_dead_lease')

    def test_dead_parent_with_live_scope_remains_charged(self):
        self.request(1); state = self.read()
        P.recover_dead(state, properties=lambda _: {'ActiveState':'active'}, start=lambda _:None)
        self.assertEqual(len(state['leases']), 1)

    def test_unknown_or_failed_manager_query_keeps_charge(self):
        self.request(1)
        for response in ({}, {'ActiveState':'unknown'}):
            state=self.read()
            P.recover_dead(state, properties=lambda _:response, start=lambda _:None)
            self.assertEqual(len(state['leases']), 1)
        state=self.read()
        with mock.patch.object(P, 'scope_properties', side_effect=ValueError('query failed')):
            P.recover_dead(state, properties=P.scope_properties, start=lambda _:None)
        self.assertEqual(len(state['leases']), 1)
        with mock.patch.object(P.subprocess, 'run', return_value=__import__('subprocess').CompletedProcess([], 1, '', 'no bus')):
            with self.assertRaisesRegex(ValueError, 'unavailable'): P.scope_properties(next(iter(state['leases'].values()))['scope'])

    def test_worker_cannot_graphically_escape_its_owned_scope(self):
        import inspect
        self.assertIn("os.environ['OBRERO_VIA'] = 'directo'", inspect.getsource(R.main))

    def test_release_waits_for_proven_scope_teardown(self):
        lease = self.request(1)
        with mock.patch.object(P, 'scope_properties', side_effect=[{'ActiveState':'active'}, {'ActiveState':'inactive'}]), mock.patch.object(P.time, 'sleep'):
            P.release(self.camp, lease['id'])
        self.assertEqual(self.read()['leases'], {})
        self.assertEqual(self.read()['history'][-1]['event'], 'released')

    def test_release_never_frees_a_scope_still_live_after_wait(self):
        lease = self.request(1)
        with mock.patch.object(P, 'scope_properties', return_value={'ActiveState':'active'}), mock.patch.object(P.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'scope_still_live'):
                P.release(self.camp, lease['id'])
        self.assertIn(lease['id'], self.read()['leases'])

    def test_legacy_recovery_requires_the_recorded_absent_pid(self):
        old = {'schema':'cafe.lanes.v1','lanes':1,'writers':1}
        path = self.camp / 'LANES_CAFE.json'; path.write_text(json.dumps(old))
        watch = self.camp / 'watch.json'; watch.write_text(json.dumps(dict(job='cf_s1_g4d_write_w1', wave='cafe', pid=999999999)))
        with mock.patch.object(P, 'pid_start', return_value=None): P.recover_legacy(self.camp, watch)
        self.assertEqual(self.read()['history'][0]['pid'], 999999999)
        self.assertEqual(json.loads((self.camp/'out/M0/legacy_lanes_v1.json').read_text()), old)

    def test_no_foreign_stop_or_process_name_kill_in_policy(self):
        source = (ROOT/'ctl/memory_policy.py').read_text()
        self.assertNotIn('pkill', source); self.assertNotIn('killpg', source)
        self.assertNotIn('os.kill(', source)

    def test_wrappers_route_through_one_bounded_runner(self):
        for name in ['muse_lane.sh', 'unit.sh']:
            text = (ROOT/'ctl'/name).read_text()
            self.assertIn('run_capped.py', text); self.assertIn('--verify-active', text)
            self.assertNotIn('systemd-run', text)
        text = (ROOT/'ctl/link_queue.py').read_text()
        self.assertIn("'run_capped.py'", text); self.assertNotIn("['systemd-run'", text)

    def test_inside_rejects_missing_kernel_bounds(self):
        lease = self.request(1)
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path).replace('\\', '/') == '/proc/self/cgroup':
                return '0::/fixture/foreign.scope\n'
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read), self.assertRaisesRegex(ValueError, 'scope_mismatch'):
            P.verify_current(self.camp, lease['id'])


if __name__ == '__main__': unittest.main()
