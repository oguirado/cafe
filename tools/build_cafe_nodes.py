"""Compose a lossless explicit-source Café mount; calibration is deliberately absent."""
import argparse
from pathlib import Path
import os
import re
import sys
import tempfile
import cafe_router_common as C

ROOT = Path(__file__).resolve().parents[1]


def read_profile(root, inventory):
    profile = C.load(root, C.PROFILE)
    if not isinstance(profile, dict) or set(profile) != {'schema', 'corpus', 'rust_roots', 'files'} or profile['schema'] != 'cafe.source-profile.v1' or profile['corpus'] != C.CORPUS:
        raise C.ContractError('invalid_source_profile')
    rows = profile['files']
    if not isinstance(rows, list) or not rows or rows != sorted(rows, key=lambda row: row['path']):
        raise C.ContractError('unsorted_source_profile')
    paths, ids = set(), set()
    for row in rows:
        if set(row) != {'path', 'id', 'language', 'symbol_prefix'} or row['language'] not in {'rust', 'js', 'python', 'markdown'}:
            raise C.ContractError('invalid_source_profile_row')
        C.safe(root, row['path'])
        C.identifier(row['id'])
        if row['path'] in paths or row['id'] in ids or row['path'] not in inventory:
            raise C.ContractError('duplicate_or_unversioned_source_profile')
        paths.add(row['path']); ids.add(row['id'])
    def eligible(path):
        return ((path.startswith('core/src/') and path.endswith('.rs')) or
                (path.startswith(('src/main/', 'src/preload/', 'src/renderer/', 'src/contracts/')) and path.endswith('.js')) or
                (path.startswith('connectors/') and path.endswith('.py')) or
                (path.startswith(('docs/', 'subjects/')) and path.endswith('.md')))
    expected = {path for path in inventory if eligible(path)} | {r['entry'] for r in profile['rust_roots']}
    if paths != expected:
        raise C.ContractError('source_profile_coverage_incomplete')
    return profile



def canonical_rust_report(path, contexts, declared_prefix):
    """Use the declared physical definition owner; other roots are witnesses only."""
    normalized, candidates = [], []
    for crate, cfg, report in contexts:
        prefix = crate + '::'
        module = report.get('module')
        if report.get('path') != path or not isinstance(module, str) or not (module == crate or module.startswith(prefix)):
            raise C.ContractError('invalid_compiled_source_namespace')
        surface = []
        for symbol in report['symbols']:
            full = symbol.get('symbol')
            if not isinstance(full, str) or not full.startswith(prefix):
                raise C.ContractError('invalid_compiled_symbol_namespace')
            # Only the known compiled crate prefix is removed. Module tails and
            # every other record field (including cfg, name and line) survive.
            surface.append(dict(symbol, symbol=full[len(prefix):]))
        physical = dict(report, module='' if module == crate else module[len(prefix):],
                        symbols=sorted(surface, key=lambda row: C.canonical(row)))
        normalized.append((cfg, physical))
        if module + '::' == declared_prefix and all(row['symbol'].startswith(declared_prefix) for row in report['symbols']):
            candidates.append(report)
    if any(context != normalized[0] for context in normalized[1:]):
        raise C.ContractError('compiled_source_context_mismatch')
    if len(candidates) != 1:
        raise C.ContractError('missing_or_ambiguous_canonical_source_owner')
    return candidates[0]


