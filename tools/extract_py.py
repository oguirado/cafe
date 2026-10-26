"""Deterministic explicit-file Python AST surface, never runtime introspection.

Only local module declarations and literal constant bindings are supported.
Imports, dynamic exports, conditional public bindings and computed __all__ are
not guessed. This is a syntactic inventory, not an effect or type analyzer.
"""
import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tokenize

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cafe_python_surface_ownership', ROOT / 'tools/qualify_cafe_router.py')
q = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = q
SPEC.loader.exec_module(q)


class ExtractionError(ValueError):
    def __init__(self, code, path=None, line=None):
        self.code, self.path, self.line = code, path, line
        super().__init__(code)


def _public(name):
    return isinstance(name, str) and name.isidentifier() and not name.startswith('_')


def _error(code, path, node):
    raise ExtractionError(code, path, getattr(node, 'lineno', None))


class _DynamicGuard(ast.NodeVisitor):
    """Inspect module-executed syntax, without entering deferred function bodies."""
    def __init__(self, path, allowed_all):
        self.path, self.allowed_all = path, allowed_all

    def visit_FunctionDef(self, node):
        for value in node.decorator_list + node.args.defaults + [v for v in node.args.kw_defaults if v is not None]:
            self.visit(value)
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [v for v in (node.args.vararg, node.args.kwarg) if v]:
            if arg.annotation:
                self.visit(arg.annotation)
        if node.returns:
            self.visit(node.returns)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v is not None]:
            self.visit(value)

    def visit_ImportFrom(self, node):
        if any(alias.name == '*' for alias in node.names):
            _error('star_import_unsupported', self.path, node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in {'exec', 'globals'}:
            _error('dynamic_module_namespace', self.path, node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == '__all__' and id(node) not in self.allowed_all:
            _error('computed_or_mutated_all', self.path, node)


def _all_declaration(tree, path):
    selected = None
    allowed = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == '__all__':
                _error('computed_or_mutated_all', path, node)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any((v.asname or v.name) == '__all__' for v in node.names):
                _error('computed_or_mutated_all', path, node)
            continue
        if not any(isinstance(t, ast.Name) and t.id == '__all__' for t in targets):
            continue
        if (selected is not None or len(targets) != 1 or
                not isinstance(node.value, ast.List) or
                any(not isinstance(v, ast.Constant) or not isinstance(v.value, str) for v in node.value.elts)):
            _error('computed_or_mutated_all', path, node)
        names = [v.value for v in node.value.elts]
        if len(names) != len(set(names)) or any(not _public(n) for n in names):
            _error('invalid_public_all', path, node)
        selected = set(names)
        allowed.add(id(targets[0]))
    return selected, allowed


def _module_bindings(node):
    """Names bound by compound module statements; function/class bodies excluded."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        yield node.id
    if isinstance(node, ast.ExceptHandler) and node.name:
        yield node.name
    for child in ast.iter_child_nodes(node):
        yield from _module_bindings(child)


def _parse(path, source, module, owner):
    try:
        tree = ast.parse(source, filename=path)
        # Compile the AST for context-sensitive SyntaxError, never execute code.
        compile(tree, path, 'exec', dont_inherit=True)
    except SyntaxError as exc:
        raise ExtractionError('syntax_error', path, exc.lineno) from None
    except (ValueError, RecursionError, MemoryError):
        raise ExtractionError('parser_limit_or_unsupported_syntax', path) from None
    selected, allowed = _all_declaration(tree, path)
    _DynamicGuard(path, allowed).visit(tree)
    declarations = {}
    imported = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {v.asname or (v.name.split('.')[0] if isinstance(node, ast.Import) else v.name) for v in node.names}
            if names & declarations.keys():
                _error('ambiguous_public_binding', path, node)
            imported.update(names)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            kind = 'class' if isinstance(node, ast.ClassDef) else 'async_function' if isinstance(node, ast.AsyncFunctionDef) else 'function'
            at = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                name = targets[0].id
                if name == '__all__' or not _public(name) or (selected is not None and name not in selected):
                    continue
                try:
                    ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
                    _error('nonliteral_public_binding', path, node)
                kind, at = 'constant', targets[0]
            else:
                if any(_public(n) and (selected is None or n in selected) for n in _module_bindings(node)):
                    _error('ambiguous_public_binding', path, node)
                continue
        else:
            if any(_public(n) and (selected is None or n in selected) for n in _module_bindings(node)):
                _error('conditional_or_dynamic_public_binding', path, node)
            continue
        if not _public(name) or (selected is not None and name not in selected):
            continue
        if name in declarations or name in imported:
            _error('ambiguous_public_binding', path, node)
        declarations[name] = {'path': path, 'symbol': module + '.' + name, 'name': name,
                              'line': at.lineno, 'owner': owner, 'kind': kind}
    if selected is not None and set(declarations) != selected:
        raise ExtractionError('unresolved_or_imported_all_export', path)
    return sorted(declarations.values(), key=lambda row: (row['line'], row['symbol']))


def extract(root, paths, inventory):
    root = Path(root).resolve()
    if (not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths) or
            paths != sorted(set(paths))):
        raise ExtractionError('explicit_sorted_unique_files_required')
    if not isinstance(inventory, list) or any(not isinstance(p, str) for p in inventory):
        raise ExtractionError('version_inventory_required')
    prepared = []
    for rel in paths:
        if (not q.safe_relative(rel) or not rel.startswith('connectors/') or not rel.endswith('.py') or rel not in inventory):
            raise ExtractionError('unversioned_or_unsafe_python_path', rel if q.safe_relative(rel) else None)
        path = root / rel
        try:
            if path.resolve() != path or not path.is_file():
                raise ExtractionError('missing_or_aliased_source', rel)
        except (OSError, RuntimeError):
            raise ExtractionError('missing_or_aliased_source', rel) from None
        parts = list(Path(rel).with_suffix('').parts[1:])
        if parts[-1] == '__init__':
            parts.pop()
        if not parts or any(not part.isidentifier() for part in parts):
            raise ExtractionError('unsupported_module_path', rel)
        prepared.append((rel, path, '.'.join(parts)))
    # Ownership policy is metadata; no source outside the explicit list is opened.
    if not q.check_ownership(root, paths).passed:
        raise ExtractionError('invalid_or_ambiguous_ownership')
    policy = json.loads((root / 'catalogue/ownership.json').read_text(encoding='utf-8'))
    files = []
    for rel, path, module in prepared:
        owner = next(row['path'] for row in policy['roots']
                     if rel == row['path'] or (row['path'].endswith('/') and rel.startswith(row['path'])))
        try:
            raw = path.read_bytes()
        except OSError:
            raise ExtractionError('source_read_failed', rel) from None
        digest = hashlib.sha256(raw).hexdigest()
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            source = raw.decode(encoding)
        except (SyntaxError, UnicodeError, LookupError):
            raise ExtractionError('source_encoding_error', rel) from None
        files.append({'path': rel, 'module': module, 'owner': owner,
                      'source_sha256': digest, 'symbols': _parse(rel, source, module, owner)})
    return {'schema': 'cafe.python-surface.v1', 'parser': 'stdlib.ast', 'files': files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', nargs='+', required=True)
    parser.add_argument('--inventory', help='explicit versioned-file snapshot for no-Git copies')
    args = parser.parse_args(argv)
    try:
        inventory, _ = q.load_inventory(ROOT, args.inventory or os.environ.get('CAFE_ROUTER_INVENTORY'))
        report = extract(ROOT, args.files, inventory)
    except ExtractionError as exc:
        error = {'error': exc.code}
        if exc.path is not None:
            error['path'] = exc.path
        if exc.line is not None:
            error['line'] = exc.line
        print(json.dumps(error, sort_keys=True, ensure_ascii=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('{"error":"version_inventory_unavailable"}', file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
