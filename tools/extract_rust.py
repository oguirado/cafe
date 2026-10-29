"""Bounded token-based Rust inventory; unsupported public syntax refuses explicitly.
Disabled private extern callbacks are skipped only under an explicit false cfg; active extern callbacks abstain."""
import argparse
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
ENTRY = 'core/src/main.rs'
CRATE = 'cafe_core'
KINDS = {'fn', 'struct', 'enum', 'trait', 'type', 'const', 'mod'}
ATTRS = {'serde', 'allow', 'deny', 'warn', 'forbid', 'doc', 'repr', 'inline',
         'must_use', 'non_exhaustive', 'cold', 'test'}
DERIVES = {'Clone', 'Copy', 'Debug', 'PartialEq', 'Eq', 'PartialOrd', 'Ord',
           'Serialize', 'Deserialize', 'Default', 'Hash'}
IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')


class ExtractionError(ValueError):
    def __init__(self, code, path='', line=None):
        self.code, self.path, self.line = code, path, line
        super().__init__(code)


class Token(NamedTuple):
    text: str
    line: int
    kind: str = 'punct'


def tokens(text, path):
    """Lex comments/literals before punctuation; never search source with pub regexes."""
    out, i, line = [], 0, 1
    def fail():
        raise ExtractionError('unsupported_or_unterminated_literal', path, line)
    while i < len(text):
        start, begin = i, line
        if text[i].isspace():
            line += text[i] == '\n'; i += 1; continue
        if text.startswith('//', i):
            end = text.find('\n', i); i = len(text) if end < 0 else end; continue
        if text.startswith('/*', i):
            i += 2; depth = 1
            while i < len(text) and depth:
                if text.startswith('/*', i): depth += 1; i += 2
                elif text.startswith('*/', i): depth -= 1; i += 2
                else: line += text[i] == '\n'; i += 1
            if depth: fail()
            continue
        raw = re.match(r'(?:br|cr|r)(#*)"', text[i:])
        if raw:
            endmark = '"' + raw[1]; end = text.find(endmark, i + len(raw[0]))
            if end < 0: fail()
            i = end + len(endmark)
            out.append(Token(text[start:i], begin, 'literal'))
            line += text[start:i].count('\n'); continue
        prefix = 1 if text[i:i+2] in ['b"', 'c"', "b'"] else 0
        quote = text[i + prefix]
        # A quote followed by an identifier without immediate closing quote is a lifetime.
        lifetime = quote == "'" and not prefix and i + 1 < len(text) and (text[i+1].isalpha() or text[i+1] == '_') and text[i+2:i+3] != "'"
        if quote == '"' or (quote == "'" and not lifetime):
            i += prefix + 1
            while i < len(text):
                if text[i] == '\\': i += 2
                elif text[i] == quote: i += 1; break
                else: i += 1
            else: fail()
            if i > len(text): fail()
            out.append(Token(text[start:i], begin, 'literal'))
            line += text[start:i].count('\n'); continue
        if text.startswith('r#', i):
            raise ExtractionError('unsupported_raw_identifier', path, line)
        if text[i].isascii() and (text[i].isalpha() or text[i] == '_'):
            i += 1
            while i < len(text) and text[i].isascii() and (text[i].isalnum() or text[i] == '_'): i += 1
            out.append(Token(text[start:i], begin, 'ident')); continue
        if not text[i].isascii():
            raise ExtractionError('unsupported_non_ascii_code', path, line)
        out.append(Token(text[i], line)); i += 1
    stack, pairs = [], {}
    for n, token in enumerate(out):
        if token.text in ['(', '[', '{']: stack.append(n)
        elif token.text in [')', ']', '}']:
            if not stack or out[stack[-1]].text != {')':'(', ']':'[', '}':'{'}[token.text]:
                raise ExtractionError('unbalanced_delimiter', path, token.line)
            first = stack.pop(); pairs[first] = n
    if stack: raise ExtractionError('unbalanced_delimiter', path, out[stack[-1]].line)
    if any(token.text == 'macro_rules' for token in out if token.kind == 'ident'):
        raise ExtractionError('macro_expansion_unavailable', path)
    return out, pairs


def profile_map(profile):
    if profile is None: return {}
    if not isinstance(profile, dict): raise ExtractionError('invalid_cfg_profile')
    for key, value in profile.items():
        if not isinstance(key, str) or not IDENT.fullmatch(key): raise ExtractionError('invalid_cfg_profile')
        if type(value) not in [bool, str, list]: raise ExtractionError('invalid_cfg_profile')
        if isinstance(value, list) and (any(not isinstance(v, str) for v in value) or value != sorted(set(value))):
            raise ExtractionError('invalid_cfg_profile')
    return dict(sorted(profile.items()))


