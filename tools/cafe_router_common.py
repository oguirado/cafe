"""Closed offline metadata, schema and byte-width contracts for the Café mount."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

CORPUS = 'cafe'
PROFILE = 'catalogue/source_profile.json'
NODES = 'catalogue/nodes.jsonl'
SYMBOLS = 'catalogue/symbols.jsonl'
COORDS = 'catalogue/coordinates.jsonl'
ROUTES = 'catalogue/routes.tsv'
RECEIPT = 'catalogue/router_sources.json'
CALIBRATION = 'catalogue/calibration.json'
TOOLS = ('tools/build_cafe_nodes.py', 'tools/resolve_cafe.py', 'tools/cafe_router_common.py',
         'tools/extract_rust.py', 'tools/extract_js.py', 'tools/extract_py.py', 'tools/qualify_cafe_router.py')
NODE_SCHEMA = 'router/standards/schemas/ctx.node.v2.schema.json'


class ContractError(ValueError):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def jsonl(rows):
    return b''.join(canonical(row) for row in rows)


def object_unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ContractError('duplicate_json_key')
        out[key] = value
    return out


def decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=object_unique)


def safe(root, rel, exists=True):
    if (not isinstance(rel, str) or not rel or '\\' in rel or '\0' in rel or
            rel.startswith('/') or ':' in rel or any(p in ('', '.', '..') for p in rel.split('/'))):
        raise ContractError('unsafe_relative_path')
    path = Path(root).resolve() / rel
    if path.resolve() != path or (exists and not path.is_file()):
        raise ContractError('missing_or_aliased_path')
    return path


def raw(root, rel):
    return safe(root, rel).read_bytes()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(root, rel):
    return decode(raw(root, rel))


def lines(root, rel):
    data = raw(root, rel)
    if not data.endswith(b'\n') or not data.strip():
        raise ContractError('incomplete_jsonl')
    return [decode(line) for line in data.splitlines()]


def tool(root, rel):
    path = safe(root, rel)
    name = 'cafe_router_' + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def inventory(root, snapshot=None):
    return tool(root, 'tools/qualify_cafe_router.py').load_inventory(Path(root), snapshot)[0]


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._:-]*', value) or len((CORPUS + ':' + value).encode()) > 32:
        raise ContractError('node_identity_width_or_shape')


def anchor(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 64 or '#' in value or any(ord(c) < 32 for c in value):
        raise ContractError('anchor_identity_width_or_shape')


def validate_nodes(root, nodes):
    schema = load(root, NODE_SCHEMA)
    validator = tool(root, 'router/tools/jsonschema_lite.py')
    ids, anchors = set(), set()
    if not nodes:
        raise ContractError('empty_nodes')
    for node in nodes:
        if validator.validate(node, schema):
            raise ContractError('node_schema_invalid')
        identifier(node['id'])
        if node['corpus'] != CORPUS or node['id'] in ids:
            raise ContractError('duplicate_or_foreign_node_id')
        ids.add(node['id'])
        family = node.get('family', node['id'])
        for entry in node['anchors']:
            anchor(entry['a'])
            key = (family, entry['a'])
            if key in anchors:
                raise ContractError('duplicate_family_anchor')
            anchors.add(key)


def check_calibration(root):
    state = load(root, CALIBRATION)
    if not isinstance(state, dict) or set(state) != {'schema', 'corpus', 'status', 'params'} or state.get('schema') != 'cafe.calibration.v1' or state.get('corpus') != CORPUS:
        raise ContractError('invalid_calibration_state')
    if state['status'] != 'uncalibrated':
        if not isinstance(state['params'], str) or not safe(root, state['params'], exists=False).is_file():
            raise ContractError('calibrated_claim_without_params')
        raise ContractError('calibration_evidence_not_implemented')
    if state['params'] is not None or safe(root, 'catalogue/router/params.json', exists=False).exists():
        raise ContractError('uncalibrated_mount_must_not_borrow_params')
    return state


def check_index(root):
    manifest = load(root, 'catalogue/router/manifest.json')
    if manifest.get('schema') != 'ctx.index-manifest.v2' or manifest.get('corpus') != CORPUS:
        raise ContractError('wrong_index_manifest')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ContractError('empty_index_manifest')
    for rel, digest in files.items():
        data = raw(root, 'catalogue/router/' + rel)
        if sha(data) != digest or len(data) > 393216:
            raise ContractError('index_file_hash_or_size')
        parsed = [decode(line) for line in data.splitlines()] if rel.endswith('.jsonl') else [decode(data)]
        def nofloat(value):
            if isinstance(value, float):
                raise ContractError('index_float')
            if isinstance(value, dict):
                for item in value.values(): nofloat(item)
            elif isinstance(value, list):
                for item in value: nofloat(item)
        for value in parsed: nofloat(value)
    identity = sha(''.join(f'{rel}\t{files[rel]}\n' for rel in sorted(files)).encode())
    if manifest.get('index_sha256') != identity or raw(root, 'catalogue/router/ID') != (identity + '\n').encode():
        raise ContractError('index_identity_mismatch')
    sources = manifest.get('sources', {})
    # Generic builder keys are logical provenance labels, not paper paths.
    if sources.get('nodes/nodes.jsonl') != sha(raw(root, NODES)) or sources.get('tools/build_index_v2.py') != sha(raw(root, 'router/tools/build_index_v2.py')) or sources.get('standards/thesaurus.json') != sha(b''):
        raise ContractError('index_source_binding_mismatch')
    return identity
