"""Explicit no-Git prepared inventory contracts; no real Git calls in these tests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import ci_support as ci
import qualify_cafe_router as q


class TestPreparedInventory(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  (self.root/'README.md').write_text('Public fixture\n');self.names=['README.md']
 def test_inline_inventory_is_explicit_and_sorted(self):
  with mock.patch.dict(os.environ,{'CAFE_PREPARED_INVENTORY_JSON':json.dumps(self.names)},clear=True):
   self.assertEqual(ci.prepared_inventory(self.root),self.names)
   self.assertEqual(q.load_inventory(self.root,None)[0],self.names)
 def test_absent_empty_malformed_or_unsafe_inventory_fails(self):
  for value in [None,'[]','not json',json.dumps(['../outside']),json.dumps(['/absolute']),json.dumps(['missing.md']),json.dumps(['README.md','README.md'])]:
   with self.subTest(value=value),mock.patch.dict(os.environ,{} if value is None else {'CAFE_PREPARED_INVENTORY_JSON':value},clear=True):
    with self.assertRaises((ValueError,json.JSONDecodeError)):ci.prepared_inventory(self.root)
 def test_ignored_and_sensitive_paths_are_rejected_before_reading(self):
  for name in ['local/config.json','node_modules/file.js','target/item','_scratch/item','.git/config','.env','auth.json']:
   path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('not public')
   with self.subTest(name=name),self.assertRaises(ValueError):ci.validate_inventory(self.root,[name])
 def test_symlink_is_not_a_prepared_source(self):
  (self.root/'alias.md').symlink_to(self.root/'README.md')
  with self.assertRaises(ValueError):ci.validate_inventory(self.root,['alias.md'])
 def test_real_git_remains_authoritative_over_inline(self):
  (self.root/'.git').mkdir()
  result=mock.Mock(stdout=b'README.md\0')
  with mock.patch.dict(os.environ,{'CAFE_PREPARED_INVENTORY_JSON':'[]'}),mock.patch.object(ci.subprocess,'run',return_value=result) as run:
   self.assertEqual(ci.prepared_inventory(self.root),['README.md'])
   self.assertEqual(run.call_args.args[0][3:],['ls-files','--cached','--others','--exclude-standard','-z'])
 def test_source_environment_drops_external_configuration(self):
  with mock.patch.dict(os.environ,{'CAFE_ROUTER_INVENTORY':'outside','CAFE_INTEGRATION_CONFIG':'outside','CAFE_AUTHORITY_FIXTURE':'outside','TIKTOKEN_CACHE_DIR':'outside','CAFE_PRIVATE_POLICY':'outside','CARGO_HOME':'approved-cache'}):
   env=ci.source_environment(self.root,self.names)
  for key in ['CAFE_ROUTER_INVENTORY','CAFE_INTEGRATION_CONFIG','CAFE_AUTHORITY_FIXTURE','TIKTOKEN_CACHE_DIR','CAFE_PRIVATE_POLICY']:self.assertNotIn(key,env)
  self.assertEqual(json.loads(env['CAFE_PREPARED_INVENTORY_JSON']),self.names);self.assertEqual(env['CARGO_HOME'],'approved-cache')
 def test_content_change_changes_tree_digest(self):
  old=ci.tree_digest(self.root,self.names);(self.root/'README.md').write_text('Changed\n');self.assertNotEqual(ci.tree_digest(self.root,self.names),old)


if __name__=='__main__':unittest.main()
