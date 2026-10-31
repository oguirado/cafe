"""Independent admission and wrapper failure contrasts without live workers."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from host_shell import run_bash
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cafe_lanes', ROOT / 'ctl/cafe_lanes.py')
lanes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lanes)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.other = self.base / 'other.json'
        self.other.write_text(json.dumps({'lanes': {'first': 4, 'second': 2}}))
        self.own = self.base / 'own.json'

    def action(self, action, count=0, sample=(60, 4, 5), writer=False):
        return lanes.transact(self.own, action, count, self.other, sample, writer=writer)

    def test_admission_and_release(self):
        self.assertTrue(self.action('request', 16)['admitted'])
        self.assertEqual(self.action('release', 30)['lanes'], 0)
        self.assertEqual(self.action('release', 1)['lanes'], 0)

    def test_ram_rejection(self):
        self.assertFalse(self.action('request', 6, (25, 1, 1))['admitted'])
        self.assertFalse(self.own.exists())

    def test_other_reserve_rejection(self):
        self.other.write_text(json.dumps({'lanes': {'first': 31}}))
        self.assertFalse(self.action('request', 2)['admitted'])
        self.assertTrue(self.action('request', 1)['admitted'])

    def test_corrupt_and_absent_other_reserve_maximum(self):
        for raw in ('broken', '{"lanes":{"first":true}}', '{"lanes":{"first":-1}}'):
            self.other.write_text(raw)
            result = self.action('request', 1)
            self.assertFalse(result['admitted'])
            self.assertEqual(result['other_live'], 40)
        self.other.unlink()
        self.assertFalse(self.action('request', 1)['admitted'])

    def test_corrupt_own_never_resets(self):
        self.own.write_text('broken')
        with self.assertRaises(ValueError):
            self.action('request', 1)
        self.assertEqual(self.own.read_text(), 'broken')

    def test_load_and_stop_hold(self):
        self.assertFalse(self.action('request', 1, (60, 16.01, 1))['admitted'])
        self.assertFalse(self.action('request', 1, (60, 1, 20.01))['admitted'])
        self.assertEqual(lanes.capacity(60, 1, 1, 0, 0, True), 0)
        self.assertEqual(lanes.capacity(24, 1, 1, 0, 0), 0)

    def test_writer_ram_and_cap(self):
        self.assertTrue(self.action('request', 6, writer=True)['admitted'])
        self.assertFalse(self.action('request', 1, writer=True)['admitted'])
        self.assertEqual(self.action('release', 1, writer=True)['writers'], 5)
        self.assertEqual(lanes.capacity(30, 1, 1, 0, 6), 1)

    def test_set_uses_admission_and_preserves_writers(self):
        self.action('request', 2, writer=True)
        self.assertFalse(self.action('set', 1)['admitted'])
        self.assertFalse(self.action('set', 25)['admitted'])
        self.assertEqual(self.action('status')['lanes'], 2)

    def test_only_link_rollbacks_hold_not_inherited_ci(self):
        self.assertFalse(lanes.link_hold(self.base))
        queue = self.base / 'LINK_QUEUE.json'
        queue.write_text(json.dumps({'schema': 'cafe.link-queue.v1', 'rollbacks_since_green': 1}))
        self.assertFalse(lanes.link_hold(self.base))
        queue.write_text(json.dumps({'schema': 'cafe.link-queue.v1', 'rollbacks_since_green': 2}))
        self.assertTrue(lanes.link_hold(self.base))
        queue.write_text('broken')
        with self.assertRaises(ValueError):
            lanes.link_hold(self.base)

    def test_formula_boundaries(self):
        self.assertEqual(lanes.capacity(60, 16, 20, 12, 6), 20)
        self.assertEqual(lanes.capacity(29.9, 1, 1, 0, 1), 8)
        for invalid in (float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                lanes.capacity(invalid, 1, 1, 0, 0)

    def test_atomic_failure_keeps_previous(self):
        self.action('request', 1)
        prior = self.own.read_bytes()
        with mock.patch.object(lanes.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.action('request', 1)
        self.assertEqual(self.own.read_bytes(), prior)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['other.json', 'own.json', 'own.json.lock'])

    def test_simultaneous_requests_cannot_overbook(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.action('request', 4), range(8)))
        self.assertEqual(sum(result['admitted'] for result in results), 6)
        self.assertEqual(self.action('status')['lanes'], 24)

    def test_wrapper_releases_on_launch_failure_and_dry_run(self):
        import os
        import shutil
        wrapper_root = self.base / 'wrapper'
        (wrapper_root / 'ctl').mkdir(parents=True)
        shutil.copy(ROOT / 'ctl/muse_lane.sh', wrapper_root / 'ctl/muse_lane.sh')
        gate = wrapper_root / 'ctl/run_capped.py'
        gate.write_text("import os,sys,subprocess\nfrom pathlib import Path\np=Path(os.environ['CAMP'])/'actions'\nif '--verify-active' in sys.argv: raise SystemExit(0)\nwith p.open('a') as f:f.write('reserve\\n')\nenv=dict(os.environ,CAFE_CAPPED_ACTIVE='fixture')\ntry: rc=subprocess.run(sys.argv[sys.argv.index('--')+1:],env=env).returncode\nfinally:\n with p.open('a') as f:f.write('release\\n')\nraise SystemExit(rc)\n")
        brief = self.base / 'brief.md'; brief.write_text('A fixed harmless brief.')
        (self.base / 'launch_muse.sh').write_text('exit 19\n', newline='\n')
        env = dict(os.environ, CAMP=str(self.base)); env.pop('CAFE_CAPPED_ACTIVE', None)
        script = wrapper_root / 'ctl/muse_lane.sh'
        for dry, expected in (('0', 19), ('1', 0)):
            result = run_bash([str(script), 'cf_probe', str(brief)],
                                    env=dict(env, CAFE_DRY_RUN=dry), capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual((self.base / 'actions').read_text().splitlines(),
                         ['reserve', 'release', 'reserve', 'release'])

    def test_wrapper_hands_gate_selected_bash_and_propagates_rc(self):
        import os
        from pathlib import PurePosixPath, PureWindowsPath
        import shutil
        wrapper_root = self.base / 'wrapper'
        (wrapper_root / 'ctl').mkdir(parents=True)
        shutil.copy(ROOT / 'ctl/muse_lane.sh', wrapper_root / 'ctl/muse_lane.sh')
        # The fixture gate records the exact command it is asked to run.
        (wrapper_root / 'ctl/run_capped.py').write_text("import json,os,sys,subprocess\nfrom pathlib import Path\nc=Path(os.environ['CAMP'])\nif '--verify-active' in sys.argv: raise SystemExit(0)\ncmd=sys.argv[sys.argv.index('--')+1:]\nwith (c/'argv.jsonl').open('a') as f:f.write(json.dumps(cmd)+'\\n')\nwith (c/'actions').open('a') as f:f.write('reserve\\n')\ntry: rc=subprocess.run(cmd,env=dict(os.environ,CAFE_CAPPED_ACTIVE='fixture')).returncode\nfinally:\n with (c/'actions').open('a') as f:f.write('release\\n')\nraise SystemExit(rc)\n")
        brief = self.base / 'brief.md'; brief.write_text('A fixed harmless brief.')
        env = dict(os.environ, CAMP=str(self.base)); env.pop('CAFE_CAPPED_ACTIVE', None)
        script = wrapper_root / 'ctl/muse_lane.sh'
        for launch_rc in (19, 42, 7):
            (self.base / 'launch_muse.sh').write_text("printf 'launched\\n' >> \"$CAMP/launched\"\nexit %d\n" % launch_rc, newline='\n')
            for dry, expected in (('0', launch_rc), ('1', 0)):
                with self.subTest(launch_rc=launch_rc, dry=dry):
                    result = run_bash([str(script), 'cf_probe', str(brief)],
                                      env=dict(env, CAFE_DRY_RUN=dry), capture_output=True)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if dry == '1':
                        self.assertIn(b'admitted dry-run; no worker started', result.stdout)
        self.assertEqual((self.base / 'actions').read_text().splitlines(), ['reserve', 'release'] * 6)
        # Dry runs never start the launcher; real runs start it exactly once each.
        self.assertEqual((self.base / 'launched').read_text().splitlines(), ['launched'] * 3)
        commands = [json.loads(line) for line in (self.base / 'argv.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 6)
        for command in commands:
            interpreter = command[0]
            # Never a bare name a native gate would resolve through the system
            # directory; never the Windows system (WSL) launcher.
            self.assertNotEqual(interpreter, 'bash')
            self.assertTrue(PurePosixPath(interpreter).is_absolute() or PureWindowsPath(interpreter).is_absolute(), interpreter)
            self.assertNotIn('system32', [part.casefold() for part in PureWindowsPath(interpreter).parts])
            self.assertTrue(Path(interpreter).is_file() or Path(interpreter + '.exe').is_file(), interpreter)
            self.assertEqual(Path(command[1]).name, 'muse_lane.sh')
            self.assertEqual(command[2], 'cf_probe')
            self.assertEqual(Path(command[3]).resolve(), brief.resolve())
            self.assertEqual(len(command), 4)

    def test_wrapper_syntax_and_volatile_refusal(self):
        script = ROOT / 'ctl/muse_lane.sh'
        result = run_bash(['-n', str(script)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        for text in ('process pid 123', 'date 2026-10-28T12:00:00'):
            brief = self.base / 'brief.md'; brief.write_text(text)
            result = run_bash([str(script), 'cf_probe', str(brief)],
                                    env={'PATH': __import__('os').environ['PATH'], 'CAMP': str(self.base)},
                                    capture_output=True)
            self.assertEqual(result.returncode, 67)
            self.assertFalse((self.base / 'LANES_CAFE.json').exists())


if __name__ == '__main__':
    unittest.main()