def cfg_value(ts, profile, path):
    i = 0
    def parse():
        nonlocal i
        if i >= len(ts) or ts[i].kind != 'ident': raise ExtractionError('unsupported_cfg', path)
        key = ts[i].text; i += 1
        if i < len(ts) and ts[i].text == '(':
            if key not in ['all', 'any', 'not']: raise ExtractionError('unsupported_cfg_operator', path)
            i += 1; values = []
            while i < len(ts) and ts[i].text != ')':
                values.append(parse())
                if i < len(ts) and ts[i].text == ',': i += 1
                elif i >= len(ts) or ts[i].text != ')': raise ExtractionError('unsupported_cfg', path)
            if i >= len(ts): raise ExtractionError('unsupported_cfg', path)
            i += 1
            if key == 'not' and len(values) != 1: raise ExtractionError('unsupported_cfg', path)
            return all(values) if key == 'all' else any(values) if key == 'any' else not values[0]
        if key not in profile: raise ExtractionError('unknown_cfg', path)
        value = profile[key]
        if i < len(ts) and ts[i].text == '=':
            i += 1
            if i >= len(ts) or ts[i].kind != 'literal': raise ExtractionError('unsupported_cfg', path)
            try: expected = json.loads(ts[i].text)
            except ValueError: raise ExtractionError('unsupported_cfg_string', path) from None
            i += 1
            if not isinstance(expected, str) or type(value) not in [str, list]: raise ExtractionError('cfg_profile_type', path)
            return value == expected if isinstance(value, str) else expected in value
        if type(value) is not bool: raise ExtractionError('cfg_profile_type', path)
        return value
    result = parse()
    if i != len(ts): raise ExtractionError('unsupported_cfg', path)
    return result


def impl_owner(header, path, line):
    """Only one generic type/lifetime and an optional plain trait bound.
    No specialization, where clauses, const generics, defaults or multiple
    parameters are inferred. Trait paths may be qualified; owners may not.
    """
    def refuse():
        raise ExtractionError('unsupported_impl', path, line)
    def shape(part, parameter, qualified):
        if not part or part[0].kind != 'ident': refuse()
        owner = part[0]; i = 1
        while i + 2 < len(part) and part[i].text == ':' and part[i+1].text == ':' and part[i+2].kind == 'ident':
            if not qualified: refuse()
            i += 3
        if i == len(part): return owner
        if part[i].text != '<' or part[-1].text != '>': refuse()
        argument = tuple(token.text for token in part[i+1:-1])
        concrete_trait_argument = qualified and len(argument) == 1 and part[i+1].kind == 'ident'
        if argument != parameter and argument != ("'", '_') and not concrete_trait_argument: refuse()
        return owner
    if not header or any(token.text == 'where' for token in header): refuse()
    parameter = None
    if header[0].text == '<':
        close = next((i for i, token in enumerate(header[1:], 1) if token.text == '>'), None)
        if close is None: refuse()
        prefix = header[1:close]
        if not prefix or any(token.text in ('<', '>', ',', '=', 'const') for token in prefix): refuse()
        if prefix[0].text == "'":
            if len(prefix) != 2 or prefix[1].kind != 'ident': refuse()
            parameter = ("'", prefix[1].text)
        else:
            if prefix[0].kind != 'ident': refuse()
            parameter = (prefix[0].text,)
            if len(prefix) > 1:
                if len(prefix) < 3 or prefix[1].text != ':': refuse()
                # A plain bound has no argument list or additional bounds.
                if any(token.text in ('<', '>', '+', "'") for token in prefix[2:]): refuse()
                shape(prefix[2:], None, True)
        header = header[close+1:]
    separators = [i for i, token in enumerate(header) if token.text == 'for']
    if len(separators) > 1: refuse()
    if separators:
        split = separators[0]
        shape(header[:split], parameter, True)
        owner = shape(header[split+1:], parameter, False)
        return owner, True
    return shape(header, parameter, False), False


