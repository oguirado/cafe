"""Independent operational-claim, native-status, publication and clone-receipt negatives."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools'))
import claims_check as claims
import ci_support as ci
import run_wgc_probe as wgc
import verify_clean_clone as clone
import publication_lint as pub


class TestClaims(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.names=ci.prepared_inventory(ROOT)
  # Copy exactly the selected inventory, never discover source trees.
  for name in self.names:
   path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,path)
 def inspect(self):
  with mock.patch.dict(os.environ,{'CAFE_PREPARED_INVENTORY_JSON':json.dumps(self.names)}):return claims.inspect(self.root)
 def test_current_discovered_claims_and_all_ci_stages_pass(self):
  result=self.inspect();self.assertEqual(result['findings'],[]);self.assertEqual(result['ci_stages'],8)
  self.assertIn('README.md',result['checked_documents']);self.assertTrue(result['deferred'])
 def test_new_unlisted_document_with_missing_path_fails(self):
  name='docs/new_claim.md';path=self.root/name;path.parent.mkdir(exist_ok=True);path.write_text('Run `tools/does_not_exist.py`.\n');self.names=sorted(self.names+[name])
  self.assertEqual(self.inspect()['status'],'fail')
 def test_new_document_unknown_flag_fails(self):
  name='docs/new_claim.md';(self.root/name).write_text('Run `python3 -B tools/run_ci.py --invented-flag`.\n');self.names=sorted(self.names+[name])
  self.assertEqual(self.inspect()['status'],'fail')
 def test_unknown_tool_or_stage_in_new_claim_fails(self):
  for text in ['Run `imaginary-tool --bogus`.', 'Run `python3 -B tools/run_ci.py --stage invented`.', '```sh\nimaginary-tool --bogus\n```']:
   with self.subTest(text=text):
    (self.root/'README.md').write_text(text);self.assertEqual(self.inspect()['status'],'fail')
 def test_changed_inherited_document_is_checked(self):
  name='docs/architecture/ARCHITECTURE.v0.1.md';(self.root/name).write_text('Implemented `tools/missing.py`.\n')
  result=self.inspect();self.assertEqual(result['status'],'fail');self.assertIn(name,result['checked_documents'])
 def test_pinned_vendor_reference_deferral_requires_exact_bytes(self):
  name='ui/src/design/vendor/open-design-openai/USAGE.md'
  result=self.inspect()
  self.assertIn(name,[row['path'] for row in result['deferred']])
  (self.root/name).write_text('Run `tools/missing_vendor_command.py`.\n')
  result=self.inspect()
  self.assertEqual(result['status'],'fail')
  self.assertIn(name,result['checked_documents'])
  self.assertTrue(any(row['path']==name for row in result['findings']))
 def test_local_link_escape_or_missing_path_fails(self):
  for text in ['[missing](missing.md)','[escape](../../outside.md)','Run tools/does_not_exist.py now.','The flag --invented-flag is supported.']:
   with self.subTest(text=text):
    (self.root/'README.md').write_text(text);self.assertEqual(self.inspect()['status'],'fail')
 def test_action_tag_permission_or_missing_stage_fails(self):
  path=self.root/'.github/workflows/ci.yml';original=json.loads(path.read_text())
  for mutation in ['tag','write','empty']:
   data=copy.deepcopy(original)
   if mutation=='tag':data['jobs']['verify']['steps'][0]['uses']='actions/checkout@v4'
   elif mutation=='write':data['permissions']={'contents':'write'}
   else:data['jobs']['verify']['steps']=[s for s in data['jobs']['verify']['steps'] if not s.get('id','').startswith('gate_')]
   path.write_text(json.dumps(data));self.assertEqual(self.inspect()['status'],'fail')
 def test_unavailable_native_is_never_capture_pass(self):
  unavailable={'status':'not_available','reason':'platform_unsupported','profile':'synthetic_test','frames':0,'first_source_time':None,'last_source_time':None}
  self.assertEqual(wgc.validate_result(78,json.dumps(unavailable))['status'],'not_available')
  for rc,changes in [(0,{}),(78,{'frames':2}),(78,{'first_source_time':'1'}),(1,{'status':'pass'})]:
   with self.subTest(rc=rc,changes=changes),self.assertRaises(ValueError):wgc.validate_result(rc,json.dumps(dict(unavailable,**changes)))
 def test_real_native_pass_needs_two_monotonic_source_clocks(self):
  record={'status':'pass','reason':'owned_window_capture_verified','profile':'synthetic_test','frames':2,'first_source_time':'10','last_source_time':'11'}
  self.assertEqual(wgc.validate_result(0,json.dumps(record))['status'],'pass')
  for changes in [{'frames':1},{'last_source_time':'10'},{'first_source_time':'01'}]:
   with self.subTest(changes=changes),self.assertRaises(ValueError):wgc.validate_result(0,json.dumps(dict(record,**changes)))
 def test_export_cannot_mint_literal_clone_receipt(self):
  with self.assertRaises(ValueError):clone.expected_receipt(self.root)
  self.assertFalse((self.root/'.git').exists())
 def test_stale_or_forged_clone_receipt_fails(self):
  expected={'schema':'cafe.clean-clone-receipt.v1','head':'a'*40,'tree_sha256':'b'*64,'ci_profile_sha256':'c'*64,'mode':'real-git-clone','linux_ci_status':'pass'}
  path=self.root/'receipt.json';path.write_text(json.dumps(dict(expected,mode='clean-export')))
  with mock.patch.object(clone,'expected_receipt',return_value=expected),mock.patch.object(clone,'receipt_path',return_value=path),self.assertRaises(ValueError):clone.verify_receipt(self.root)
 def test_gitleaks_exception_is_whole_line_only(self):
  import tomllib
  data=tomllib.loads((ROOT/'.gitleaks.toml').read_text());allow=data['allowlist']
  self.assertNotIn('paths',allow);self.assertEqual(allow['regexTarget'],'line');patterns=allow['regexes'];self.assertTrue(all(p.startswith('^') and p.endswith('$') for p in patterns))
  line='SCHEMA_SHA256 = "'+'a'*64+'"';self.assertTrue(any(re.fullmatch(p,line) for p in patterns))
  token='ghp_'+'a1'*18;self.assertFalse(any(re.fullmatch(p,line+' '+token) for p in patterns));self.assertTrue(pub.CREDENTIAL.search(line+' '+token))
 def test_shipped_tokenizer_asset_digest(self):
  asset=ROOT/'tools/assets/tokenizer/fb374d419588a4632f3f557e76b4b70aebbca790'
  self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(),'446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d')


if __name__=='__main__':unittest.main()
