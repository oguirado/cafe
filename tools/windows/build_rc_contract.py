"""Offline Windows RC build contract. Missing prerequisites are never success."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import zipfile

TARGET = 'x86_64-pc-windows-msvc'
PYTHON_SHA = '7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06'
PYTHON_BYTES = 21987516


class Blocked(ValueError):
    def __init__(self, code, command):
        self.code, self.command = code, command
        super().__init__(code)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def local_path(value, *, existing=True):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or str(p).startswith(chr(92) * 2):
        raise Blocked('LOCAL_PATH_REQUIRED', 'Use an absolute local path without links')
    target = p if existing else p.parent
    if target.resolve(strict=True) != target:
        raise Blocked('CANONICAL_PATH_REQUIRED', 'Use the canonical local path')
    for part in (target, *target.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise Blocked('LINK_PATH_REJECTED', 'Use a local directory without reparse points')
    return p


def relative(value):
    if (type(value) is not str or not value or chr(92) in value or ':' in value or
            chr(0) in value or str(PurePosixPath(value)) != value or
            any(x in {'', '.', '..'} for x in value.split('/')) or value.startswith('/')):
        raise ValueError('manifest_path_rejected')
    return value


def invoke(argv, *, cwd, runner=subprocess.run, timeout=30):
    try:
        p = runner(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                   check=False, env=dict(os.environ, CARGO_NET_OFFLINE='true', npm_config_offline='true', npm_config_audit='false', npm_config_update_notifier='false'))
    except (OSError, subprocess.SubprocessError):
        raise Blocked('COMMAND_UNAVAILABLE', ' '.join(Path(v).name if Path(v).is_absolute() else v for v in argv[:3])) from None
    if p.returncode:
        raise Blocked('COMMAND_FAILED', ' '.join(Path(v).name if Path(v).is_absolute() else v for v in argv[:3]))
    return p.stdout


def preflight(source, commit, python, archive, *, runner=subprocess.run, which=shutil.which, platform=sys.platform):
    if platform != 'win32':
        raise Blocked('WINDOWS_REQUIRED', 'Run tools/windows/build_rc.ps1 on Windows x64')
    for tool in ('git', 'rustup', 'node', 'npm.cmd', 'link.exe'):
        if which(tool) is None:
            raise Blocked('TOOLCHAIN_MISSING', 'Get-Command ' + tool + ' -ErrorAction Stop')
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise Blocked('REVIEWED_COMMIT_REQUIRED', 'git rev-parse HEAD')
    source = local_path(source)
    def run(argv):
        return invoke(argv, cwd=source, runner=runner)
    if Path(run(['git', 'rev-parse', '--show-toplevel']).strip()).resolve() != source:
        raise Blocked('CHECKOUT_ROOT_REQUIRED', 'git rev-parse --show-toplevel')
    if run(['git', 'rev-parse', 'HEAD']).strip() != commit:
        raise Blocked('REVIEWED_COMMIT_MISMATCH', 'git rev-parse HEAD')
    if run(['git', 'status', '--porcelain', '--untracked-files=normal']).strip():
        raise Blocked('CLEAN_CHECKOUT_REQUIRED', 'git status --porcelain --untracked-files=normal')
    for tool in ('rustc', 'cargo'):
        out = run(['rustup', 'run', '1.97.0', tool, '--version'])
        if not re.match(re.escape(tool) + r' 1\.97\.0(?:\s|$)', out):
            raise Blocked('RUST_PIN_REQUIRED', 'rustup run 1.97.0 ' + tool + ' --version')
    if TARGET not in run(['rustup', 'target', 'list', '--installed', '--toolchain', '1.97.0']).split():
        raise Blocked('MSVC_TARGET_MISSING', 'rustup target list --installed --toolchain 1.97.0')
    if not re.fullmatch(r'v24\.[0-9]+\.[0-9]+\s*', run(['node', '--version'])):
        raise Blocked('NODE_24_REQUIRED', 'node --version')
    # Reject a GNU link executable accidentally shadowing the MSVC linker.
    try:
        p = runner(['link.exe', '/?'], cwd=source, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise Blocked('MSVC_LINKER_REQUIRED', 'link.exe /?') from None
    if 'Microsoft' not in p.stdout + p.stderr or 'Linker' not in p.stdout + p.stderr:
        raise Blocked('MSVC_LINKER_REQUIRED', 'link.exe /?')
    archive = local_path(archive)
    if archive.stat().st_size != PYTHON_BYTES or digest(archive) != PYTHON_SHA:
        raise Blocked('APPROVED_PYTHON_ARCHIVE_REQUIRED', 'Get-FileHash -Algorithm SHA256 $PythonArchive')
    python = local_path(python)
    if run([str(python), '-I', '-c', 'import sys;print(".".join(map(str,sys.version_info[:3])))']).strip() != '3.13.16':
        raise Blocked('APPROVED_PYTHON_REQUIRED', '$PythonExe -I --version')
    for name in ('tools/build_windows_app.js', 'tools/package_windows.py', 'tools/check_shell_archive.py', 'package-lock.json', 'Cargo.lock'):
        local_path(source / name)
    if not (source / 'node_modules').is_dir():
        raise Blocked('OFFLINE_NODE_CACHE_REQUIRED', 'Test-Path node_modules')
    inventory = run(['git', 'ls-files', '--cached', '-z']).split(chr(0))
    inventory = sorted(set(inventory) - {''})
    if not inventory or len(inventory) > 4096:
        raise Blocked('SOURCE_INVENTORY_REQUIRED', 'git ls-files --cached')
    for name in inventory:
        relative(name)
    return inventory


def inspect_zip(path):
    rows, seen, total = [], set(), 0
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > 20000:
            raise ValueError('archive_count_rejected')
        for entry in entries:
            name = relative(entry.filename.rstrip('/'))
            key = name.casefold()
            if key in seen or entry.flag_bits & 1:
                raise ValueError('archive_duplicate_or_encrypted')
            seen.add(key)
            if entry.is_dir():
                continue
            if stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK:
                raise ValueError('archive_link_rejected')
            total += entry.file_size
            if total > 2 * 1024 ** 3 or entry.file_size > 512 * 1024 ** 2:
                raise ValueError('archive_size_rejected')
            h = hashlib.sha256(); size = 0
            with archive.open(entry) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    size += len(block); h.update(block)
                    if size > entry.file_size:
                        raise ValueError('archive_size_mismatch')
            if size != entry.file_size:
                raise ValueError('archive_size_mismatch')
            rows.append({'path':name, 'bytes':size, 'sha256':h.hexdigest()})
    return sorted(rows, key=lambda r:r['path'])


def validate_manifest_shape(value):
    if (type(value) is not dict or set(value) != {'schema','version','source_commit','target','windows_verification','files','archive_files'} or
            value['schema'] != 'cafe.windows.rc-manifest.v1' or value['version'] != '0.1.0-rc.1' or value['target'] != TARGET or
            value['windows_verification'] != 'pending' or type(value['source_commit']) is not str or not re.fullmatch('[a-f0-9]{40}', value['source_commit'])):
        raise ValueError('manifest_shape_rejected')
    for group in ('files', 'archive_files'):
        rows=value[group]
        if type(rows) is not list or not rows or len(rows)>20000:
            raise ValueError('manifest_count_rejected')
        seen=set()
        for row in rows:
            if type(row) is not dict or set(row)!={'path','bytes','sha256'}:
                raise ValueError('manifest_record_rejected')
            name=relative(row['path'])
            if name.casefold() in seen or type(row['bytes']) is not int or row['bytes']<0 or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',row['sha256']):
                raise ValueError('manifest_record_rejected')
            seen.add(name.casefold())
        if rows != sorted(rows,key=lambda r:r['path']):
            raise ValueError('manifest_order_rejected')
    if {r['path'] for r in value['files']} != {'authority/cafe-core.exe','cafe-0.1.0-rc.1.zip'}:
        raise ValueError('manifest_artifacts_rejected')
    return value


def validate_manifest(value, artifact_root):
    """Verify a typed manifest against the actual files and every ZIP entry."""
    validate_manifest_shape(value)
    root=local_path(artifact_root)
    for row in value['files']:
        path=local_path(root/row['path'])
        if not path.is_file() or path.stat().st_size!=row['bytes'] or digest(path)!=row['sha256']:
            raise ValueError('manifest_artifact_mismatch')
    if inspect_zip(root/'cafe-0.1.0-rc.1.zip')!=value['archive_files']:
        raise ValueError('manifest_archive_mismatch')
    return value


def build(args):
    source=Path(args.source);output=local_path(args.output,existing=False)
    if output.exists() or source.resolve() in output.parents or output==source:
        raise Blocked('NEW_EXTERNAL_OUTPUT_REQUIRED','Use a new output directory outside the checkout')
    inventory=preflight(source,args.commit,args.python,args.python_archive)
    electron=local_path(args.electron_cache);wheel=local_path(args.websockets_wheel)
    before={p:digest(source/p) for p in ('Cargo.lock','package-lock.json')}
    output.mkdir();target=output/'target';staging=output/'authority';staging.mkdir()
    try:
        invoke(['rustup','run','1.97.0','cargo','build','--release','--offline','--locked','--bin','cafe-core','--target',TARGET,'--target-dir',str(target)],cwd=source,timeout=900)
        exe=target/TARGET/'release/cafe-core.exe'
        if not exe.is_file():
            raise Blocked('AUTHORITY_BINARY_MISSING','cargo build --release --offline --locked --bin cafe-core --target '+TARGET)
        shutil.copyfile(exe,staging/'cafe-core.exe');exe=staging/'cafe-core.exe'
        for script in ('build:preload','build:renderer','check'):
            invoke(['node',str(source/'node_modules/npm/bin/npm-cli.js'),'run',script],cwd=source,timeout=900) if (source/'node_modules/npm/bin/npm-cli.js').is_file() else invoke(['npm.cmd','run',script],cwd=source,timeout=900)
        zip_path=output/'cafe-0.1.0-rc.1.zip'
        request={'source':str(source),'output':str(zip_path),'electronZipDir':str(electron),'pythonTool':str(Path(args.python)),'pythonArchive':str(Path(args.python_archive)),'websocketsWheel':str(wheel),'authorityExe':str(exe),'authoritySha256':digest(exe),'inventory':inventory}
        request_path=output/'package-request.json';request_path.write_text(json.dumps(request),encoding='utf-8')
        try:
            result=json.loads(invoke(['node','tools/build_windows_app.js',str(request_path)],cwd=source,timeout=900))
        finally:
            request_path.unlink(missing_ok=True)
        if result.get('status') != 'built_unsigned_native_pending':
            raise Blocked('PACKAGE_NOT_VERIFIED','node tools/build_windows_app.js $RequestFile')
        invoke([args.python,'-B',str(source/'tools/check_shell_archive.py'),str(zip_path),'--exe','Cafe.exe','--node',shutil.which('node')],cwd=source,timeout=900)
        rows=[{'path':str(p.relative_to(output)).replace(chr(92),'/'),'bytes':p.stat().st_size,'sha256':digest(p)} for p in [exe,zip_path]]
        value={'schema':'cafe.windows.rc-manifest.v1','version':'0.1.0-rc.1','source_commit':args.commit,'target':TARGET,'windows_verification':'pending','files':sorted(rows,key=lambda r:r['path']),'archive_files':inspect_zip(zip_path)}
        validate_manifest(value,output)
        if any(digest(source/name)!=sha for name,sha in before.items()):
            raise Blocked('LOCKFILE_CHANGED','git diff --exit-code -- Cargo.lock package-lock.json')
        if invoke(['git','rev-parse','HEAD'],cwd=source).strip()!=args.commit or invoke(['git','status','--porcelain','--untracked-files=normal'],cwd=source).strip():
            raise Blocked('CHECKOUT_CHANGED_DURING_BUILD','git status --porcelain --untracked-files=normal')
        (output/'manifest.json').write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
        return {'schema':'cafe.windows.rc-build.v1','status':'built_unsigned_native_pending','manifest':'manifest.json','version':'0.1.0-rc.1'}
    finally:
        # Only the new directory created by this invocation is a build scratch.
        shutil.rmtree(target,ignore_errors=False) if target.exists() else None


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','commit','python','python-archive','websockets-wheel','electron-cache','output'):
        p.add_argument('--'+name)
    p.add_argument('--preflight-only',action='store_true');p.add_argument('--verify-manifest');a=p.parse_args(argv)
    try:
        if a.verify_manifest:
            path=local_path(a.verify_manifest)
            if path.stat().st_size>8*1024*1024:raise ValueError('manifest_size_rejected')
            def closed(pairs):
                value={}
                for name,item in pairs:
                    if name in value:raise ValueError('duplicate_manifest_key')
                    value[name]=item
                return value
            validate_manifest(json.loads(path.read_bytes(),object_pairs_hook=closed),path.parent)
            result={'schema':'cafe.windows.rc-build.v1','status':'manifest_verified'}
        elif not all(getattr(a,name) for name in ['source','commit','python','python_archive','websockets_wheel','electron_cache','output']):
            raise Blocked('BUILD_ARGUMENT_REQUIRED','Run tools/windows/build_rc.ps1 with every documented parameter')
        elif a.preflight_only:
            preflight(Path(a.source),a.commit,a.python,a.python_archive);result={'schema':'cafe.windows.rc-build.v1','status':'preflight_pass'}
        else:
            result=build(a)
    except Blocked as error:
        result={'schema':'cafe.windows.rc-build.v1','status':'blocked','code':error.code,'required_command':error.command}
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
        result={'schema':'cafe.windows.rc-build.v1','status':'blocked','code':'BUILD_REJECTED','required_command':'Run the documented preflight and inspect its fixed failure code'}
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status'] in {'preflight_pass','built_unsigned_native_pending','manifest_verified'} else 2


if __name__=='__main__':
    raise SystemExit(main())