class Parser:
    def __init__(self, path, text, profile, module_paths=None):
        self.path, self.profile = path, profile
        self.module_paths = module_paths or {}
        self.path_attributes = {}
        self.explicit_modules = {}
        self.ts, self.pairs = tokens(text, path)
        # Validate cfg/expanding attributes even in ignored function bodies or inactive items.
        for i, token in enumerate(self.ts):
            if token.text == '#': self.attributes(i)

    def error(self, code, i):
        raise ExtractionError(code, self.path, self.ts[i].line if i < len(self.ts) else None)

    def attributes(self, i):
        cfg, active = [], True
        while i < len(self.ts) and self.ts[i].text == '#':
            j = i + 1
            if j < len(self.ts) and self.ts[j].text == '!': j += 1
            if j >= len(self.ts) or self.ts[j].text != '[': self.error('unsupported_attribute', i)
            end = self.pairs[j]; body = self.ts[j+1:end]
            if not body: self.error('unsupported_attribute', i)
            name = body[0].text
            if name == 'cfg':
                if self.ts[i+1].text == '!': self.error('unsupported_inner_cfg', i)
                if len(body) < 4 or body[1].text != '(' or body[-1].text != ')': self.error('unsupported_cfg', i)
                expression = body[2:-1]
                value = cfg_value(expression, self.profile, self.path)
                active = active and value; cfg.append(''.join(t.text for t in expression))
            elif name == 'path':
                if (not self.module_paths or self.ts[i+1].text == '!' or len(body) != 3 or
                        body[1].text != '=' or body[2].kind != 'literal'):
                    self.error('unsupported_attribute', i)
                try:
                    value = json.loads(body[2].text)
                except ValueError:
                    self.error('unsupported_literal_module_path', i)
                if not isinstance(value, str) or not value or '\\' in value:
                    self.error('unsupported_literal_module_path', i)
                self.path_attributes[i] = value
            elif name == 'derive':
                if len(body) < 3 or body[1].text != '(' or body[-1].text != ')' or any(t.text not in DERIVES | {','} for t in body[2:-1]):
                    self.error('unsupported_derive', i)
            elif name == 'test' and len(body) != 1: self.error('unsupported_attribute', i)
            elif name not in ATTRS: self.error('unsupported_attribute', i)
            i = end + 1
        return i, cfg, active

    def end_item(self, i):
        """Find a balanced declaration body or its semicolon, not interior lookalikes."""
        while i < len(self.ts):
            t = self.ts[i].text
            if t in ['(', '[']: i = self.pairs[i] + 1
            elif t in ['{', ';']: return i, self.pairs[i] + 1 if t == '{' else i + 1
            else: i += 1
        self.error('unterminated_item', i)

    def items(self, start, end, module, directory, emit, inherited=(), active=True, context='module'):
        i = start
        while i < end:
            if self.ts[i].text == ';': i += 1; continue
            attr_start = i
            i, cfg, enabled = self.attributes(i); cfg = tuple(inherited) + tuple(cfg); enabled = active and enabled
            if i >= end: break  # inner crate attributes may finish a file
            public = self.ts[i].text == 'pub'
            if public:
                i += 1
                if i < end and self.ts[i].text == '(': public = False; i = self.pairs[i] + 1
            while i < end and self.ts[i].text in ['unsafe', 'async']: i += 1
            if i + 1 < end and self.ts[i].text == 'const' and self.ts[i+1].text == 'fn': i += 1
            if i >= end: self.error('unsupported_item', i)
            kind = self.ts[i].text
            if kind == 'extern':
                if public or enabled or i+2 >= end or self.ts[i+1].kind != 'literal' or self.ts[i+2].text != 'fn':
                    self.error('unsupported_active_or_public_extern', i)
                stop, after = self.end_item(i+3)
                if self.ts[stop].text != '{': self.error('unsupported_extern_declaration', i)
                i = after; continue
            declared_paths = [value for at, value in self.path_attributes.items() if attr_start <= at < i]
            if declared_paths and (kind != 'mod' or len(declared_paths) != 1):
                self.error('unsupported_literal_module_path', i)
            if kind == 'use':
                stop = i + 1
                while stop < end and self.ts[stop].text != ';':
                    stop = self.pairs[stop] + 1 if self.ts[stop].text in ['(', '[', '{'] else stop + 1
                if stop >= end: self.error('unterminated_use', i)
                after = stop + 1
                if public: self.error('glob_reexport' if any(t.text == '*' for t in self.ts[i:stop]) else 'unsupported_reexport', i)
                if self.ts[stop].text != ';': self.error('unsupported_use', i)
                i = after; continue
            if kind == 'impl':
                stop, after = self.end_item(i+1); header = self.ts[i+1:stop]
                if self.ts[stop].text != '{': self.error('unsupported_impl', i)
                owner_token, trait = impl_owner(header, self.path, self.ts[i].line)
                owner = module + (owner_token.text,)
                yield from self.items(stop+1, after-1, owner, directory, emit, cfg, enabled, 'trait_impl' if trait else 'impl')
                i = after; continue
            if kind == 'static' and not public:
                if i+1 >= end or self.ts[i+1].kind != 'ident' or self.ts[i+1].text == 'mut':
                    self.error('unsupported_private_static', i)
                stop, after = self.end_item(i+2)
                if self.ts[stop].text != ';': self.error('unsupported_private_static', i)
                i = after; continue
            if kind not in KINDS: self.error('unsupported_item', i)
            if i + 1 >= end or self.ts[i+1].kind != 'ident': self.error('unsupported_item_name', i)
            name = self.ts[i+1].text; name_token = self.ts[i+1]
            if public and context == 'trait_impl': self.error('unsupported_public_trait_impl', i)
            if (public or context == 'public_trait') and enabled:
                emit(self.path, module + (name,), name, name_token.line, list(cfg))
            stop, after = self.end_item(i+2)
            if kind == 'mod':
                if context != 'module': self.error('unsupported_nested_mod', i)
                if self.ts[stop].text == ';':
                    if declared_paths:
                        target = posixpath.normpath(str(PurePosixPath(self.path).parent / declared_paths[0]))
                        if not safe_path(target) or self.module_paths.get(self.path + '::' + name) != target:
                            self.error('literal_module_path_not_explicit', i)
                        if name in self.explicit_modules:
                            self.error('ambiguous_module_owner', i)
                        self.explicit_modules[name] = target
                    yield (name, module + (name,), directory / name, cfg, enabled)
                else:
                    yield from self.items(stop+1, after-1, module+(name,), directory/name, emit, cfg, enabled)
            elif kind == 'trait' and self.ts[stop].text == '{':
                yield from self.items(stop+1, after-1, module+(name,), directory, emit, cfg, enabled, 'public_trait' if public else 'trait')
            # const/type initializers can contain balanced blocks, then require semicolon.
            elif kind in ['const', 'type'] and self.ts[stop].text == '{':
                if after >= end or self.ts[after].text != ';': self.error('unsupported_initializer', stop)
                after += 1
            i = after


