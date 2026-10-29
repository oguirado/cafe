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
 def test_stage_equals_duplicate_and_missing_value_are_not_proven(self):
  for command in ['python3 tools/run_ci.py --stage=invented','python3 tools/run_ci.py --stage=rust','python3 tools/run_ci.py --stage rust --stage invented','python3 tools/run_ci.py --stage rust --stage','python3 tools/run_ci.py --stage']:
   with self.subTest(command=command):
    (self.root/'README.md').write_text('Run `'+command+'`.\n')
    self.assertEqual(self.inspect()['status'],'fail')
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


class TestStrictCommandClaims(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.names={'Cargo.toml','tests/existing.js','tests/existing.rs'}
  (self.root/'tests').mkdir()
  (self.root/'tests/existing.js').write_text('/* named test fixture */\n')
  (self.root/'tests/existing.rs').write_text('#[test] fn existing() {}\n')
  self.manifest()
 def manifest(self,path='tests/existing.rs',extra=''):
  (self.root/'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.1.0"\n[[test]]\nname="existing"\npath='+json.dumps(path)+'\n'+extra)
 def reject(self,command):
  with self.assertRaises(ValueError,msg=command):claims.command_claim(self.root,command,self.names)
 def test_exact_named_file_and_declared_target_pass(self):
  for command in ['node --test tests/existing.js','cargo test --test existing','cargo test --locked --offline --test existing','cargo test --target aarch64-unknown-linux-gnu --test existing']:
   with self.subTest(command=command):claims.command_claim(self.root,command,self.names)
 def test_node_directory_absent_from_inventory_rejected(self):
  (self.root/'tests/folder.js').mkdir()
  self.reject('node --test tests/folder.js')
  self.names.add('tests/folder.js');self.reject('node --test tests/folder.js')
 def test_node_requires_exactly_one_inventory_file(self):
  (self.root/'tests/unlisted.js').write_text('')
  for command in ['node --test','node --test tests/existing.js tests/existing.js','node --test tests/unlisted.js','node --test tests/missing.js','node --test tests/*.js','node --test tests/existing.rs','node --test=tests/existing.js','node --test --invented-flag tests/existing.js','node --test params.json','node --test target/debug/cafe-wgc-synthetic.exe']:
   with self.subTest(command=command):self.reject(command)
 def test_node_alias_absolute_and_symlink_paths_rejected(self):
  (self.root/'tests/link.js').symlink_to('existing.js');self.names.add('tests/link.js')
  (self.root/'tests/loop.js').symlink_to('loop.js');self.names.add('tests/loop.js')
  (self.root/'alias').symlink_to(self.root/'tests',target_is_directory=True);self.names.add('alias/existing.js')
  os.link(self.root/'tests/existing.js',self.root/'tests/hard.js');self.names.add('tests/hard.js')
  for path in ['tests/loop.js','tests/link.js','alias/existing.js','tests/hard.js','tests/existing.js','./tests/existing.js','tests/../tests/existing.js','tests//existing.js','../escape.js',str(self.root/'tests/existing.js'),r'tests\existing.js']:
   with self.subTest(path=path):self.reject('node --test '+json.dumps(path))
 def test_cargo_equals_duplicates_and_wrong_subcommands_rejected(self):
  for command in ['cargo test --test=missing','cargo test --test=existing','cargo test --test existing --test missing','cargo test --test existing --test existing','cargo build --test existing','cargo check --test existing','cargo --test existing','cargo test --test','cargo test --test missing','cargo test --tests --test existing','cargo test -- --test existing','cargo test --test existing --invented-flag','cargo test --test existing extra','cargo test --bin existing --test existing']:
   with self.subTest(command=command):self.reject(command)
 def test_cargo_removed_declared_source_rejected(self):
  (self.root/'tests/existing.rs').unlink()
  self.reject('cargo test --test existing')
 def test_cargo_target_must_be_uniquely_declared(self):
  self.manifest(extra='[[test]]\nname="existing"\npath="tests/existing.rs"\n')
  self.reject('cargo test --test existing')
  (self.root/'Cargo.toml').write_text('# [[test]]\n# name="existing"\n# path="tests/existing.rs"\n')
  self.reject('cargo test --test existing')
 def test_cargo_unsafe_missing_unlisted_directory_sources_rejected(self):
  (self.root/'tests/folder.rs').mkdir()
  (self.root/'tests/unlisted.rs').write_text('')
  (self.root/'tests/link.rs').symlink_to('existing.rs')
  (self.root/'alias').symlink_to(self.root/'tests',target_is_directory=True)
  self.names.update({'tests/folder.rs','tests/link.rs','alias/existing.rs'})
  for path in ['../escape.rs','/escape.rs',str(self.root/'tests/existing.rs'),'tests/../tests/existing.rs','./tests/existing.rs','tests//existing.rs',r'tests\existing.rs','tests/missing.rs','tests/unlisted.rs','tests/folder.rs','tests/link.rs','alias/existing.rs','tests/existing.js','params.json']:
   with self.subTest(path=path):
    self.manifest(path);self.reject('cargo test --test existing')
  os.link(self.root/'tests/existing.rs',self.root/'tests/hard.rs');self.names.add('tests/hard.rs')
  self.manifest('tests/hard.rs');self.reject('cargo test --test existing')
 def test_cargo_manifest_itself_requires_inventory_regular_file(self):
  self.names.remove('Cargo.toml');self.reject('cargo test --test existing')
 def test_cargo_bin_selector_also_requires_declared_source(self):
  (self.root/'Cargo.toml').write_text('[[bin]]\nname="existing"\npath="tests/existing.rs"\n')
  claims.command_claim(self.root,'cargo build --locked --bin existing',self.names)
  self.reject('cargo build --bin missing')
  (self.root/'tests/existing.rs').unlink();self.reject('cargo build --bin existing')
 def flags(self,source):
  path=self.root/'cli.py';path.write_text(source);return claims.argument_flags(path)
 def test_literal_argparse_flags_and_finite_tuple_loop(self):
  source="p.add_argument('--literal')\nfor name in ('alpha', 'beta-flag'):\n p.add_argument('--'+name, required=True)\n"
  self.assertEqual(self.flags(source),{'--literal','--alpha','--beta-flag'})
 def test_finite_loop_contrasts_do_not_infer_flags(self):
  for source in [
   "for name in dynamic_names:\n p.add_argument('--'+name)\n",
   "names=('alpha',)\nfor name in names:\n p.add_argument('--'+name)\n",
   "for name in ['alpha']:\n p.add_argument('--'+name)\n",
   "for name in ('alpha',):\n name='bogus'\n p.add_argument('--'+name)\n",
   "for name in ('alpha',):\n p.add_argument('--'+name)\n mutate()\n",
   "for name in ('alpha',):\n p.add_argument('--'+name, default=(name:='bogus'))\n",
   "for name in ('alpha',):\n p.add_argument('--'+name, default=rebind())\n",
   "for name in ('alpha',):\n p.add_argument('--'+name, **options)\n",
   "for name in ('alpha',):\n p.add_argument('--'+name, *options)\n",
   "for name in ('alpha',):\n p.add_argument('--'+name)\nelse:\n mutate()\n",
   "for name in ('alpha', dynamic_name):\n p.add_argument('--'+name)\n",
   "for name in ('alpha',):\n p.add_argument('--'+name, '--alias')\n",
   "for name in ('alpha',):\n p.add_argument(f'--{name}')\n",
   "for p in dynamic_parsers:\n for name in ('alpha',):\n  p.add_argument('--'+name)\n",
   "for name in ('alpha',):\n name.add_argument('--'+name)\n",
  ]:
   with self.subTest(source=source):self.assertNotIn('--alpha',self.flags(source))
 def test_finite_flags_remain_local_to_python_entrypoint(self):
  (self.root/'cli.py').write_text("for name in ('alpha', 'beta'):\n p.add_argument('--'+name)\n")
  self.names.add('cli.py')
  claims.command_claim(self.root,'python3 cli.py --alpha value --beta value',self.names)
  self.reject('python3 cli.py --invented-flag')


class TestClaimDiscoveryRepair(unittest.TestCase):
 # Reuse fixture setup without rerunning inherited test methods.
 setUp=TestClaims.setUp
 inspect=TestClaims.inspect
 def test_actual_connector_cli_flags_are_discovered(self):
  (self.root/'README.md').write_text('Use `python3 connectors/cafe/twitch_storage.py --channel fixture --data fixture`.\nUse `python3 connectors/cafe/mcp_server.py --data fixture`.\n')
  self.assertEqual(self.inspect()['findings'],[])
  (self.root/'README.md').write_text('Use `python3 connectors/cafe/mcp_server.py --channel fixture`.\n')
  self.assertEqual(self.inspect()['status'],'fail')
 def test_finite_loop_discovery_in_tools_and_connectors(self):
  for name in ['tools/finite_fixture.py','connectors/cafe/finite_fixture.py']:
   with self.subTest(name=name):
    (self.root/name).write_text("for name in ('finite-claim-fixture',):\n p.add_argument('--'+name)\n")
    self.names=sorted(set(self.names)|{name})
    (self.root/'README.md').write_text('Use `python3 '+name+' --finite-claim-fixture value`.\n')
    self.assertEqual(self.inspect()['findings'],[])
    (self.root/name).write_text("for name in dynamic_names:\n p.add_argument('--'+name)\n")
    self.assertEqual(self.inspect()['status'],'fail')
    self.names.remove(name)
 def test_node_and_cargo_claims_in_markdown_and_workflow(self):
  (self.root/'README.md').write_text('Run `node --test tests/security/control.test.js`.\n```bash\ncargo test --test journal_recovery\n```\n')
  self.assertEqual(self.inspect()['findings'],[])
  for text in ['Run `node --test tests/security/missing.js`.','```bash\ncargo test --test=missing\n```','```text\nnode --test tests/security/missing.js\n```']:
   with self.subTest(text=text):
    (self.root/'README.md').write_text(text);self.assertEqual(self.inspect()['status'],'fail')
  (self.root/'README.md').write_text('No command claims.\n')
  path=self.root/'.github/workflows/ci.yml';data=json.loads(path.read_text())
  data['jobs']['verify']['steps'].append({'run':'node --test tests/security/missing.js'})
  path.write_text(json.dumps(data));self.assertEqual(self.inspect()['status'],'fail')


if __name__=='__main__':unittest.main()