def extraction_reports(root, profile, inventory):
    reports, rust_contexts = {}, {}
    rust = C.tool(root, 'tools/extract_rust.py')
    import tomllib
    manifest = tomllib.loads(C.raw(root, 'Cargo.toml').decode())
    binaries = {row['path']: row['name'].replace('-', '_') for row in manifest['bin']}
    for item in profile['rust_roots']:
        if set(item) != {'entry', 'crate', 'files', 'cfg', 'module_paths'} or binaries.get(item['entry']) != item['crate']:
            raise C.ContractError('rust_root_not_declared_by_cargo')
        result = rust.extract(root, item['files'], inventory, item['cfg'], entry=item['entry'], crate=item['crate'], module_paths=item['module_paths'], include_modules=True)
        for file in result['files']:
            rust_contexts.setdefault(file['path'], []).append((item['crate'], item['cfg'], file))
    declared = {row['path']: row['symbol_prefix'] for row in profile['files'] if row['language'] == 'rust'}
    for path, contexts in rust_contexts.items():
        if path not in declared:
            raise C.ContractError('undeclared_compiled_source')
        reports[path] = (contexts[0][2] if len(contexts) == 1 else
                         canonical_rust_report(path, contexts, declared[path]))
    for language, tool in [('js', 'tools/extract_js.py'), ('python', 'tools/extract_py.py')]:
        paths = sorted(row['path'] for row in profile['files'] if row['language'] == language)
        if paths:
            for file in C.tool(root, tool).extract(root, paths, inventory)['files']:
                if file['path'] in reports:
                    raise C.ContractError('multiple_source_namespace_owners')
                reports[file['path']] = file
    if set(reports) != {r['path'] for r in profile['files'] if r['language'] != 'markdown'}:
        raise C.ContractError('extractor_profile_coverage_mismatch')
    return reports


def document_locations(root, path, data):
    text = data.decode('utf-8')
    blocks = C.tool(root, 'router/tools/blocks.py')
    blocks.blocks_from_text(text, path)  # Validate any actual typed companion blocks.
    locations = []
    for match in blocks.ANCHOR_RE.finditer(text):
        locations.append({'anchor': match[1], 'name': match[1], 'line': text.count('\n', 0, match.start()) + 1, 'symbol': None, 'role': 'section'})
    number = 0
    for line, value in enumerate(text.splitlines(), 1):
        if re.match(r'^#{1,6} ', value):
            number += 1
            locations.append({'anchor': 'heading-' + str(number), 'name': value.strip(), 'line': line, 'symbol': None, 'role': 'section'})
    return locations


