"""Resolve exact, fresh Café coordinates; exploratory ranking is uncalibrated."""
import argparse
from pathlib import Path
import os
import subprocess
import sys
import cafe_router_common as C
import build_cafe_nodes as B

ROOT = Path(__file__).resolve().parents[1]


def verify(root, inventory):
    root = Path(root).resolve()
    C.check_calibration(root)
    nodes = C.lines(root, C.NODES)
    C.validate_nodes(root, nodes)
    receipt = C.load(root, C.RECEIPT)
    if not isinstance(receipt, dict) or set(receipt) != {'schema', 'corpus', 'profile_sha256', 'node_schema_sha256', 'sources', 'tools', 'families', 'metadata'} or receipt['schema'] != 'cafe.router-sources.v1' or receipt['corpus'] != C.CORPUS:
        raise C.ContractError('invalid_source_receipt')
    for path, digest in receipt['sources'].items():
        if C.sha(C.raw(root, path)) != digest:
            raise C.ContractError('stale_source_hash')
    coordinates = C.lines(root, C.COORDS)
    by_id = {node['id']: node for node in nodes}
    route_lines = C.raw(root, C.ROUTES).decode('utf-8').splitlines()
    if not route_lines or route_lines[0] != 'id\tpath':
        raise C.ContractError('invalid_generic_route_header')
    lookup = {}
    for line in route_lines[1:]:
        fields = line.split('\t')
        if len(fields) != 2:
            raise C.ContractError('invalid_generic_route_row')
        ident, path = fields
        C.identifier(ident); C.safe(root, path)
        if ident in lookup:
            raise C.ContractError('duplicate_generic_route_id')
        lookup[ident] = path
    if set(lookup) != set(by_id):
        raise C.ContractError('generic_route_coverage_mismatch')
    pairs = {}
    family_paths = {}
    expected_fields = {'id', 'anchor', 'path', 'name', 'line', 'symbol', 'source_sha256'}
    for row in coordinates:
        if not isinstance(row, dict) or set(row) != expected_fields or row['id'] not in by_id:
            raise C.ContractError('invalid_coordinate_record')
        C.anchor(row['anchor'])
        if row['path'] != lookup[row['id']]:
            raise C.ContractError('coordinate_does_not_match_generic_lookup')
        path = C.safe(root, lookup[row['id']])
        if type(row['line']) is not int or row['line'] < 1 or not isinstance(row['name'], str) or not row['name']:
            raise C.ContractError('invalid_coordinate_name_or_line')
        node = by_id[row['id']]
        key = (row['id'], row['anchor'])
        if key in pairs or row['anchor'] not in {a['a'] for a in node['anchors']}:
            raise C.ContractError('duplicate_or_unadvertised_coordinate')
        data = path.read_bytes()
        if C.sha(data) != row['source_sha256'] or row['source_sha256'] != node['source_sha256']:
            raise C.ContractError('coordinate_source_hash_mismatch')
        # Python encoding cookies are honored by its extractor; use the same text view.
        if row['path'].endswith('.py'):
            import io, tokenize
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        else:
            text = data.decode('utf-8')
        lines = text.splitlines()
        if row['line'] > len(lines) or row['name'] not in lines[row['line'] - 1]:
            raise C.ContractError('stale_coordinate_line_or_name')
        family = node.get('family', node['id'])
        if family in family_paths and family_paths[family] != row['path']:
            raise C.ContractError('family_crosses_source_files')
        family_paths[family] = row['path']
        pairs[key] = row
    if set(pairs) != {(n['id'], a['a']) for n in nodes for a in n['anchors']}:
        raise C.ContractError('missing_advertised_coordinate')
    symbols = C.lines(root, C.SYMBOLS)
    if any(not isinstance(s, dict) or set(s) != {'path', 'symbol', 'name', 'line'} for s in symbols):
        raise C.ContractError('invalid_symbol_record')
    actual_symbols = [{k: row[k] for k in ['path', 'symbol', 'name', 'line']} for row in coordinates if row['symbol'] is not None]
    if sorted(symbols, key=lambda s: (s['path'], s['symbol'])) != sorted(actual_symbols, key=lambda s: (s['path'], s['symbol'])):
        raise C.ContractError('admitted_symbol_coverage_mismatch')
    # Re-extraction detects public removals even after honest source rehash/index rebuild.
    expected = B.compose(root, inventory)
    if expected[C.SYMBOLS] != C.raw(root, C.SYMBOLS):
        raise C.ContractError('current_public_surface_differs_from_admitted_symbols')
    if any(C.raw(root, rel) != data for rel, data in expected.items()):
        raise C.ContractError('source_or_metadata_composition_drift')
    identity = C.check_index(root)
    return nodes, coordinates, identity


def resolve(root, key, inventory):
    if not isinstance(key, str) or not key or key != key.strip():
        raise C.ContractError('invalid_exact_key')
    nodes, coordinates, _ = verify(root, inventory)
    node_namespace = key.startswith(C.CORPUS + ':')
    if node_namespace:
        key = key[len(C.CORPUS) + 1:]
        C.identifier(key.split('#', 1)[0])
    ids = {node['id']: node for node in nodes}
    candidates = []
    if '#' in key:
        ident, anchor = key.split('#', 1)
        C.identifier(ident); C.anchor(anchor)
        candidates = [r for r in coordinates if r['id'] == ident and r['anchor'] == anchor]
    elif key in ids:
        primary = ids[key]['anchors'][0]['a']
        candidates = [r for r in coordinates if r['id'] == key and r['anchor'] == primary]
        if not node_namespace:
            candidates += [r for r in coordinates if r['symbol'] == key or r['anchor'] == key]
    elif node_namespace:
        raise C.ContractError('missing_exact_namespaced_node_id')
    else:
        candidates = [r for r in coordinates if r['symbol'] == key or r['anchor'] == key]
    # The same coordinate cannot be selected twice through different key forms.
    unique = {(r['path'], r['line'], r['name'], r['source_sha256']): r for r in candidates}
    if len(unique) != 1:
        raise C.ContractError('missing_or_ambiguous_exact_key')
    row = next(iter(unique.values()))
    return {key: row[key] for key in ['path', 'line', 'name', 'source_sha256']}


def route(root, query, inventory, acknowledged=False):
    if not acknowledged:
        raise C.ContractError('explicit_uncalibrated_ack_required')
    verify(root, inventory)
    result = subprocess.run([sys.executable, '-B', 'router/tools/route_v2.py', '--index', 'catalogue/router', '--q', query], cwd=root, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise C.ContractError('uncalibrated_kit_route_failed')
    return {'calibration': 'uncalibrated', 'warning': 'Exploratory shortlist only; resolve and verify exact coordinates before editing.', 'shortlist': C.decode(result.stdout.encode())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('key', nargs='?')
    parser.add_argument('--route')
    parser.add_argument('--ack-uncalibrated', action='store_true')
    parser.add_argument('--inventory')
    args = parser.parse_args(argv)
    try:
        if bool(args.key) == bool(args.route):
            raise C.ContractError('select_exact_key_or_exploratory_query')
        inventory = C.inventory(ROOT, args.inventory or os.environ.get('CAFE_ROUTER_INVENTORY'))
        result = route(ROOT, args.route, inventory, args.ack_uncalibrated) if args.route else resolve(ROOT, args.key, inventory)
        sys.stdout.buffer.write(C.canonical(result))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        sys.stderr.buffer.write(C.canonical({'error': str(exc) if isinstance(exc, C.ContractError) else 'offline_resolution_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
