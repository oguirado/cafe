"""Frozen approved Node graph admission and offline provisioning contrasts."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import clean_export_test as gate

APPROVED={'typescript':'5.9.3','vue':'3.5.43','dockview-core':'8.4.1','dockview-vue':'8.4.1',
 'openvue':'1.0.0','@openvue/icons':'1.0.0','@openvue/themes':'1.0.0','electron':'44.9.2',
 'vite':'8.3.4','@vitejs/plugin-vue':'6.0.9','@electron/packager':'20.3.0','@electron/asar':'4.3.1','@electron/fuses':'2.1.3'}

class NodeProfileTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.source=Path(self.temp.name)/'source';self.destination=Path(self.temp.name)/'destination'
  for root in [self.source,self.destination]:
   (root/'tools').mkdir(parents=True)
   for name in ['package.json','package-lock.json','tools/node_profile.json']:
    shutil.copyfile(ROOT/name,root/name)
 def mutate(self,name,operation):
  target=self.source/name;value=json.loads(target.read_text());operation(value);target.write_text(json.dumps(value))
 def test_exact_manual_direct_inventory_and_audited_closure(self):
  manifest=json.loads((self.source/'package.json').read_text())
  self.assertEqual({**manifest['dependencies'],**manifest['devDependencies']},APPROVED)
  self.assertEqual(gate.validate_node_profile(self.source),'frozen-shell-profile')
  self.assertEqual(len(json.loads((self.source/'package-lock.json').read_text())['packages'])-1,125)
 def test_extra_root_dependency_kinds_are_rejected(self):
  for key in ['optionalDependencies','peerDependencies']:
   with self.subTest(key=key):
    shutil.copyfile(ROOT/'package.json',self.source/'package.json')
    self.mutate('package.json',lambda row:row.update({key:{'unapproved':'1.0.0'}}))
    with self.assertRaises(ValueError):gate.validate_node_profile(self.source)
 def test_missing_or_changed_full_policy_fails_closed(self):
  target=self.source/'tools/node_profile.json';raw=target.read_bytes();target.unlink()
  with self.assertRaises(FileNotFoundError):gate.validate_node_profile(self.source)
  target.write_bytes(raw+b' ')
  with self.assertRaises(ValueError):gate.validate_node_profile(self.source)
 def test_unknown_or_floating_direct_pin_rejected(self):
  for value in ['^44.9.2','44.9.3']:
   with self.subTest(value=value):
    shutil.copyfile(ROOT/'package.json',self.source/'package.json')
    self.mutate('package.json',lambda row:row['devDependencies'].update(electron=value))
    with self.assertRaises(ValueError):gate.validate_node_profile(self.source)
 def test_changed_lock_excluded_package_or_unapproved_resolution_rejected(self):
  changes=[lambda row:row['packages'].update({'node_modules/node-pty':{'version':'1.0.0'}}),
   lambda row:row['packages']['node_modules/electron'].update(resolved='git+https://example.invalid/repo'),
   lambda row:row['packages']['node_modules/electron'].update(resolved='file:artifact.tgz'),
   lambda row:row['packages']['node_modules/@electron/fuses'].update(integrity='sha512-invalid')]
  for change in changes:
   with self.subTest(mutation=changes.index(change)):
    shutil.copyfile(ROOT/'package-lock.json',self.source/'package-lock.json');self.mutate('package-lock.json',change)
    with self.assertRaises(ValueError):gate.validate_node_profile(self.source)
 def test_missing_approved_cache_and_project_config_fail_before_execution(self):
  with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(gate,'command') as command:
   with self.assertRaisesRegex(ValueError,'cache_unavailable'):gate.provision_node_modules(self.source,self.destination)
   command.assert_not_called()
  (self.source/'.npmrc').write_text('must not be read')
  with mock.patch.object(gate,'command') as command:
   with self.assertRaisesRegex(ValueError,'config_not_opened'):gate.provision_node_modules(self.source,self.destination)
   command.assert_not_called()
 def test_offline_ignore_scripts_empty_configs_and_no_auth_environment(self):
  cache=Path(self.temp.name)/'cache';cache.mkdir();calls=[]
  def command(args,cwd,env):
   calls.append(args)
   self.assertNotIn('NPM_TOKEN',env);self.assertNotIn('NODE_OPTIONS',env)
   self.assertEqual(Path(env['NPM_CONFIG_USERCONFIG']).read_bytes(),b'')
   self.assertEqual(Path(env['NPM_CONFIG_GLOBALCONFIG']).read_bytes(),b'')
   self.assertEqual(env['NPM_CONFIG_OFFLINE'],'true')
   return {'rc':0,'stdout':'v24.14.0\n' if '--version' in args else ''}
  with mock.patch.dict(os.environ,{'CAFE_NPM_CACHE':str(cache),'NPM_TOKEN':'synthetic','NODE_OPTIONS':'synthetic'}),mock.patch.object(gate.shutil,'which',side_effect=lambda name:name),mock.patch.object(gate,'command',side_effect=command),mock.patch.object(gate,'verify_installed_node_profile'):
   self.assertEqual(gate.provision_node_modules(self.source,self.destination),'frozen-shell-profile')
  self.assertEqual(calls[1][1:],['ci','--offline','--ignore-scripts','--no-audit','--no-fund'])
 def test_legacy_profile_is_exact_and_rejects_extra_installed_packages(self):
  lock={'packages':{'':{'dependencies':{'typescript':'5.9.3'}},'node_modules/typescript':{'version':'5.9.3'}}}
  (self.source/'package.json').write_text(json.dumps({'dependencies':{'typescript':'5.9.3'}}))
  (self.source/'package-lock.json').write_text(json.dumps(lock))
  self.assertEqual(gate.validate_node_profile(self.source),'legacy-typescript-only')
  module=self.source/'node_modules/typescript';module.mkdir(parents=True)
  (module/'package.json').write_text(json.dumps({'name':'typescript','version':'5.9.3'}))
  gate.verify_installed_node_profile(self.source)
  (self.source/'node_modules/unapproved').mkdir()
  with self.assertRaises(ValueError):gate.verify_installed_node_profile(self.source)
 def test_installed_package_version_or_missing_required_package_rejected(self):
  (self.source/'node_modules').mkdir()
  with self.assertRaises(ValueError):gate.verify_installed_node_profile(self.source)
  module=self.source/'node_modules/electron';module.mkdir()
  (module/'package.json').write_text(json.dumps({'name':'electron','version':'0.0.0'}))
  with self.assertRaises(ValueError):gate.verify_installed_node_profile(self.source)
