"""Inspect the actual Windows executable extracted from a bounded shell ZIP."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile
import zipfile

from run_integration_gate import run_owned

ROOT = Path(__file__).resolve().parents[1]


class Rejected(ValueError):
    pass


def closed_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Rejected('DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def safe_member(name):
    if not isinstance(name, str) or not name or chr(92) in name or chr(0) in name or ':' in name:
        raise Rejected('ZIP_PATH_REJECTED')
    trimmed = name[:-1] if name.endswith('/') else name
    parts = trimmed.split('/')
    if name.startswith('/') or any(part in ['', '.', '..'] for part in parts):
        raise Rejected('ZIP_PATH_REJECTED')
    if str(PurePosixPath(trimmed)) != trimmed:
        raise Rejected('ZIP_PATH_REJECTED')
    return trimmed


def pe_integrity(data):
    """Read standard PE resource metadata; no fuse bytes are parsed or patched."""
    def read(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise Rejected('PE_BOUNDS_REJECTED')
        return struct.unpack_from(fmt, data, offset)
    if data[:2] != b'MZ':
        raise Rejected('PE_REJECTED')
    pe, = read('<I', 60)
    if data[pe:pe+4] != bytes([80, 69, 0, 0]):
        raise Rejected('PE_REJECTED')
    machine, count = read('<HH', pe+4)
    optional_size, = read('<H', pe+20)
    if machine != 0x8664 or not 1 <= count <= 96:
        raise Rejected('PE_PLATFORM_REJECTED')
    optional = pe+24
    magic, = read('<H', optional)
    if magic != 0x20b or optional_size < 136:
        raise Rejected('PE_OPTIONAL_REJECTED')
    resource_rva, resource_size = read('<II', optional+112+16)
    sections = []
    for index in range(count):
        offset = optional+optional_size+index*40
        virtual_size, virtual_address, raw_size, raw_offset = read('<IIII', offset+8)
        if raw_offset+raw_size > len(data):
            raise Rejected('PE_SECTION_REJECTED')
        sections.append((virtual_address, raw_size, raw_offset))
    def mapped(rva, size):
        matches = [raw+(rva-address) for address, length, raw in sections
                   if address <= rva and rva+size <= address+length]
        if len(matches) != 1:
            raise Rejected('PE_RVA_REJECTED')
        return matches[0]
    if resource_size == 0 or resource_size > 64*1024*1024:
        raise Rejected('PE_RESOURCE_REJECTED')
    base = mapped(resource_rva, resource_size)
    def bounded(offset, size):
        if offset < 0 or offset+size > resource_size:
            raise Rejected('PE_RESOURCE_BOUNDS_REJECTED')
        return base+offset
    def name_value(raw):
        if not raw & 0x80000000:
            return raw
        offset = raw & 0x7fffffff
        length, = read('<H', bounded(offset, 2))
        return data[bounded(offset+2, length*2):base+offset+2+length*2].decode('utf-16-le')
    def entries(offset):
        _, _, _, _, named, numbered = read('<IIHHHH', bounded(offset, 16))
        total = named+numbered
        if total > 4096:
            raise Rejected('PE_RESOURCE_COUNT_REJECTED')
        values = []
        for index in range(total):
            label, target = read('<II', bounded(offset+16+index*8, 8))
            values.append((name_value(label), bool(target & 0x80000000), target & 0x7fffffff))
        return values
    types = [row for row in entries(0) if row[0] == 'INTEGRITY']
    if len(types) != 1 or not types[0][1]:
        raise Rejected('EMBEDDED_INTEGRITY_ABSENT')
    names = [row for row in entries(types[0][2]) if row[0] == 'ELECTRONASAR']
    if len(names) != 1 or not names[0][1]:
        raise Rejected('EMBEDDED_INTEGRITY_ABSENT')
    payloads = []
    for _, directory, offset in entries(names[0][2]):
        if directory:
            raise Rejected('PE_RESOURCE_DEPTH_REJECTED')
        rva, length, _, _ = read('<IIII', bounded(offset, 16))
        if not 1 <= length <= 65536:
            raise Rejected('EMBEDDED_INTEGRITY_REJECTED')
        start = mapped(rva, length)
        payloads.append(data[start:start+length])
    if not payloads or len(set(payloads)) != 1:
        raise Rejected('EMBEDDED_INTEGRITY_AMBIGUOUS')
    records = json.loads(payloads[0].decode('utf-8'), object_pairs_hook=closed_object)
    if not isinstance(records, list) or len(records) != 1:
        raise Rejected('EMBEDDED_INTEGRITY_REJECTED')
    row = records[0]
    # Official packager emits the platform-native separator for the same fixed ASAR.
    fixed_asar_names = {'resources/app.asar', 'resources'+chr(92)+'app.asar'}
    if set(row) != {'file', 'alg', 'value'} or row['file'] not in fixed_asar_names or row['alg'] != 'SHA256':
        raise Rejected('EMBEDDED_INTEGRITY_REJECTED')
    if not isinstance(row['value'], str) or len(row['value']) != 64 or any(c not in '0123456789abcdef' for c in row['value']):
        raise Rejected('EMBEDDED_INTEGRITY_REJECTED')
    return row['value']


def inspect_archive(archive_path, executable, node):
    archive_path = Path(archive_path).resolve(strict=True)
    if not archive_path.is_file() or archive_path.stat().st_size > 512*1024*1024:
        raise Rejected('ZIP_SIZE_REJECTED')
    executable = safe_member(executable)
    relative = PurePosixPath(executable)
    if relative.suffix.lower() != '.exe':
        raise Rejected('ZIP_EXECUTABLE_REJECTED')
    prefix = str(relative.parent)
    prefix = '' if prefix == '.' else prefix+'/'
    asar_name = prefix+'resources/app.asar'
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= 8192 or sum(row.file_size for row in entries) > 2*1024**3:
            raise Rejected('ZIP_BUDGET_REJECTED')
        names, folded = {}, set()
        for row in entries:
            name = safe_member(row.filename)
            if name.casefold() in folded or row.flag_bits & 1 or stat.S_ISLNK(row.external_attr >> 16):
                raise Rejected('ZIP_ENTRY_REJECTED')
            if row.compress_type not in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED] or row.file_size > 512*1024*1024:
                raise Rejected('ZIP_ENTRY_REJECTED')
            folded.add(name.casefold());names[name] = row
            loose = prefix+'resources/app'
            if name == loose or name.startswith(loose+'/') or name == prefix+'resources/default_app.asar':
                raise Rejected('UNPACKED_APP_FALLBACK_REJECTED')
            if name.startswith(prefix+'resources/app.asar.unpacked/src/main/') or name.startswith(prefix+'resources/app.asar.unpacked/dist/preload/'):
                raise Rejected('UNPACKED_APP_FALLBACK_REJECTED')
        if executable not in names or asar_name not in names or names[executable].is_dir() or names[asar_name].is_dir():
            raise Rejected('PACKAGE_RESOURCES_ABSENT')
        with tempfile.TemporaryDirectory(prefix='cafe-shell-archive-') as temp:
            temp = Path(temp)
            binary, asar = temp/'main.exe', temp/'app.asar'
            for source, target in [(executable, binary), (asar_name, asar)]:
                with archive.open(names[source]) as reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader, writer, 1024*1024)
            embedded = pe_integrity(binary.read_bytes())
            result = run_owned([node, str(ROOT/'tools/check_shell_fuses.js'), str(binary), '--asar', str(asar)],
                               ROOT, dict(os.environ), 90)
            report = json.loads(result.stdout, object_pairs_hook=closed_object)
            if report.get('structure') != 'pass' or report.get('asar', {}).get('header_sha256') != embedded:
                raise Rejected('ASAR_INTEGRITY_MISMATCH')
            if result.returncode not in [0, 1] or (result.returncode == 0) != (report.get('status') == 'pass'):
                raise Rejected('FUSE_GATE_PROTOCOL_REJECTED')
            if result.returncode and report.get('code') != 'FUSE_POLICY_REJECTED':
                raise Rejected('FUSE_GATE_PROTOCOL_REJECTED')
            return {**report, 'schema':'cafe.shell-archive.v1', 'embedded_asar_integrity':'verified',
                    'exe_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(), 'archive_executable_extracted':True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    parser.add_argument('--exe', required=True)
    parser.add_argument('--node', default='node')
    args = parser.parse_args(argv)
    try:
        report = inspect_archive(args.archive, args.exe, args.node)
    except (OSError, ValueError, TypeError, KeyError, struct.error, zipfile.BadZipFile):
        report = {'schema':'cafe.shell-archive.v1','status':'fail','code':'ARCHIVE_OR_PACKAGE_REJECTED'}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
