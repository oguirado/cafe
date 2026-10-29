"""Manual package-path and pin oracles; no download or native execution."""
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('cafe_windows_package',ROOT/'tools/package_windows.py')
package=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(package)


def pe():
    data=bytearray(128);data[:2]=b'MZ';struct.pack_into('<I',data,60,64)
    data[64:68]=b'PE\0\0';struct.pack_into('<H',data,68,0x8664);struct.pack_into('<H',data,88,0x20b)
    return bytes(data)


class WindowsPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.archive=self.root/'runtime.tar.gz';self.out=self.root/'prepared'
    def fixture(self,extra=()):
        entries=[('python/python.exe',pe()),('python/python3.dll',pe()),('python/python313.dll',pe()),*extra]
        with tarfile.open(self.archive,'w:gz') as tar:
            for name,raw in entries:
                item=tarfile.TarInfo(name);item.size=len(raw);tar.addfile(item,io.BytesIO(raw))
        return mock.patch.multiple(package,PYTHON_BYTES=self.archive.stat().st_size,PYTHON_SHA256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
    def test_operator_exact_pins_are_not_a_new_runtime_selection(self):
        self.assertEqual(package.PYTHON_BYTES,21987516)
        self.assertEqual(package.PYTHON_SHA256,'7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06')
        self.assertEqual(package.WEBSOCKETS_SHA256,'6aa59f0ef92e796b2db6f5f26550c4713c0e4036899fadf02f55e2ed4db0b7ae')
    def test_pin_failure_happens_before_any_extraction(self):
        self.fixture()
        with self.assertRaisesRegex(package.PackageBlocked,'PIN_MISMATCH'):package.extract_verified_python(self.archive,self.out)
        self.assertFalse(self.out.exists())
    def test_synthetic_verified_runtime_reports_pending_not_native_pass(self):
        with self.fixture():result=package.extract_verified_python(self.archive,self.out)
        self.assertEqual(result['native_execution'],'pending');self.assertEqual(len(result['files']),3)
        self.assertEqual((self.out/'python/python.exe').read_bytes(),pe())
        self.assertEqual(result['files']['python/python.exe']['sha256'],hashlib.sha256(pe()).hexdigest())
    def test_windows_path_inventory_rejects_all_escape_forms(self):
        values=['../escape','/absolute','python/../escape','python//file','python/CON.txt','python/NUL','python/trailing.','python/trailing ','python/'+chr(92)+'escape','python/a:b','python/'+chr(0)+'bad']
        for value in values:
            with self.subTest(value=repr(value)),self.assertRaises(package.PackageBlocked):package.safe_member(value)
    def test_casefold_alias_is_rejected_before_destination_creation(self):
        with self.fixture([('python/PYTHON.EXE',pe())]):
            with self.assertRaisesRegex(package.PackageBlocked,'ALIAS'):package.extract_verified_python(self.archive,self.out)
        self.assertFalse(self.out.exists())
    def test_tar_link_does_not_reach_the_filesystem(self):
        self.fixture()
        with tarfile.open(self.archive,'w:gz') as tar:
            item=tarfile.TarInfo('python/python.exe');item.type=tarfile.SYMTYPE;item.linkname='../outside';tar.addfile(item)
        with mock.patch.multiple(package,PYTHON_BYTES=self.archive.stat().st_size,PYTHON_SHA256=hashlib.sha256(self.archive.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(package.PackageBlocked,'ENTRY'):package.extract_verified_python(self.archive,self.out)
        self.assertFalse(self.out.exists())
    def test_invalid_pe_cleans_owned_destination(self):
        with self.fixture([('python/readme.txt',b'not native proof')]):
            with mock.patch.object(package,'windows_x64_image',side_effect=package.PackageBlocked('WINDOWS_X64_PE_REQUIRED')):
                with self.assertRaises(package.PackageBlocked):package.extract_verified_python(self.archive,self.out)
        self.assertFalse(self.out.exists())
    def test_expanded_budget_and_file_count_fail_closed(self):
        for limits in [{'MAX_FILES':2},{'MAX_EXPANDED':100}]:
            with self.fixture(),mock.patch.multiple(package,**limits):
                with self.assertRaisesRegex(package.PackageBlocked,'BUDGET'):package.extract_verified_python(self.archive,self.out)
            self.assertFalse(self.out.exists())
    def test_authority_hash_is_required_and_never_proves_runtime(self):
        source=self.root/'cafe-core.exe';source.write_bytes(pe());digest=hashlib.sha256(pe()).hexdigest()
        self.assertEqual(package.authority_input(source,digest)['native_execution'],'pending')
        with self.assertRaisesRegex(package.PackageBlocked,'PIN'):package.authority_input(source,'0'*64)
    def test_archive_is_complete_or_absent_and_does_not_bundle_ffmpeg(self):
        stage=self.root/'stage';stage.mkdir();(stage/'app.txt').write_text('bounded fixture')
        destination=self.root/'app.zip';result=package.write_archive(stage,destination)
        self.assertEqual(result['files'],1);self.assertEqual(result['native_execution'],'pending')
        before=destination.read_bytes()
        with self.assertRaises(package.PackageBlocked):package.write_archive(stage,destination)
        self.assertEqual(destination.read_bytes(),before)
        (stage/'ffmpeg.exe').write_bytes(pe())
        with self.assertRaisesRegex(package.PackageBlocked,'NOT_BUNDLED'):package.write_archive(stage,self.root/'bad.zip')
        self.assertFalse((self.root/'bad.zip').exists())
    def test_flush_failure_never_publishes_an_archive(self):
        stage=self.root/'stage';stage.mkdir();(stage/'app.txt').write_text('bounded fixture')
        destination=self.root/'failed.zip'
        with mock.patch.object(package.os,'fsync',side_effect=OSError('simulated flush failure')):
            with self.assertRaises(OSError):package.write_archive(stage,destination)
        self.assertFalse(destination.exists());self.assertFalse(list(self.root.glob('.cafe-package-*')))

    def test_runtime_alias_rejected_before_any_wheel_write(self):
        wheel=self.root/'wheel.zip'
        with zipfile.ZipFile(wheel,'w') as archive:archive.writestr('websockets/__init__.py','synthetic')
        self.out.mkdir();before=list(self.out.iterdir());original=Path.is_symlink
        with mock.patch.object(package,'WEBSOCKETS_SHA256',hashlib.sha256(wheel.read_bytes()).hexdigest()),mock.patch.object(Path,'is_symlink',autospec=True,side_effect=lambda path: path==self.out or original(path)):
            with self.assertRaisesRegex(package.PackageBlocked,'RUNTIME_PATH_REJECTED'):package.install_verified_websockets(wheel,self.out)
        self.assertEqual(list(self.out.iterdir()),before)
    def test_wheel_parent_alias_rejected_before_any_write(self):
        wheel=self.root/'wheel.zip'
        with zipfile.ZipFile(wheel,'w') as archive:archive.writestr('websockets/__init__.py','synthetic')
        site=self.out/'python/Lib/site-packages';site.mkdir(parents=True)
        original=Path.resolve
        def resolve(path,*args,**kwargs):
            if path==site/'websockets/__init__.py':return self.root/'outside/__init__.py'
            return original(path,*args,**kwargs)
        with mock.patch.object(package,'WEBSOCKETS_SHA256',hashlib.sha256(wheel.read_bytes()).hexdigest()),mock.patch.object(Path,'resolve',resolve):
            with self.assertRaisesRegex(package.PackageBlocked,'WHEEL_PATH_REJECTED'):package.install_verified_websockets(wheel,self.out)
        self.assertFalse((self.root/'outside').exists());self.assertFalse(list(site.iterdir()))


if __name__=='__main__':unittest.main()
