"""Portable structural oracles; native PowerShell/analyzer remains a separate gate."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('build_rc_contract',ROOT/'tools/windows/build_rc_contract.py')
B=importlib.util.module_from_spec(spec);spec.loader.exec_module(B)


def manifest():
    return {'schema':'cafe.windows.rc-manifest.v1','version':'0.1.0-rc.1','source_commit':'a'*40,'target':'x86_64-pc-windows-msvc','windows_verification':'pending','files':[{'path':'authority/cafe-core.exe','bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()},{'path':'cafe-0.1.0-rc.1.zip','bytes':4,'sha256':hashlib.sha256(b'fake').hexdigest()}],'archive_files':[{'path':'Cafe.exe','bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}


class TestWindowsBuildRC(unittest.TestCase):
    def test_manifest_closed_shape_and_relative_coordinates(self):
        self.assertEqual(B.validate_manifest_shape(manifest()),manifest())
        for group in ['files','archive_files']:
            for path in ['../escape','/absolute','C:'+chr(92)+'file','a//b','a/./b',chr(92)*2+'host'+chr(92)+'file']:
                bad=manifest();bad[group][0]['path']=path
                with self.subTest(group=group,path=path),self.assertRaises(ValueError):B.validate_manifest_shape(bad)

    def test_unknown_key_wrong_versions_and_volatile_metadata_reject(self):
        for key,value in [('extra',True),('schema','other'),('version','0.1.0'),('target','arm64'),('windows_verification','pass'),('source_commit','short'),('timestamp','synthetic')]:
            bad=manifest();bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):B.validate_manifest_shape(bad)

    def test_duplicates_case_collision_and_scalar_types_reject(self):
        for update in ['duplicate','boolean_size','float_size','bad_hash','unsorted']:
            bad=manifest()
            if update=='duplicate':bad['archive_files'].append(dict(bad['archive_files'][0],path='cafe.EXE'))
            elif update=='boolean_size':bad['files'][0]['bytes']=True
            elif update=='float_size':bad['files'][0]['bytes']=3.0
            elif update=='bad_hash':bad['files'][0]['sha256']='z'*64
            else:bad['files'].reverse()
            with self.subTest(update=update),self.assertRaises(ValueError):B.validate_manifest_shape(bad)

    def test_missing_artifact_rejects_manifest(self):
        bad=manifest();bad['files'].pop()
        with self.assertRaises(ValueError):B.validate_manifest_shape(bad)

    def test_zip_hash_is_over_actual_bytes_and_tampering_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fixture.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('Cafe.exe',b'abc')
            self.assertEqual(B.inspect_zip(p),[{'path':'Cafe.exe','bytes':3,'sha256':'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}])
            with zipfile.ZipFile(p,'w') as z:z.writestr('Cafe.exe',b'abd')
            self.assertNotEqual(B.inspect_zip(p)[0]['sha256'],'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')

    def test_zip_escape_and_case_collisions_reject(self):
        for names in [['../escape'],['Cafe.exe','cafe.EXE']]:
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'fixture.zip'
                with zipfile.ZipFile(p,'w') as z:
                    for name in names:z.writestr(name,b'abc')
                with self.assertRaises(ValueError):B.inspect_zip(p)

    def test_missing_toolchain_blocks_before_any_command_or_effect(self):
        calls=[]
        def runner(*args,**kwargs):calls.append(args);raise AssertionError('command must not run')
        with self.assertRaises(B.Blocked) as error:B.preflight(ROOT,'a'*40,'missing','missing',runner=runner,which=lambda name:None,platform='win32')
        self.assertEqual(error.exception.code,'TOOLCHAIN_MISSING')
        self.assertEqual(error.exception.command,'Get-Command git -ErrorAction Stop')
        self.assertEqual(calls,[])

    def test_private_runtime_probe_cannot_write_bytecode(self):
        class ProbeReached(RuntimeError):
            pass
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp).resolve()
            archive=source/'private-runtime.tar.gz';archive.write_bytes(b'fixture')
            executable=str(Path(sys.executable).resolve())
            expected={
                ('git','rev-parse','--show-toplevel'):str(source),
                ('git','rev-parse','HEAD'):'a'*40,
                ('git','status','--porcelain','--untracked-files=normal'):'',
                ('rustup','run','1.97.0','rustc','--version'):'rustc 1.97.0 fixture',
                ('rustup','run','1.97.0','cargo','--version'):'cargo 1.97.0 fixture',
                ('rustup','target','list','--installed','--toolchain','1.97.0'):B.TARGET,
                ('node','--version'):'v24.14.0',
                ('link.exe','/?'):'Microsoft Linker',
            }
            def runner(argv,**kwargs):
                if argv[0]==executable:
                    # Exercise the real interpreter with the actual flags chosen by preflight.
                    actual=subprocess.run(argv[:-1]+['import sys;print(int(sys.dont_write_bytecode))'],
                        capture_output=True,text=True,timeout=30)
                    self.assertEqual(actual.returncode,0,actual.stderr)
                    self.assertEqual(actual.stdout.strip(),'1')
                    raise ProbeReached()
                self.assertIn(tuple(argv),expected)
                return subprocess.CompletedProcess(argv,0,expected[tuple(argv)],'')
            with patch.object(B,'PYTHON_BYTES',7),patch.object(B,'PYTHON_SHA',hashlib.sha256(b'fixture').hexdigest()):
                with self.assertRaises(ProbeReached):
                    B.preflight(source,'a'*40,executable,archive,runner=runner,
                        which=lambda name:str(source/name),platform='win32')

    def test_preflight_cli_missing_prerequisite_is_nonzero_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv=[sys.executable,'-B',str(ROOT/'tools/windows/build_rc_contract.py'),'--preflight-only','--source',tmp,'--commit','a'*40,'--python','missing','--python-archive','missing','--websockets-wheel','missing','--electron-cache','missing','--output',str(Path(tmp)/'absent')]
            p=subprocess.run(argv,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(p.returncode,0)
            result=json.loads(p.stdout);self.assertEqual(result['status'],'blocked');self.assertTrue(result['required_command'])
            self.assertFalse((Path(tmp)/'absent').exists())

    def test_powershell_structure_is_not_an_analyzer_substitute(self):
        text=(ROOT/'tools/windows/build_rc.ps1').read_text()
        for required in ['Get-Module -ListAvailable -Name PSScriptAnalyzer','Invoke-ScriptAnalyzer','Test-PrivateRuntime','Get-FileHash','build_rc_contract.py','exit $LASTEXITCODE','Write-Blocked']:
            self.assertIn(required,text)
        for forbidden in ['Invoke-Expression','Invoke-WebRequest','Install-Module','Start-BitsTransfer']:
            self.assertNotIn(forbidden,text)
        self.assertLess(text.index('Test-PrivateRuntime -Executable'),text.index('& $PythonExe'))

    def test_approved_python_pin_set_is_closed_and_english_relative(self):
        pins=json.loads((ROOT/'tools/windows/python_runtime_pins.json').read_text())
        self.assertEqual(pins['version'],'3.13.16+20261013')
        self.assertEqual(pins['archive_bytes'],21987516)
        self.assertEqual(pins['archive_sha256'],'7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06')
        self.assertGreater(len(pins['files']),3)
        for name,row in pins['files'].items():
            self.assertTrue(name.startswith('python/'));B.relative(name)
            self.assertEqual(set(row),{'bytes','sha256'});self.assertRegex(row['sha256'],r'^[a-f0-9]{64}$')

    def test_artifact_verifier_rejects_well_formed_hash_forgery_and_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'authority').mkdir();(root/'authority/cafe-core.exe').write_bytes(b'abc')
            zip_path=root/'cafe-0.1.0-rc.1.zip'
            with zipfile.ZipFile(zip_path,'w') as z:z.writestr('Cafe.exe',b'abc')
            value=manifest();value['files'][1]={'path':'cafe-0.1.0-rc.1.zip','bytes':zip_path.stat().st_size,'sha256':hashlib.sha256(zip_path.read_bytes()).hexdigest()}
            self.assertEqual(B.validate_manifest(value,root),value)
            for group in ('files','archive_files'):
                forged=copy.deepcopy(value);forged[group][0]['sha256']='0'*64
                with self.subTest(group=group),self.assertRaises(ValueError):B.validate_manifest(forged,root)
            (root/'authority/cafe-core.exe').write_bytes(b'abd')
            with self.assertRaises(ValueError):B.validate_manifest(value,root)
            (root/'authority/cafe-core.exe').write_bytes(b'abc')
            with zipfile.ZipFile(zip_path,'w') as z:z.writestr('Cafe.exe',b'abd')
            # Even a recalculated outer ZIP digest cannot hide a stale inner entry.
            value['files'][1]['bytes']=zip_path.stat().st_size;value['files'][1]['sha256']=hashlib.sha256(zip_path.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):B.validate_manifest(value,root)

    def test_packaging_includes_the_actual_runtime_factory(self):
        text=(ROOT/'tools/build_windows_app.js').read_text()
        self.assertIn("'src/main/runtimeBackend.js'",text)
        self.assertIn('inspect.status',text)
        self.assertIn('prune:false',text)


if __name__=='__main__':unittest.main()
