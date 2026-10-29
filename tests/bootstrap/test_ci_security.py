"""Every selected JavaScript security file must execute nonempty, unskipped tests."""
from pathlib import Path
import sys
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools'))
import run_ci as ci
FILES=['tests/security/backendSupervisor.test.js','tests/security/electronShell.test.js','tests/security/shellFuses.test.js']
GOOD='# tests 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n'

class SecurityStageTests(unittest.TestCase):
 def stage(self,names=FILES,output=GOOD):
  self.calls=[]
  def command(argv,root,env,timeout=900):
   self.assertEqual(timeout,900)
   self.calls.append(argv)
   return {'rc':0,'stdout':'v24.14.0\n' if '--version' in argv else output,'stderr':''}
  with mock.patch.object(ci,'prepared_inventory',return_value=names),mock.patch.object(ci,'executable',side_effect=lambda name:name),mock.patch.object(ci,'command',side_effect=command):
   return ci.run_stage('node')
 def test_all_security_files_are_run_individually_after_preload_build(self):
  extra='tests/security/future.test.js';result=self.stage(FILES+[extra]);self.assertEqual(result['status'],'pass')
  selected=[call[-1] for call in self.calls if '--test' in call]
  self.assertEqual(selected,sorted(FILES+[extra]));self.assertEqual(self.calls[1],['npm','run','build:preload'])
  self.assertTrue(all(row['test_counts']['tests']>0 for row in result['records'] if 'test_counts' in row))
 def test_empty_or_omitted_required_selection_fails(self):
  for names in [[],FILES[:-1]]:
   with self.subTest(names=names):self.assertEqual(self.stage(names)['status'],'fail')
 def test_empty_skipped_failed_or_cancelled_output_fails(self):
  for text in [GOOD.replace('tests 1','tests 0'),GOOD.replace('pass 1','pass 0'),GOOD.replace('skipped 0','skipped 1'),GOOD.replace('fail 0','fail 1'),GOOD.replace('cancelled 0','cancelled 1'),'# tests 1\n',GOOD+GOOD]:
   with self.subTest(output=text):self.assertEqual(self.stage(output=text)['status'],'fail')
 def test_actual_node_reporter_forms_are_both_accepted(self):
  self.assertEqual(ci.node_test_evidence(GOOD)['pass'],1)
  self.assertEqual(ci.node_test_evidence(GOOD.replace('#','ℹ'))['pass'],1)
