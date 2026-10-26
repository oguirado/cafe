"""Clean-export selection and hook rejection contrasts, separate from real CI evidence."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import tomllib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools'))
import ci_support as ci
import clean_export_test as export
import install_pre_push as installer


class TestCleanExport(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'repo';self.root.mkdir();self.names=ci.prepared_inventory(ROOT)
  for name in self.names:
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
 def test_exact_selected_files_only_and_cleanup_on_success(self):
  (self.root/'local').mkdir();(self.root/'local/ignored.json').write_text('ignored')
  seen=[]
  def profile(root,names):
   seen.append(root)
   self.assertEqual(names,self.names);self.assertFalse((root/'local').exists());self.assertFalse((root/'.git').exists())
   self.assertEqual(ci.tree_digest(root,names),ci.tree_digest(self.root,self.names))
   return {'status':'pass','records':[]}
  with mock.patch.dict(os.environ,{'CAFE_PREPARED_INVENTORY_JSON':json.dumps(self.names)}),mock.patch.object(export,'provision_node_modules'),mock.patch.object(export,'run_profile',side_effect=profile):result=export.clean_export(self.root)
  self.assertEqual(result['mode'],'clean-export test');self.assertIn('PENDING',result['literal_clean_clone']);self.assertTrue(result['cleanup']);self.assertFalse(seen[0].exists())
 def test_export_cleanup_even_when_gate_raises(self):
  seen=[]
  def fail(root,names):seen.append(root);raise ValueError('injected_gate_failure')
  with mock.patch.dict(os.environ,{'CAFE_PREPARED_INVENTORY_JSON':json.dumps(self.names)}),mock.patch.object(export,'provision_node_modules'),mock.patch.object(export,'run_profile',side_effect=fail),self.assertRaises(ValueError):export.clean_export(self.root)
  self.assertFalse(seen[0].exists())
 def test_unknown_or_empty_profile_is_not_pass(self):
  with mock.patch.object(export,'ci_profile',return_value=[]),self.assertRaises(ValueError):export.run_profile(self.root,self.names)
  with mock.patch.object(export,'ci_profile',return_value=['python3 -B tools/run_ci.py --invented rust']),self.assertRaises(ValueError):export.run_profile(self.root,self.names)
 def test_hook_installer_refuses_external_path_and_conflicting_hook(self):
  (self.root/'.git/hooks').mkdir(parents=True)
  result=mock.Mock(stdout='../external/pre-push\n')
  with mock.patch.object(installer.subprocess,'run',return_value=result),self.assertRaises(ValueError):installer.install(self.root)
  target=self.root/'.git/hooks/pre-push';target.write_text('previous operator hook\n');result.stdout='.git/hooks/pre-push\n'
  with mock.patch.object(installer.subprocess,'run',return_value=result),self.assertRaises(ValueError):installer.install(self.root)
  self.assertEqual(target.read_text(),'previous operator hook\n')
 def test_hook_rejects_real_credential_before_missing_clone_receipt(self):
  token='gh'+'p_'+secrets.token_hex(18);(self.root/'README.md').write_text('Synthetic security contrast: '+token+'\n')
  env=ci.source_environment(self.root,self.names)
  result=subprocess.run(['sh','tools/hooks/pre-push'],cwd=self.root,env=env,capture_output=True,text=True,timeout=30)
  self.assertNotEqual(result.returncode,0)
  data=json.loads(result.stdout);self.assertEqual(data['status'],'fail')
  self.assertIn('credential',{row['rule'] for row in data['findings']})
  self.assertNotIn('clean-clone',result.stdout);self.assertNotIn(token,result.stdout+result.stderr)


 def test_git_inventory_cache_is_ignored_directory_and_inventory_stays_exact(self):
  source=self.root/'node_modules/typescript';source.mkdir(parents=True)
  (source/'package.json').write_text(json.dumps({'version':'5.9.3'}))
  clone=Path(self.temp.name)/'git-fixture';clone.mkdir()
  # Reproduce Git ignore semantics without creating any commit or requiring a source .git.
  subprocess.run(['git','init','--quiet',str(clone)],check=True,capture_output=True)
  (clone/'.gitignore').write_bytes((ROOT/'.gitignore').read_bytes())
  (clone/'package-lock.json').write_bytes((ROOT/'package-lock.json').read_bytes())
  names=ci.prepared_inventory(clone)
  export.provision_node_modules(self.root,clone)
  self.assertFalse((clone/'node_modules').is_symlink())
  self.assertEqual(ci.prepared_inventory(clone),names)
  self.assertEqual(subprocess.run(['git','check-ignore','-q','node_modules'],cwd=clone).returncode,0)
 def test_metadata_exceptions_reject_appended_credentials(self):
  patterns=tomllib.loads((ROOT/'.gitleaks.toml').read_text())['allowlist']['regexes']
  lines=[(ROOT/'catalogue/router_sources.json').read_text().rstrip('\n'),next(v for v in (ROOT/'tests/bootstrap/test_extract_rust.py').read_text().splitlines() if v.startswith('SOURCE_HASHES = ')),next(v for v in (ROOT/'requirements-ci.txt').read_text().splitlines() if re.fullmatch(r'\s*--hash=sha256:[a-f0-9]{64}\s*\\?\s*',v))]
  for line in lines:
   for prefix in ['', '\n']:
    with self.subTest(metadata_shape=True,prefix=bool(prefix)):self.assertTrue(any(re.fullmatch(p,prefix+line) for p in patterns))
    token='gh'+'p_'+secrets.token_hex(18)
    with self.subTest(appended_credential=True,prefix=bool(prefix)):self.assertFalse(any(re.fullmatch(p,prefix+line+' '+token) for p in patterns))
  secret='secret_'+'key = "'+secrets.token_hex(32)+'"'
  self.assertFalse(any(re.fullmatch(p,secret) for p in patterns))


if __name__=='__main__':unittest.main()
