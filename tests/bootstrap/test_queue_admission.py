"""No-Git queue verification; only Git plumbing is mocked, not admission or state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('queue_without_git',Path(__file__).resolve().parents[2]/'ctl/link_queue.py')
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)


class QueueAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name);self.repo=self.base/'repo';self.repo.mkdir();self.camp=self.base/'camp';self.camp.mkdir();(self.camp/'PARADA.txt').write_text('LIBRE')
        self.file=self.repo/'sample.txt';self.file.write_bytes(b'before\n')
        self.patch=self.base/'unit.patch';self.patch.write_bytes(b'passive patch data')
        self.evidence=self.base/'proof.json'
        self.proof={'verdict':'PASS','patch_sha256':hashlib.sha256(self.patch.read_bytes()).hexdigest(),'preimages':{'sample.txt':{'sha256':hashlib.sha256(b'before\n').hexdigest(),'mode':stat.S_IMODE(self.file.stat().st_mode)}}}
        self.paths=mock.patch.object(queue,'patch_paths',return_value=['sample.txt']);self.paths.start();self.addCleanup(self.paths.stop)
        self.git=mock.patch.object(queue,'git',side_effect=self.git_plumbing);self.git.start();self.addCleanup(self.git.stop)

    def git_plumbing(self,repo,*args):
        if args==('apply',str(self.camp/'out/link/patches/alpha.patch')):
            self.file.write_bytes(b'after\n')
        elif args and args[0]=='show':
            raise ValueError('no committed fixture')
        return b''

    def append(self):
        self.evidence.write_text(json.dumps(self.proof))
        return queue.append(self.repo,self.camp,'alpha','runtime',self.patch,self.evidence,'Implement runtime')

    def test_exact_preimages_accepted_and_persisted(self):
        row=self.append();self.assertEqual(row['preimages'],self.proof['preimages']);self.assertEqual(row['status'],'queued')

    def test_missing_preimages_rejected_without_ledger(self):
        self.proof.pop('preimages')
        with self.assertRaisesRegex(ValueError,'preimages'):self.append()
        self.assertFalse((self.camp/'LINK_QUEUE.json').exists())

    def test_mismatched_hash_or_mode_rejected(self):
        for field,value in [('sha256','0'*64),('mode',0)]:
            old=self.proof['preimages']['sample.txt'][field];self.proof['preimages']['sample.txt'][field]=value
            with self.assertRaisesRegex(ValueError,'preimages'):self.append()
            self.proof['preimages']['sample.txt'][field]=old

    def test_unreviewed_new_preimage_is_not_substituted(self):
        self.file.write_bytes(b'operator new work\n')
        with self.assertRaisesRegex(ValueError,'preimages'):self.append()
        self.assertEqual(self.file.read_bytes(),b'operator new work\n')

    def test_green_and_red_actual_filesystem_effects(self):
        self.append();row=queue.apply_next(self.repo,self.camp,lambda *_:0)
        self.assertEqual(row['status'],'linked');self.assertEqual(self.file.read_bytes(),b'after\n')

    def test_red_rolls_back_only_owned_preimage(self):
        operator=self.repo/'operator.txt';operator.write_bytes(b'keep')
        self.append();row=queue.apply_next(self.repo,self.camp,lambda *_:1)
        self.assertEqual(row['status'],'failed');self.assertEqual(self.file.read_bytes(),b'before\n');self.assertEqual(operator.read_bytes(),b'keep')
        self.assertEqual(json.loads((self.camp/'LINK_QUEUE.json').read_text())['rollbacks_since_green'],1)

    def test_stale_patch_preimage_and_unsafe_paths_fail_closed(self):
        self.append();self.file.write_bytes(b'new')
        with self.assertRaisesRegex(ValueError,'preimage'):queue.apply_next(self.repo,self.camp,lambda *_:0)
        for path in ['../outside','.git/config','local/private','target/binary']:
            with self.assertRaises(ValueError):queue.safe_path(self.repo,path)


if __name__=='__main__':unittest.main()
