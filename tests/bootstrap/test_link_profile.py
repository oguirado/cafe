"""Generated metadata versioning; no Git process or real command is executed."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

root=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('link_profile',root/'ctl/link_queue_v2.py')
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)
queue=profile.queue


class LinkProfileTests(unittest.TestCase):
    def test_only_exact_coordinate_artifact_added(self):
        self.assertEqual(queue.SHARED_GENERATED-profile.ORIGINAL_SHARED,{'catalogue/coordinates.jsonl'})
        self.assertTrue(profile.ORIGINAL_SHARED<=queue.SHARED_GENERATED)
        self.assertNotIn('core/src/main.rs',queue.SHARED_GENERATED)
        self.assertNotIn('catalogue/arbitrary.jsonl',queue.SHARED_GENERATED)
        self.assertEqual(hashlib.sha256((root/'ctl/link_queue.py').read_bytes()).hexdigest(),profile.EXPECTED_BASE_SHA256)

    def test_two_serial_units_share_coordinates_only_in_final_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);repo=base/'repo';repo.mkdir();camp=base/'camp';camp.mkdir();(camp/'PARADA.txt').write_text('LIBRE')
            (repo/'catalogue').mkdir();coordinate=repo/'catalogue/coordinates.jsonl';coordinate.write_bytes(b'zero\n')
            selected=[]
            def fake_git(_repo,*args):
                if args[0]=='show':raise subprocess.CalledProcessError(1,list(args))
                if args[0]=='apply' and '--check' not in args:
                    for name in selected:(repo/name).write_bytes(b'after\n' if not name.startswith('catalogue/') else (b'one\n' if 'first.txt' in selected else b'two\n'))
                return b''
            with mock.patch.object(queue,'patch_paths',side_effect=lambda *_:list(selected)),mock.patch.object(queue,'git',side_effect=fake_git):
                for unit,code in [('alpha','first.txt'),('beta','second.txt')]:
                    (repo/code).write_bytes(b'before\n');selected[:]=[code,'catalogue/coordinates.jsonl']
                    patch=base/(unit+'.patch');patch.write_bytes(unit.encode())
                    preimages={name:{'sha256':hashlib.sha256((repo/name).read_bytes()).hexdigest(),'mode':stat.S_IMODE((repo/name).stat().st_mode)} for name in selected}
                    proof=base/(unit+'.json');proof.write_text(json.dumps({'verdict':'PASS','patch_sha256':hashlib.sha256(patch.read_bytes()).hexdigest(),'preimages':preimages}))
                    queue.append(repo,camp,unit,unit,patch,proof,'Implement '+unit)
                    self.assertEqual(queue.apply_next(repo,camp,lambda *_:0)['status'],'linked')
                window=queue.window(camp,repo);text=Path(window['path']).read_text();commits=[line for line in text.splitlines() if line.startswith('git commit')]
                self.assertEqual(len(commits),3)
                self.assertNotIn('coordinates.jsonl',commits[0]);self.assertNotIn('coordinates.jsonl',commits[1]);self.assertIn('coordinates.jsonl',commits[2])
                self.assertEqual(coordinate.read_bytes(),b'two\n')
                self.assertEqual((repo/'first.txt').read_bytes(),b'after\n');self.assertEqual((repo/'second.txt').read_bytes(),b'after\n')

    def test_changed_base_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            (directory/'link_queue_v2.py').write_bytes((root/'ctl/link_queue_v2.py').read_bytes())
            (directory/'link_queue.py').write_text("raise AssertionError('untrusted_base_executed')\n")
            spec=importlib.util.spec_from_file_location('changed_profile',directory/'link_queue_v2.py')
            changed=importlib.util.module_from_spec(spec)
            with self.assertRaisesRegex(RuntimeError,'base_link_tool_version_changed'):
                spec.loader.exec_module(changed)
