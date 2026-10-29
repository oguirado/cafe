"""Explicit-source JS inventory using the pinned compiler AST and a bounded export profile."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROCESSES = {'main', 'preload', 'renderer', 'contracts'}
PARSER_VERSION = '5.9.3'


class ExtractionError(ValueError):
    def __init__(self, code, path='', line=None):
        self.code, self.path, self.line = code, path, line
        super().__init__(code)


# Source is parsed, never evaluated. No Program, import resolver or directory walk.
# Attached jsDoc arrays are a version-pinned compiler representation; the public
# getJSDocTags helper alone omits earlier consecutive typedef blocks.
AST_DRIVER = r'''
'use strict';
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let ts;
const fail = (code, unit, node) => {
  throw {code, path:unit ? unit.path : '', line:unit && node ? unit.sf.getLineAndCharacterOfPosition(node.getStart(unit.sf)).line+1 : null};
};
try {
  ts = require(input.compiler);
  if (ts.version !== '5.9.3') fail('unsupported_parser_version');
  const units = new Map();
  const identifier = n => n && ts.isIdentifier(n) && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(n.text);
  const line = (u,n) => u.sf.getLineAndCharacterOfPosition(n.getStart(u.sf)).line+1;
  const modifiers = n => ts.canHaveModifiers(n) ? ts.getModifiers(n)||[] : [];
  const has = (n,kind) => modifiers(n).some(m=>m.kind===kind);
  const unwrap = n => {while (n && ts.isParenthesizedExpression(n)) n=n.expression;return n;};
  const isFunction = n => n && (ts.isFunctionDeclaration(n)||ts.isFunctionExpression(n)||ts.isArrowFunction(n)||ts.isMethodDeclaration(n)||ts.isGetAccessorDeclaration(n)||ts.isSetAccessorDeclaration(n)||ts.isConstructorDeclaration(n));
  const isClass = n => n && (ts.isClassDeclaration(n)||ts.isClassExpression(n));
  const access = n => {
    if (!n) return null;
    if (identifier(n)) return [n.text];
    if (ts.isPropertyAccessExpression(n)) {const base=access(n.expression);return base ? [...base,n.name.text] : null;}
    if (ts.isElementAccessExpression(n)) {const base=access(n.expression);return base ? [...base,null] : null;}
    return null;
  };
  function objectLiteral(u,expr) {
    expr=unwrap(expr);
    if (expr && ts.isCallExpression(expr)) {
      const a=access(expr.expression);
      if (a && a.join('.')==='Object.freeze') {
        u.freezeUsed=true;
        if (u.objectBound||u.freezeMutated||expr.arguments.length!==1) fail('ambiguous_freeze',u,expr);
        expr=unwrap(expr.arguments[0]);
      }
    }
    return expr && ts.isObjectLiteralExpression(expr) ? expr : null;
  }
  function bindings(u,statements) {
    const out=new Map();
    const bind=(name,node,value)=>{if(!identifier(name)){if(name&&(ts.isObjectBindingPattern(name)||ts.isArrayBindingPattern(name)))for(const e of name.elements)if(e.name)bind(e.name,node,null);return;} if(out.has(name.text)) fail('ambiguous_binding',u,name);out.set(name.text,{node,value});};
    for(const s of statements) {
      if ((ts.isFunctionDeclaration(s)||ts.isClassDeclaration(s)) && s.name) bind(s.name,s,s);
      if(ts.isVariableStatement(s)) for(const d of s.declarationList.declarations) bind(d.name,d,unwrap(d.initializer));
      if(ts.isImportDeclaration(s) && s.importClause) {
        const clause=s.importClause;if(clause.name) bind(clause.name,clause,null);
        if(clause.namedBindings) {
          if(ts.isNamespaceImport(clause.namedBindings)) bind(clause.namedBindings.name,clause,null);
          else for(const e of clause.namedBindings.elements) bind(e.name,e,null);
        }
      }
    }
    return out;
  }
  // Conservative binding census across executed compounds. Deferred bodies do
  // not bind the surrounding scope; declaration names still do. Block locals
  // are deliberately treated as ambiguous even outside their lexical reach.
  function bindsObject(root) {
    let found=false;
    const name=n=>{if(identifier(n)){if(n.text==='Object')found=true;}
      else if(n&&(ts.isObjectBindingPattern(n)||ts.isArrayBindingPattern(n)))
        for(const e of n.elements)if(e.name)name(e.name);};
    function visit(n) {
      if(isFunction(n)||isClass(n)){if(ts.isFunctionDeclaration(n)||ts.isClassDeclaration(n))name(n.name);return;}
      if(ts.isVariableDeclaration(n)||ts.isParameter(n)||ts.isBindingElement(n))name(n.name);
      if(ts.isImportClause(n)||ts.isNamespaceImport(n)||ts.isImportSpecifier(n))name(n.name);
      ts.forEachChild(n,visit);
    }
    visit(root);return found;
  }
  function properties(u,obj,scope,facade=false) {
    const found=[],seen=new Set();
    for(const p of obj.properties) {
      if (!p.name || !identifier(p.name) || ts.isSpreadAssignment(p) || ts.isGetAccessorDeclaration(p)||ts.isSetAccessorDeclaration(p)) fail('unsupported_object_export',u,p);
      const name=p.name.text;if(name==='__proto__'&&ts.isPropertyAssignment(p))fail('unsupported_object_export',u,p);if(seen.has(name)) fail('duplicate_export',u,p);seen.add(name);
      let value=null,site=p.name;
      if(ts.isShorthandPropertyAssignment(p)) {
        const b=scope.get(name);if(!b || !b.value || p.objectAssignmentInitializer) fail('unresolved_export_binding',u,p);
        value=b.value;if(facade&&isFunction(value))site=b.node;
      } else if(ts.isPropertyAssignment(p)) {
        value=unwrap(p.initializer);
        if(identifier(value)) {const b=scope.get(value.text);if(!b || !b.value) fail('unresolved_export_binding',u,p);value=b.value;}
      } else if(ts.isMethodDeclaration(p)) value=p;
      else fail('unsupported_object_export',u,p);
      found.push({name,value,site});
    }
    return found;
  }
  function add(u,name,node,value=null,ref=null) {
    if(u.exports.has(name)) fail('duplicate_export',u,node);
    u.exports.set(name,{name,node,value,ref});
  }
  for(const file of input.files) {
    const sf=ts.createSourceFile(file.path,file.text,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
    const u={path:file.path,sf,exports:new Map(),types:[],allowed:new Set(),esm:false,cjs:false,whole:-1,alias:false};
    units.set(file.path,u);
    if(sf.parseDiagnostics.length) {
      const d=sf.parseDiagnostics[0];throw {code:'unsupported_or_invalid_syntax',path:file.path,line:sf.getLineAndCharacterOfPosition(d.start||0).line+1};
    }
    u.bindings=bindings(u,sf.statements);
    u.objectBound=bindsObject(sf);
    if(u.bindings.has('module')||u.bindings.has('exports')) fail('ambiguous_commonjs_binding',u);
    const typeNames=new Set();
    for(const statement of sf.statements) {
      for(const doc of statement.jsDoc||[]) {
        const tags=doc.tags||[];if(tags.some(t=>['private','internal'].includes(t.tagName.text)))continue;
        for(const tag of tags) if(ts.isJSDocTypedefTag(tag)) {
          if(!identifier(tag.name)||!identifier(tag.fullName))fail('unsupported_public_typedef',u,tag);
          if(typeNames.has(tag.name.text))fail('duplicate_public_typedef',u,tag);typeNames.add(tag.name.text);
          u.types.push({name:tag.name.text,node:tag});
        }
      }
      if(ts.isExportDeclaration(statement)) {
        u.esm=true;
        if(statement.isTypeOnly)fail('unsupported_typescript_syntax',u,statement);
        if(!statement.exportClause||!ts.isNamedExports(statement.exportClause))fail('export_star_or_namespace_unsupported',u,statement);
        for(const element of statement.exportClause.elements) {
          if(element.isTypeOnly||!identifier(element.name)||element.propertyName&&!identifier(element.propertyName))fail('unsupported_export_name',u,element);
          const local=(element.propertyName||element.name).text;
          if(statement.moduleSpecifier) {
            if(!ts.isStringLiteral(statement.moduleSpecifier))fail('ambiguous_reexport',u,statement);
            add(u,element.name.text,element.name,null,{specifier:statement.moduleSpecifier.text,name:local});
          } else {
            const b=u.bindings.get(local);if(!b||!b.value)fail('unresolved_export_binding',u,element);
            add(u,element.name.text,element.name,b.value);
          }
        }
      } else if(ts.isExportAssignment(statement)) {
        u.esm=true;if(statement.isExportEquals)fail('unsupported_typescript_syntax',u,statement);
        let value=unwrap(statement.expression);if(identifier(value)){const b=u.bindings.get(value.text);if(!b||!b.value)fail('unresolved_export_binding',u,statement);value=b.value;}
        add(u,'default',statement,value);
      } else if(has(statement,ts.SyntaxKind.ExportKeyword)) {
        u.esm=true;
        if(ts.isFunctionDeclaration(statement)||ts.isClassDeclaration(statement)) {
          const isDefault=has(statement,ts.SyntaxKind.DefaultKeyword);
          if(!isDefault&&!identifier(statement.name))fail('unsupported_export_declaration',u,statement);
          add(u,isDefault?'default':statement.name.text,statement,statement);
        } else if(ts.isVariableStatement(statement)) {
          for(const d of statement.declarationList.declarations) {
            if(!identifier(d.name))fail('unsupported_export_destructuring',u,d);
            add(u,d.name.text,d.name,unwrap(d.initializer));
          }
        } else fail('unsupported_export_declaration',u,statement);
      }
      if(ts.isExpressionStatement(statement)&&ts.isBinaryExpression(statement.expression)) {
        const e=statement.expression,a=access(e.left);
        if(a && (a[0]==='exports'||a[0]==='module'&&a[1]==='exports')) {
          u.cjs=true;if(e.operatorToken.kind!==ts.SyntaxKind.EqualsToken||a.includes(null))fail('computed_or_dynamic_commonjs_export',u,e.left);
          u.allowed.add(e.left);
          if(a.join('.')==='module.exports') {
            if(u.whole!==-1 || u.exports.size)fail('ambiguous_commonjs_reassignment',u,e);
            u.whole=statement.pos;const obj=objectLiteral(u,e.right);
            if(obj)for(const p of properties(u,obj,u.bindings))add(u,p.name,p.site,p.value);
            else {
              let value=unwrap(e.right);if(identifier(value))value=u.bindings.get(value.text)?.value;
              if(!isFunction(value)&&!isClass(value))fail('dynamic_commonjs_surface',u,e.right);
              add(u,'default',e.left,value);
            }
          } else if(a[0]==='exports'&&a.length===2 || a[0]==='module'&&a.length===3) {
            if(a[0]==='exports')u.alias=true;
            let value=unwrap(e.right);if(identifier(value))value=u.bindings.get(value.text)?.value;
            add(u,a[a.length-1],e.left.name,value);
          } else fail('unsupported_commonjs_export',u,e.left);
        }
      }
    }
    if(u.esm&&u.cjs)fail('mixed_module_systems',u);
    if(u.whole!==-1&&u.alias)fail('ambiguous_commonjs_alias',u);
    const forbidden=new Set(['InterfaceDeclaration','TypeAliasDeclaration','EnumDeclaration','ModuleDeclaration','ImportEqualsDeclaration','AsExpression','SatisfiesExpression','TypeAssertionExpression','NonNullExpression','Decorator','JsxElement','JsxSelfClosingElement','JsxFragment']);
    function validate(n) {
      if((ts.isExportDeclaration(n)||ts.isExportAssignment(n)||has(n,ts.SyntaxKind.ExportKeyword))&&n.parent!==sf)fail('unsupported_nested_export',u,n);
      if(forbidden.has(ts.SyntaxKind[n.kind]) || n.typeParameters?.length || n.type && !ts.isJSDoc(n))fail('unsupported_typescript_or_jsx_syntax',u,n);
      const a=access(n);
      const namePosition=identifier(n)&&n.parent&&(
        ts.isPropertyAccessExpression(n.parent)&&n.parent.name===n ||
        ts.isPropertyAssignment(n.parent)&&n.parent.name===n ||
        ts.isMethodDeclaration(n.parent)&&n.parent.name===n);
      if(ts.isBinaryExpression(n)&&n.operatorToken.kind>=ts.SyntaxKind.FirstAssignment&&n.operatorToken.kind<=ts.SyntaxKind.LastAssignment) {
        const target=access(n.left);if(target&&target[0]==='Object')u.freezeMutated=true;
      }
      if(ts.isDeleteExpression(n)||ts.isPrefixUnaryExpression(n)||ts.isPostfixUnaryExpression(n)) {
        const target=access(n.expression||n.operand);
        if(target&&target[0]==='Object'&&(ts.isDeleteExpression(n)||n.operator===ts.SyntaxKind.PlusPlusToken||n.operator===ts.SyntaxKind.MinusMinusToken))u.freezeMutated=true;
      }
      if(ts.isCallExpression(n)) {
        const target=access(n.expression), first=access(n.arguments[0]);
        if(target&&first&&first.join('.')==='Object'&&['Object.assign','Object.defineProperty','Object.defineProperties','Object.setPrototypeOf','Reflect.set','Reflect.defineProperty','Reflect.deleteProperty','Reflect.setPrototypeOf'].includes(target.join('.')))u.freezeMutated=true;
      }
      if((ts.isCallExpression(n)||ts.isNewExpression(n))&&identifier(n.expression)&&['eval','Function'].includes(n.expression.text))fail('dynamic_code_unsupported',u,n);
      if(n.kind===ts.SyntaxKind.ThisKeyword) {
        let owner=n.parent;while(owner&&!(isFunction(owner)&&!ts.isArrowFunction(owner))&&!isClass(owner))owner=owner.parent;
        if(!owner)fail('implicit_commonjs_surface_unsupported',u,n);
      }
      if(a && !namePosition && (a[0]==='exports'||a[0]==='module')) {
        if(![...u.allowed].some(lhs=>{for(let p=n;p;p=p.parent){if(p===lhs)return true;if(p===sf)break;}return false;}))fail(a.includes(null)?'computed_commonjs_export':'commonjs_alias_or_nested_export',u,n);
      }
      if(!sf.statements.includes(n))for(const doc of n.jsDoc||[])if((doc.tags||[]).some(t=>ts.isJSDocTypedefTag(t))&&(doc.tags||[]).some(t=>t.tagName.text==='public'))fail('unsupported_nested_public_typedef',u,n);
      ts.forEachChild(n,validate);
    }
    validate(sf);
    if(u.freezeUsed&&u.freezeMutated)fail('ambiguous_freeze',u);
  }
  function resolve(u,entry,seen=new Set()) {
    const key=u.path+'\0'+entry.name;if(seen.has(key))fail('cyclic_reexport',u,entry.node);seen.add(key);
    if(!entry.ref)return entry.value;
    const spec=entry.ref.specifier;
    if(!(spec.startsWith('./')||spec.startsWith('../'))||spec.includes('\\')||!spec.endsWith('.js'))fail('ambiguous_reexport',u,entry.node);
    const target=require('node:path').posix.normalize(require('node:path').posix.join(require('node:path').posix.dirname(u.path),spec));
    const other=units.get(target);if(!other||!other.exports.has(entry.ref.name))fail('unlisted_or_unresolved_reexport',u,entry.node);
    // A re-export retains its own process boundary and export-site coordinate.
    resolve(other,other.exports.get(entry.ref.name),seen);return null;
  }
  const output=[];
  for(const u of units.values()) {
    const process=u.path.split('/')[1],base='cafe_shell::'+u.path.slice(4,-3).split('/').join('::');
    const rows=[],symbols=new Set();
    const emit=(name,suffix,node)=>{const symbol=base+'::'+suffix;if(symbols.has(symbol))fail('duplicate_symbol',u,node);symbols.add(symbol);rows.push({path:u.path,symbol,name,line:line(u,node),process});};
    for(const item of u.types)emit(item.name,'typedef::'+item.name,item.node);
    for(const item of u.exports.values()) {
      const value=resolve(u,item);emit(item.name,item.name,item.node);
      if(!isFunction(value)||!value.body)continue;
      const body=value.body;
      let scope=ts.isBlock(body)?bindings(u,body.statements):new Map();
      const returns=[];
      function collect(n){if(n!==body&&(isFunction(n)||isClass(n)))return;if(ts.isReturnStatement(n))returns.push({expr:n.expression,node:n,direct:n.parent===body});ts.forEachChild(n,collect);}
      if(ts.isBlock(body))collect(body);else returns.push({expr:body,direct:true});
      const objects=returns.map(e=>objectLiteral(u,e.expr));
      if(!objects.some(Boolean))continue;
      if(value.asteriskToken||has(value,ts.SyntaxKind.AsyncKeyword))fail('unsupported_async_or_generator_facade',u,value);
      if(objects.length!==1||!objects[0])fail('ambiguous_factory_facade',u,value);
      if(!returns[0].direct) {
        const block=returns[0].node?.parent, attempt=block?.parent;
        if(!block||!ts.isBlock(block)||!attempt||!ts.isTryStatement(attempt)||attempt.tryBlock!==block||attempt.parent!==body||
           !attempt.catchClause||attempt.catchClause.block.statements.length!==1||!ts.isThrowStatement(attempt.catchClause.block.statements[0]))fail('ambiguous_factory_facade',u,value);
        const local=bindings(u,block.statements);
        for(const [name,binding] of local){if(scope.has(name))fail('ambiguous_binding',u,binding.node);scope.set(name,binding);}
      }
      function parameterHasObject(n){if(identifier(n))return n.text==='Object';return !!n?.elements?.some(e=>e.name&&parameterHasObject(e.name));}
      if(bindsObject(body)||parameterHasObject(value.name)||value.parameters.some(p=>parameterHasObject(p.name)))fail('ambiguous_freeze',u,value);
      for(const p of properties(u,objects[0],scope,true))emit(p.name,item.name+'::'+p.name,p.site);
    }
    output.push({path:u.path,symbols:rows});
  }
  process.stdout.write(JSON.stringify(output));
} catch(error) {
  const safe=error && typeof error.code==='string' ? error : {code:'parser_evidence_unavailable',path:'',line:null};
  process.stderr.write(JSON.stringify(safe));process.exitCode=2;
}
'''


def _safe_path(path):
    return (isinstance(path, str) and re.fullmatch(r'[A-Za-z0-9_$./-]+', path) is not None
            and str(PurePosixPath(path)) == path and not path.startswith('/')
            and all(p not in ('', '.', '..') for p in path.split('/')))


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


def public_inventory(root, snapshot=None):
    if not (root / '.git').exists():
        if snapshot is None:
            inline = read_inline_inventory(root)
            if inline is not None: return inline
            raise ExtractionError('version_inventory_unavailable')
        return json.loads(Path(snapshot).read_text(encoding='utf-8'))
    x = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                        '--exclude-standard', '-z'], capture_output=True, timeout=30)
    if x.returncode: raise ExtractionError('version_inventory_unavailable')
    actual = sorted(set(x.stdout.decode('utf-8').split('\0')) - {''})
    if snapshot is not None and json.loads(Path(snapshot).read_text()) != actual:
        raise ExtractionError('stale_inventory_snapshot')
    return actual


def extract(root, paths, inventory):
    """Hash/decode only explicit sources; parse in one non-evaluating AST process."""
    root = Path(root).resolve()
    if (not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths)
            or paths != sorted(set(paths))): raise ExtractionError('unordered_or_empty_sources')
    if not isinstance(inventory, list) or not inventory or any(not _safe_path(p) for p in inventory):
        raise ExtractionError('version_inventory_unavailable')
    for path in paths:
        if not _safe_path(path) or not path.endswith('.js'):
            raise ExtractionError('unsafe_source_path')
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != 'src' or parts[1] not in PROCESSES:
            raise ExtractionError('unknown_process_boundary', path)
        if path not in inventory: raise ExtractionError('source_not_versioned', path)
        lexical = root / path
        try: valid = lexical.resolve() == lexical and lexical.is_file()
        except (OSError, RuntimeError): valid = False
        if not valid: raise ExtractionError('source_missing_or_aliased', path)
    files = []
    for path in paths:
        data = (root / path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        try: text = data.decode('utf-8', errors='strict')
        except UnicodeError: raise ExtractionError('invalid_utf8', path) from None
        files.append({'path': path, 'source_sha256': digest, 'text': text})
    node = os.environ.get('ROUTER_NODE') or shutil.which('node')
    if not node: raise ExtractionError('node24_unavailable')
    compiler = ROOT / 'node_modules/typescript/lib/typescript.js'
    if not compiler.is_file(): raise ExtractionError('parser_unavailable')
    env = dict(os.environ)
    for key in ['NODE_OPTIONS', 'NODE_PATH']: env.pop(key, None)
    try:
        version = subprocess.run([node, '--version'], capture_output=True, text=True, env=env, timeout=30)
        if version.returncode or not re.fullmatch(r'v24\.\d+\.\d+\s*', version.stdout):
            raise ExtractionError('unsupported_node_runtime')
        payload = json.dumps({'compiler': str(compiler), 'files': [{'path': f['path'], 'text': f['text']} for f in files]})
        result = subprocess.run([node, '-e', AST_DRIVER], input=payload, capture_output=True,
                                text=True, env=env, cwd=root, timeout=120)
    except (OSError, subprocess.TimeoutExpired): raise ExtractionError('parser_evidence_unavailable') from None
    if result.returncode:
        try:
            error = json.loads(result.stderr)
            code, path, line = error['code'], error.get('path', ''), error.get('line')
            if not isinstance(code, str) or not re.fullmatch(r'[a-z_]+', code) or path not in paths + ['']:
                raise ValueError()
        except (ValueError, KeyError, TypeError): raise ExtractionError('parser_evidence_unavailable') from None
        raise ExtractionError(code, path, line)
    try: parsed = json.loads(result.stdout)
    except ValueError: raise ExtractionError('parser_evidence_unavailable') from None
    if not isinstance(parsed, list) or [f.get('path') for f in parsed] != paths:
        raise ExtractionError('parser_evidence_unavailable')
    output = []
    for source, item in zip(files, parsed):
        rows = item['symbols']
        if any(set(v) != {'path','symbol','name','line','process'} or v['path'] != source['path']
               or v['process'] != source['path'].split('/')[1] or type(v['line']) is not int
               or not 1 <= v['line'] <= len(re.split(r'\r\n|\r|\n|\u2028|\u2029', source['text'])) for v in rows):
            raise ExtractionError('invalid_parser_coordinates')
        output.append({'path': source['path'], 'source_sha256': source['source_sha256'],
                       'symbols': sorted(rows, key=lambda v: (v['line'], v['symbol']))})
    return {'schema': 'cafe.js-surface.v1', 'parser': {'name': 'typescript', 'version': PARSER_VERSION}, 'files': output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', nargs='+', required=True)
    parser.add_argument('--inventory', help='explicit public-file snapshot for no-Git verification copies')
    args = parser.parse_args(argv)
    try: report = extract(ROOT, args.files, public_inventory(ROOT, args.inventory))
    except ExtractionError as error:
        print(json.dumps({'error': error.code, 'path': error.path, 'line': error.line}, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('{"error":"input_evidence_unavailable"}', file=sys.stderr); return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