def safe_path(path):
    return (isinstance(path, str) and path and '\\' not in path and '\0' not in path and
            not path.startswith('/') and ':' not in path and
            all(p not in ['', '.', '..'] for p in path.split('/')) and str(PurePosixPath(path)) == path)


def extract(root, paths, inventory, profile=None, *, entry=ENTRY, crate=CRATE, module_paths=None, include_modules=False):
    try: root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError): raise ExtractionError('invalid_explicit_root') from None
    profile = profile_map(profile)
    if not safe_path(entry) or not IDENT.fullmatch(crate): raise ExtractionError('invalid_explicit_root')
    module_paths = {} if module_paths is None else module_paths
    if not isinstance(module_paths, dict) or any(not isinstance(k, str) or not safe_path(v) for k,v in module_paths.items()):
        raise ExtractionError('invalid_explicit_module_paths')
    if (not isinstance(paths, list) or not paths or any(not safe_path(p) or not p.endswith('.rs') for p in paths) or
            paths != sorted(set(paths)) or entry not in paths): raise ExtractionError('invalid_explicit_list')
    if not isinstance(inventory, list) or any(not safe_path(p) for p in inventory) or len(set(inventory)) != len(inventory):
        raise ExtractionError('invalid_inventory')
    if not set(paths) <= set(inventory): raise ExtractionError('source_not_in_inventory')
    parsers, files = {}, {}
    for path in paths:
        lexical = root / path
        try: resolved = lexical.resolve(strict=True)
        except (OSError, RuntimeError): raise ExtractionError('unsafe_or_missing_source', path) from None
        if (not resolved.is_relative_to(root) or not resolved.is_file() or
                any(part.is_symlink() for part in [lexical, *lexical.parents] if part != root.parent)):
            raise ExtractionError('unsafe_or_missing_source', path)
    for path in paths:
        try: data = (root / path).read_bytes()
        except OSError: raise ExtractionError('source_unreadable', path) from None
        digest = hashlib.sha256(data).hexdigest()
        try: text = data.decode('utf-8', errors='strict')
        except UnicodeError: raise ExtractionError('invalid_utf8', path) from None
        parsers[path] = Parser(path, text, profile, module_paths)
        files[path] = {'path': path, 'source_sha256': digest, 'symbols': []}
    seen, identities = set(), set()
    def emit(path, qualified, name, line, cfg):
        symbol = '::'.join(qualified)
        if symbol in identities: raise ExtractionError('duplicate_symbol', path, line)
        identities.add(symbol)
        files[path]['symbols'].append({'path': path, 'symbol': symbol, 'name': name, 'line': line, 'cfg': cfg})
    def walk(path, module, directory, inherited=(), active=True):
        if path in seen: raise ExtractionError('ambiguous_module_owner', path)
        seen.add(path); parser = parsers[path]
        if include_modules: files[path]['module'] = '::'.join(module)
        for name, qualified, base, cfg, enabled in parser.items(0, len(parser.ts), module, directory, emit, inherited, active):
            override = parser.explicit_modules.get(name)
            choices = [override] if override else [str(base)+'.rs', str(base/'mod.rs')]
            selected = [p for p in choices if p in parsers]
            if len(selected) != 1: raise ExtractionError('module_not_explicit_or_ambiguous', path)
            walk(selected[0], qualified, PurePosixPath(override).with_suffix('') if override else base, cfg, enabled)
    walk(entry, (crate,), PurePosixPath(entry).parent)
    consumed = {p + '::' + name: target for p,parser in parsers.items() for name,target in parser.explicit_modules.items()}
    if consumed != module_paths: raise ExtractionError('unused_or_missing_explicit_module_path')
    if seen != set(paths): raise ExtractionError('unresolved_explicit_source')
    for file in files.values(): file['symbols'].sort(key=lambda v: (v['line'], v['symbol']))
    return {'schema': 'cafe.rust-surface.v1', 'crate': crate, 'cfg_profile': profile,
            'files': [files[p] for p in paths]}


