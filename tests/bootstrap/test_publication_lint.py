"""Independent public and hashed-policy rejection oracles; no private literal fixtures."""
import hashlib
import importlib.util
import json
import os
from unittest import mock
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('publication_lint',ROOT/'tools/publication_lint.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


def policy(value):
    tokens=P.words(value)
    return [{'category':'private_user','words':len(tokens),'first_sha256':P.digest(tokens[0]),'phrase_sha256':P.digest(' '.join(tokens))}]


class TestPublicationLint(unittest.TestCase):
    def test_home_posix_windows_and_unc_are_rejected(self):
        for value in ['/'+ 'home' +'/someone/file', '/'+ 'Users' +'/someone/file', 'C:'+chr(92)+'Users'+chr(92)+'someone',chr(92)*2+'host'+chr(92)+'share']:
            with self.subTest(kind='constructed home or UNC'):
                self.assertTrue(any(f['rule']=='absolute_home_or_unc_path' for f in P.scan_text('privacy.txt',value)))

    def test_declared_identity_is_exact_and_foreign_email_rejects(self):
        self.assertEqual(P.scan_text('privacy.txt',P.DECLARED_IDENTITY),[])
        self.assertEqual(P.scan_text('privacy.txt',"'"+P.DECLARED_IDENTITY+"'"),[])
        for value in ['other'+'@'+'fixture.invalid','a'+P.DECLARED_IDENTITY,P.DECLARED_IDENTITY+'Z']:
            self.assertTrue(any(f['rule']=='undeclared_email' for f in P.scan_text('privacy.txt',value)))

    def test_private_key_and_token_are_rejected_without_echo(self):
        for value in ['-----BEGIN '+'PRIVATE KEY-----','gh'+'p_'+'A'*36,'secret_'+'key='+ 'a1'*32]:
            findings=P.scan_text('privacy.txt',value)
            self.assertTrue(any(f['rule']=='credential' for f in findings))
            self.assertNotIn(value,json.dumps(findings))

    def test_epoch_attached_to_letter_is_rejected(self):
        for value in ['1700000000'+'Z','1700000000000'+'ms']:
            self.assertTrue(any(f['rule']=='epoch_with_letter_suffix' for f in P.scan_text('privacy.txt',value)))
        self.assertEqual(P.scan_text('privacy.txt','sha256 '+ 'a1'*32),[])

    def test_host_user_labels_and_non_english_names_reject(self):
        for text in ['host'+'name=privateNode','user'+'name: privatePerson']:
            self.assertTrue(any(f['rule']=='user_or_host_identifier' for f in P.scan_text('privacy.txt',text)))
        self.assertEqual(P.scan_text('privacy.txt','user:read:chat'),[])
        for name in ['archivo.py','\u00e9trange.txt']:
            self.assertTrue(any(f['rule']=='non_english_or_unreviewed_filename' for f in P.scan_text(name,'safe')))

    def test_hash_policy_matches_normalized_tokens_not_substrings(self):
        canary='private identity fixture'
        rules=policy(canary)
        self.assertTrue(P.private_hit('Private identity fixture',rules))
        self.assertFalse(P.private_hit('private identity fixtureSuffix',rules))
        self.assertNotIn(canary,json.dumps(rules))
        self.assertTrue(any(f['rule']=='private_identifier' for f in P.scan_text('privacy.txt',canary,rules)))

    def test_whole_line_allowlist_never_hides_appended_token(self):
        with tempfile.TemporaryDirectory(prefix='cafe-publication-') as temp:
            p=Path(temp)/'policy.json';p.write_text(json.dumps({'schema':'cafe.publication-allowlist.v1','lines':[{'pattern':'^Safe identifier$','reason':'reviewed identifier fixture'}]}));allowed=P.load_allowlist(p)
            self.assertEqual(P.scan_text('privacy.txt','Safe identifier',allowlist=allowed),[])
            value='Safe identifier '+'gh'+'p_'+'B'*36
            self.assertTrue(any(f['rule']=='credential' for f in P.scan_text('privacy.txt',value,allowlist=allowed)))
            for pattern in ['Safe identifier','^.*$','^prefix.+$']:
                p.write_text(json.dumps({'schema':'cafe.publication-allowlist.v1','lines':[{'pattern':pattern,'reason':'invalid'}]}))
                with self.assertRaises(ValueError):P.load_allowlist(p)

    def test_cli_quoted_json_keys_and_windows_case_variants_reject(self):
        cases=[]
        for key in ['host'+'name','user'+'name','host','user']:
            cases.append((json.dumps({key:'FixtureIdentity'}),'user_or_host_identifier'))
        key='host'+'name'
        cases.append(('{\n"'+key+'"\n:\n"FixtureIdentity"\n}','user_or_host_identifier'))
        for drive,component,separator in [('c','users',chr(92)),('C','uSeRs',chr(92)),('z','USERS','/')]:
            value=drive+':'+separator+component+separator+'someone'
            cases.append((value,'absolute_home_or_unc_path'))
            cases.append((json.dumps({'path':value}),'absolute_home_or_unc_path'))
        for text,rule in cases:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tools/publication_lint.py'),'--stdin','--name','privacy.txt'],input=text,text=True,capture_output=True)
            self.assertEqual(result.returncode,1,result.stdout+result.stderr)
            self.assertTrue(any(f['rule']==rule for f in json.loads(result.stdout)['findings']))

    def test_missing_or_malformed_policy_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix='cafe-publication-') as temp:
            p=Path(temp)/'policy.json'
            with self.assertRaises(OSError):P.load_policy(p)
            p.write_text('{}')
            with self.assertRaises(ValueError):P.load_policy(p)

    def test_vendor_generic_gate_remains_effective_without_policy(self):
        import sys
        sys.path.insert(0,str(ROOT/'router/tools'))
        import gates_v2 as gate
        with tempfile.TemporaryDirectory(prefix='cafe-publication-') as temp:
            p=Path(temp)/'privacy.txt';p.write_text('/'+'home'+'/someone/file')
            with mock.patch.dict(os.environ):
                os.environ.pop("ROUTER_PRIVATE_POLICY",None)
                result=gate.g_privacy([str(p)])
            self.assertFalse(result['pass'])
            self.assertNotIn('someone',json.dumps(result))


if __name__=='__main__':unittest.main()