def compose(root, inventory):
    profile = read_profile(root, inventory)
    reports = extraction_reports(root, profile, inventory)
    nodes, symbols, coordinates, source_hashes, families = [], [], [], {}, {}
    for row in profile['files']:
        path, base = row['path'], row['id']
        data = C.raw(root, path); digest = C.sha(data)
        source_hashes[path] = digest; families[base] = path
        if row['language'] == 'markdown':
            locations = document_locations(root, path, data)
        else:
            file = reports[path]
            if file['source_sha256'] != digest:
                raise C.ContractError('source_changed_during_extraction')
            expected_prefix = (file['module'] + ('::' if row['language'] == 'rust' else '.')
                               if row['language'] in {'rust', 'python'} else
                               'cafe_shell::' + path[4:-3].replace('/', '::') + '::')
            if row['symbol_prefix'] != expected_prefix:
                raise C.ContractError('clipped_or_incorrect_module_namespace')
            locations = []
            for symbol in file['symbols']:
                full = symbol['symbol']; prefix = row['symbol_prefix']
                if not prefix or not full.startswith(prefix):
                    raise C.ContractError('symbol_namespace_profile_mismatch')
                local = full[len(prefix):]  # Exact namespace removal, never clipping.
                C.anchor(local)
                public = {key: symbol[key] for key in ['path', 'symbol', 'name', 'line']}
                symbols.append(public)
                locations.append({'anchor': local, 'name': symbol['name'], 'line': symbol['line'], 'symbol': full, 'role': 'symbol'})
        if not locations:
            first = next(((i, line.strip()) for i, line in enumerate(data.decode('utf-8').splitlines(), 1) if line.strip()), None)
            if first is None:
                raise C.ContractError('empty_source_has_no_coordinate')
            locations = [{'anchor': 'file', 'name': first[1], 'line': first[0], 'symbol': None, 'role': 'asset'}]
        seen = set()
        for loc in locations:
            C.anchor(loc['anchor'])
            if loc['anchor'] in seen:
                raise C.ContractError('duplicate_source_anchor')
            seen.add(loc['anchor'])
        locations.sort(key=lambda loc: (loc['line'], loc['anchor']))
        for offset in range(0, len(locations), 16):
            part = locations[offset:offset + 16]
            ident = base if offset == 0 else base + '-p' + str(offset // 16 + 1)
            C.identifier(ident)
            node = {'schema': 'ctx.node.v2', 'id': ident, 'corpus': C.CORPUS,
                    'kind': 'section' if row['language'] == 'markdown' else 'symbol',
                    'title': path + (' part ' + str(offset // 16 + 1) if offset else ''),
                    'purpose': 'Exact ' + row['language'] + ' source coordinates for ' + path,
                    'family': base, 'anchors': [{'a': loc['anchor'], 'role': loc['role']} for loc in part],
                    'search': {'triggers': [loc['anchor'] for loc in part[:12]], 'keywords': [loc['anchor'] for loc in part]},
                    'status': 'active', 'source_sha256': digest}
            nodes.append(node)
            for loc in part:
                coordinates.append({'id': ident, 'anchor': loc['anchor'], 'path': path, 'name': loc['name'], 'line': loc['line'], 'symbol': loc['symbol'], 'source_sha256': digest})
    nodes.sort(key=lambda node: node['id'])
    symbols.sort(key=lambda symbol: (symbol['path'], symbol['symbol']))
    coordinates.sort(key=lambda loc: (loc['id'], loc['anchor']))
    if len({row['symbol'] for row in symbols}) != len(symbols):
        raise C.ContractError('duplicate_full_symbol')
    C.validate_nodes(root, nodes)
    paths = {row['id']: row['path'] for row in coordinates}
    routes = ('id\tpath\n' + ''.join(node['id'] + '\t' + paths[node['id']] + '\n' for node in nodes)).encode()
    receipt = {'schema': 'cafe.router-sources.v1', 'corpus': C.CORPUS, 'profile_sha256': C.sha(C.raw(root, C.PROFILE)),
               'node_schema_sha256': C.sha(C.raw(root, C.NODE_SCHEMA)),
               'metadata': {path: C.sha(C.raw(root, path)) for path in ['Cargo.toml', 'catalogue/ownership.json', 'catalogue/protocol.json']}, 'sources': dict(sorted(source_hashes.items())),
               'tools': {path: C.sha(C.raw(root, path)) for path in C.TOOLS}, 'families': dict(sorted(families.items()))}
    return {C.NODES: C.jsonl(nodes), C.SYMBOLS: C.jsonl(symbols), C.COORDS: C.jsonl(coordinates), C.ROUTES: routes, C.RECEIPT: C.canonical(receipt)}


def enforce_admitted_symbols(root, products):
    if C.safe(root, C.SYMBOLS, exists=False).exists():
        admitted = {(s['path'], s['symbol'], s['name']) for s in C.lines(root, C.SYMBOLS)}
        current = {(s['path'], s['symbol'], s['name']) for s in [C.decode(line) for line in products[C.SYMBOLS].splitlines()]}
        if not admitted <= current:
            raise C.ContractError('public_symbol_removed_requires_review')


def write_products(root, products):
    for rel, data in products.items():
        path = C.safe(root, rel, exists=False); path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.cafe-router-', delete=False) as stream:
                temp = Path(stream.name)
                stream.write(data); stream.flush()
            # Windows rejects replacing/unlinking this name while its handle is open.
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


def build(root, inventory, check=False):
    products = compose(root, inventory)
    enforce_admitted_symbols(root, products)
    C.check_calibration(root)
    if check:
        if any(C.raw(root, rel) != data for rel, data in products.items()):
            raise C.ContractError('composition_not_byte_identical')
    else:
        write_products(root, products)
    return {'schema': 'cafe.nodes-build.v1', 'nodes': len(products[C.NODES].splitlines()), 'symbols': len(products[C.SYMBOLS].splitlines()), 'check': check, 'calibration': 'uncalibrated'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--inventory')
    args = parser.parse_args(argv)
    try:
        import json
        result = build(ROOT, C.inventory(ROOT, args.inventory or os.environ.get('CAFE_ROUTER_INVENTORY')), args.check)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(C.canonical({'error': str(exc) if isinstance(exc, C.ContractError) else 'source_composition_unavailable'}).decode(), file=sys.stderr, end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