def read_inline_inventory(root):
    """Explicit clean-export metadata; never discover or trust a fallback tree."""
    value = os.environ.get('CAFE_PREPARED_INVENTORY_JSON')
    if value is None:
        return None
    try:
        names = json.loads(value)
        if not isinstance(names, list) or not names or any(not isinstance(p, str) for p in names) or names != sorted(set(names)):
            raise ValueError('invalid_prepared_inventory')
        root = Path(root).resolve()
        for name in names:
            parts = name.split('/')
            if not name or name.startswith('/') or '\\' in name or ':' in name or '\0' in name or any(p in ['', '.', '..'] for p in parts) or parts[0] in ['.git', 'local', 'node_modules', 'target', '_scratch', '_target']:
                raise ValueError('invalid_prepared_inventory')
            path = root / name
            if path.resolve() != path or not path.is_file():
                raise ValueError('invalid_prepared_inventory')
        return names
    except (ValueError, TypeError, OSError):
        raise ExtractionError('invalid_prepared_inventory') from None


def public_inventory(root, snapshot):
    if not (root / '.git').exists():
        if snapshot is None:
            inline = read_inline_inventory(root)
            if inline is not None: return inline
            raise ExtractionError('version_inventory_unavailable')
        return json.loads(Path(snapshot).read_text(encoding='utf-8'))
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                             '--exclude-standard', '-z'], capture_output=True, timeout=30)
    if result.returncode: raise ExtractionError('version_inventory_unavailable')
    actual = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    if snapshot is not None and json.loads(Path(snapshot).read_text(encoding='utf-8')) != actual:
        raise ExtractionError('stale_inventory_snapshot')
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', nargs='+', required=True)
    parser.add_argument('--inventory', help='explicit public inventory for no-Git verification copies')
    parser.add_argument('--cfg-profile', help='explicit JSON map; no host target inference')
    parser.add_argument('--module-paths', help='explicit JSON module override map; no path inference')
    args = parser.parse_args(argv)
    try:
        profile = json.loads(Path(args.cfg_profile).read_text(encoding='utf-8')) if args.cfg_profile else None
        module_paths = json.loads(Path(args.module_paths).read_text(encoding='utf-8')) if args.module_paths else {}
        if not isinstance(module_paths, dict): raise ExtractionError('invalid_explicit_module_paths')
        report = extract(ROOT, args.files, public_inventory(ROOT, args.inventory), profile, module_paths=module_paths)
    except ExtractionError as error:
        print(json.dumps({'error': error.code, 'path': error.path, 'line': error.line}, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('{"error":"input_evidence_unavailable"}', file=sys.stderr); return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
