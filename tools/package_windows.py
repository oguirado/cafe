"""Prepare pinned private Python and explicit unsigned desktop build inputs.

No downloader, installer, FFmpeg bundle, native-success inference or shell.
The caller supplies an actual Windows authority executable and its digest.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import zipfile

PYTHON_BYTES = 21987516
PYTHON_SHA256 = '7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06'
WEBSOCKETS_SHA256 = '6aa59f0ef92e796b2db6f5f26550c4713c0e4036899fadf02f55e2ed4db0b7ae'
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 8192
RESERVED = {'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]}


class PackageBlocked(ValueError):
    """A fixed public error code; never include a selected host path."""


def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode()


def local_file(value):
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or path.resolve(strict=True)!=path or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():
        raise PackageBlocked('EXPLICIT_REGULAR_FILE_REQUIRED')
    return path


def digest_stream(stream):
    result=hashlib.sha256()
    for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def sha256_file(value):
    with local_file(value).open('rb') as stream:return digest_stream(stream)


def windows_x64_image(value):
    with local_file(value).open('rb') as stream:
        size=os.fstat(stream.fileno()).st_size;header=stream.read(64)
        if len(header)!=64 or header[:2]!=b'MZ':raise PackageBlocked('WINDOWS_X64_PE_REQUIRED')
        offset=struct.unpack_from('<I',header,60)[0]
        if offset<64 or offset+26>size:raise PackageBlocked('WINDOWS_X64_PE_REQUIRED')
        stream.seek(offset);pe=stream.read(26)
        if pe[:4]!=b'PE\0\0' or struct.unpack_from('<H',pe,4)[0]!=0x8664 or struct.unpack_from('<H',pe,24)[0]!=0x20b:
            raise PackageBlocked('WINDOWS_X64_PE_REQUIRED')


def safe_member(name):
    if not isinstance(name,str) or not name or len(name)>512 or not name.isascii() or chr(92) in name or ':' in name or any(ord(c)<32 or ord(c)==127 for c in name):
        raise PackageBlocked('ARCHIVE_PATH_REJECTED')
    name=name[:-1] if name.endswith('/') else name
    parts=name.split('/')
    if not name or name.startswith('/') or str(PurePosixPath(name))!=name:
        raise PackageBlocked('ARCHIVE_PATH_REJECTED')
    for part in parts:
        if part in {'','.','..'} or part.endswith((' ','.')) or part.split('.',1)[0].casefold() in RESERVED:
            raise PackageBlocked('ARCHIVE_PATH_REJECTED')
    return name


def new_directory(value):
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or path.exists() or path.is_symlink():raise PackageBlocked('NEW_DESTINATION_REQUIRED')
    parent=path.parent.resolve(strict=True)
    if parent!=path.parent or any(p.is_symlink() for p in path.parents):raise PackageBlocked('NEW_DESTINATION_REQUIRED')
    path.mkdir();return path


def extract_verified_python(archive,destination):
    """Hash and size before parsing; no special entries, aliasing or overwrites."""
    source=local_file(archive)
    with source.open('rb') as stream:
        if os.fstat(stream.fileno()).st_size!=PYTHON_BYTES or digest_stream(stream)!=PYTHON_SHA256:
            raise PackageBlocked('APPROVED_PYTHON_PIN_MISMATCH')
        stream.seek(0)
        with tarfile.open(fileobj=stream,mode='r:gz') as tar:
            entries=[];seen={};expanded=0
            for entry in tar:
                name=safe_member(entry.name)
                if name!='python' and not name.startswith('python/'):raise PackageBlocked('PYTHON_ROOT_REJECTED')
                folded=name.casefold()
                if folded in seen:raise PackageBlocked('ARCHIVE_ALIAS_REJECTED')
                seen[folded]=name
                if not (entry.isdir() or entry.isfile()) or entry.size<0:raise PackageBlocked('ARCHIVE_ENTRY_REJECTED')
                expanded+=entry.size;entries.append((name,entry))
                if len(entries)>MAX_FILES or expanded>MAX_EXPANDED:raise PackageBlocked('ARCHIVE_BUDGET_REJECTED')
            for name,_ in entries:
                for parent in PurePosixPath(name).parents:
                    folded=parent.as_posix().casefold()
                    if folded in seen and seen[folded]!=parent.as_posix():raise PackageBlocked('ARCHIVE_ALIAS_REJECTED')
            out=new_directory(destination);files={}
            try:
                for name,entry in entries:
                    target=out/name
                    if entry.isdir():target.mkdir(parents=True,exist_ok=True);continue
                    target.parent.mkdir(parents=True,exist_ok=True)
                    reader=tar.extractfile(entry)
                    if reader is None:raise PackageBlocked('ARCHIVE_ENTRY_REJECTED')
                    result=hashlib.sha256();remaining=entry.size
                    with reader,target.open('xb') as writer:
                        while remaining:
                            block=reader.read(min(1024*1024,remaining))
                            if not block:raise PackageBlocked('ARCHIVE_TRUNCATED')
                            writer.write(block);result.update(block);remaining-=len(block)
                        if reader.read(1):raise PackageBlocked('ARCHIVE_SIZE_REJECTED')
                    target.chmod(0o644);files[name]={'bytes':entry.size,'sha256':result.hexdigest()}
                windows_x64_image(out/'python/python.exe')
                for required in ['python/python3.dll','python/python313.dll']:
                    if required not in files:raise PackageBlocked('PYTHON_PRINCIPAL_FILE_ABSENT')
                    windows_x64_image(out/required)
                return {'schema':'cafe.private-python.v1','version':'3.13.16+20261013','archive_bytes':PYTHON_BYTES,'archive_sha256':PYTHON_SHA256,'files':files,'expanded_bytes':expanded,'native_execution':'pending'}
            except BaseException:
                shutil.rmtree(out);raise


def install_verified_websockets(wheel,runtime):
    """Copy the already approved universal wheel without pip or new dependencies."""
    source=local_file(wheel)
    if sha256_file(source)!=WEBSOCKETS_SHA256:raise PackageBlocked('APPROVED_WEBSOCKETS_PIN_MISMATCH')
    runtime=Path(runtime)
    if not runtime.is_absolute() or runtime.is_symlink() or runtime.resolve(strict=True)!=runtime:
        raise PackageBlocked('RUNTIME_PATH_REJECTED')
    destination=runtime/'python/Lib/site-packages'
    if not destination.is_dir() or destination.resolve()!=destination:raise PackageBlocked('PYTHON_SITE_ABSENT')
    with zipfile.ZipFile(source) as archive:
        entries=archive.infolist();seen=set();total=0
        if not entries or len(entries)>512:raise PackageBlocked('WHEEL_BUDGET_REJECTED')
        for entry in entries:
            name=safe_member(entry.filename)
            if not (name.startswith('websockets/') or name.startswith('websockets-17.2.dist-info/')):raise PackageBlocked('WHEEL_PACKAGE_REJECTED')
            if name.casefold() in seen or stat.S_ISLNK(entry.external_attr>>16):raise PackageBlocked('ARCHIVE_ALIAS_REJECTED')
            seen.add(name.casefold());total+=entry.file_size
            if total>16*1024*1024 or entry.flag_bits&1:raise PackageBlocked('WHEEL_BUDGET_REJECTED')
            if name.endswith(('.so','.pyd','.exe','.dll')):raise PackageBlocked('UNIVERSAL_WHEEL_REQUIRED')
            target=destination/name
            if target.is_symlink() or target.resolve(strict=False)!=target:raise PackageBlocked('WHEEL_PATH_REJECTED')
            if target.exists():raise PackageBlocked('WHEEL_OVERWRITE_REJECTED')
        files={}
        for entry in entries:
            name=safe_member(entry.filename);target=destination/name
            if entry.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True);hasher=hashlib.sha256()
            with archive.open(entry) as reader,target.open('xb') as writer:
                remaining=entry.file_size
                while remaining:
                    block=reader.read(min(1024*1024,remaining))
                    if not block:raise PackageBlocked('WHEEL_TRUNCATED')
                    writer.write(block);hasher.update(block);remaining-=len(block)
                if reader.read(1):raise PackageBlocked('WHEEL_SIZE_REJECTED')
            files['python/Lib/site-packages/'+name]={'bytes':entry.file_size,'sha256':hasher.hexdigest()}
        return {'version':'17.2','wheel_sha256':WEBSOCKETS_SHA256,'files':files}


def authority_input(binary,expected):
    if not isinstance(expected,str) or re.fullmatch('[0-9a-f]{64}',expected) is None or sha256_file(binary)!=expected:
        raise PackageBlocked('AUTHORITY_PIN_MISMATCH')
    windows_x64_image(binary)
    return {'sha256':expected,'native_execution':'pending','source_provenance':'caller_receipt_required'}


def write_archive(stage,destination):
    """Serialize only a bounded owned stage; validate the final archive separately."""
    root=Path(stage)
    if not root.is_absolute() or root.resolve(strict=True)!=root or not root.is_dir():raise PackageBlocked('STAGE_REJECTED')
    target=Path(destination)
    if not target.is_absolute() or target.exists() or target.is_symlink() or target.parent.resolve(strict=True)!=target.parent:raise PackageBlocked('NEW_ARCHIVE_REQUIRED')
    files=[];seen=set();total=0
    for path in root.rglob('*'):
        if path.is_symlink():raise PackageBlocked('STAGE_LINK_REJECTED')
        if path.is_dir():continue
        name=safe_member(path.relative_to(root).as_posix())
        if name.casefold() in seen or not path.is_file():raise PackageBlocked('STAGE_ALIAS_REJECTED')
        if path.name.casefold() in {'ffmpeg.exe','ffprobe.exe'}:raise PackageBlocked('FFMPEG_NOT_BUNDLED')
        seen.add(name.casefold());total+=path.stat().st_size;files.append((name,path))
        if len(files)>MAX_FILES or total>2*1024**3:raise PackageBlocked('STAGE_BUDGET_REJECTED')
    if not files:raise PackageBlocked('EMPTY_STAGE_REJECTED')
    fd,name=tempfile.mkstemp(prefix='.cafe-package-',suffix='.zip',dir=target.parent);os.close(fd);temporary=Path(name)
    linked=False
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=4) as archive:
            for relative,path in sorted(files):
                info=zipfile.ZipInfo(relative,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3;info.external_attr=0o100644<<16
                before=path.stat()
                with path.open('rb') as reader,archive.open(info,'w') as writer:shutil.copyfileobj(reader,writer,1024*1024)
                after=path.stat()
                if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise PackageBlocked('STAGE_CHANGED_DURING_ARCHIVE')
        with temporary.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
        # Publish only the already closed and flushed file. A hard link creates
        # the destination atomically and never replaces an existing path.
        os.link(temporary,target);linked=True
        return {'sha256':sha256_file(target),'bytes':target.stat().st_size,'files':len(files),'native_execution':'pending'}
    except BaseException:
        if linked and target.exists() and os.path.samestat(target.stat(),temporary.stat()):target.unlink()
        raise
    finally:temporary.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--python-archive',required=True);parser.add_argument('--websockets-wheel',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    created=False
    try:
        report=extract_verified_python(args.python_archive,args.out)
        created=True
        report['websockets']=install_verified_websockets(args.websockets_wheel,args.out)
        (Path(args.out)/'runtime-manifest.json').write_bytes(canonical(report))
        print(json.dumps({'schema':report['schema'],'status':'prepared_static_verified','files':len(report['files'])+len(report['websockets']['files']),'native_execution':'pending'}));return 0
    except (OSError,ValueError,tarfile.TarError,zipfile.BadZipFile):
        if created:shutil.rmtree(args.out)
        print(json.dumps({'schema':'cafe.private-python.v1','status':'blocked','code':'PRIVATE_RUNTIME_REJECTED'}));return 1


if __name__=='__main__':raise SystemExit(main())
