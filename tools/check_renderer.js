// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { parse, compileScript } = require('@vue/compiler-sfc');

/** One spelling per file: TypeScript hands the compilerHost slash-separated names
 * (C:/x/y.js on Windows) while path.join builds native ones (C:\x\y.js), so every
 * compilerHost lookup and comparison goes through this. Keeps a leading // (UNC share).
 * @param {string} name */
function fileIdentity(name) {
  const slashed = String(name).replace(/\\/g, '/');
  const normal = (slashed.startsWith('//') ? '/' : '') + path.posix.normalize(slashed);
  return normal.length > 1 && normal.endsWith('/') && !/^[A-Za-z]:\/$/.test(normal) ? normal.slice(0, -1) : normal;
}

const BOUNDARY_FILES = ['ui/src/cafe.css', 'ui/src/design/tokens.cafe.css'];
const BOUNDARY_DIR = 'ui/src/kit/';

/** Build the checked program inputs. Exported so tests can drive the compilerHost with the
 * other separator spelling on any platform.
 * @param {string} root
 * @param {string} content */
function createRendererHost(root, content) {
  const rootId = fileIdentity(path.resolve(root));
  const entry = fileIdentity(path.join(rootId, 'ui/src/main.js'));
  const virtualApp = fileIdentity(path.join(rootId, 'ui/src/__cafe_app_check.js'));
  const options = { allowJs: true, checkJs: true, noEmit: true, skipLibCheck: true,
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler, types: [], strict: false };
  const compilerHost = ts.createCompilerHost(options);
  /** @param {string} name */
  const key = name => compilerHost.getCanonicalFileName(fileIdentity(name));
  /** @type {Map<string, string>} */
  const virtual = new Map([[key(virtualApp), content]]);
  // fileIdentity drops trailing slashes, so append after canonicalising: /a must not prefix /ab.
  const rootKey = key(rootId), rootPrefix = rootKey.endsWith('/') ? rootKey : rootKey + '/';
  const allowedFiles = new Set(BOUNDARY_FILES.map(rel => compilerHost.getCanonicalFileName(rel)));
  const allowedDir = compilerHost.getCanonicalFileName(BOUNDARY_DIR);
  const entryKey = key(entry);
  const read = compilerHost.readFile.bind(compilerHost), exists = compilerHost.fileExists.bind(compilerHost), source = compilerHost.getSourceFile.bind(compilerHost);
  compilerHost.fileExists = name => virtual.has(key(name)) || exists(name);
  compilerHost.readFile = name => virtual.get(key(name)) ?? read(name);
  compilerHost.getSourceFile = (name, language, onError, create) => virtual.has(key(name))
    ? ts.createSourceFile(name, virtual.get(key(name)) || '', typeof language === 'object' ? language.languageVersion : language, true, ts.ScriptKind.JS)
    : source(name, language, onError, create);
  compilerHost.resolveModuleNames = (names, containing) => names.map(name => {
    if (name === './App.vue' && key(containing) === entryKey) return { resolvedFileName: virtualApp, extension: ts.Extension.Js };
    if (name.endsWith('.vue') || name.endsWith('.css')) {
      // Backslash specifiers mean different files on POSIX and Windows: refuse both.
      if (name.includes('\\')) return undefined;
      const dependency = fileIdentity(path.posix.join(path.posix.dirname(fileIdentity(containing)), name));
      const dependencyKey = key(dependency);
      if (!dependencyKey.startsWith(rootPrefix)) return undefined;
      const rel = dependencyKey.slice(rootPrefix.length);
      const allowed = rel.startsWith(allowedDir) || allowedFiles.has(rel);
      if (!allowed || !fs.statSync(dependency, { throwIfNoEntry: false })?.isFile()) return undefined;
      const stub = dependency + '.boundary.js';
      virtual.set(key(stub), name.endsWith('.vue')
        ? "/** @type {import('vue').Component} */ const component = {}; export default component;"
        : 'export {};');
      return { resolvedFileName: stub, extension: ts.Extension.Js };
    }
    return ts.resolveModuleName(name, containing, options, compilerHost).resolvedModule;
  });
  /** @param {string} name */
  const relativeToRoot = name => key(name).startsWith(rootPrefix)
    ? fileIdentity(name).slice(rootPrefix.length)
    : path.relative(rootId, name).split(path.sep).join('/');
  return { compilerHost, options, entry, virtualApp, relativeToRoot };
}

/** Check authored JavaScript, including script-setup, without authored TS files.
 * Copied Vue components are dependency boundaries, not silently rewritten.
 * Template syntax is checked separately by the actual Vite/Vue build.
 * @param {string} [root] */
function checkRenderer(root = path.resolve(__dirname, '..')) {
  const app = path.join(root, 'ui/src/App.vue');
  const parsed = parse(fs.readFileSync(app, 'utf8'), { filename: app });
  if (parsed.errors.length || parsed.descriptor.script?.lang || parsed.descriptor.scriptSetup?.lang) throw new Error('AUTHORED_JAVASCRIPT_REQUIRED');
  compileScript(parsed.descriptor, { id: 'cafe-check' });
  if (parsed.descriptor.script && parsed.descriptor.scriptSetup) throw new Error('MIXED_SCRIPT_CHECK_UNSUPPORTED');
  // Check the original JS: compiler hoisting can attach its JSDoc to generated exports.
  const script = parsed.descriptor.scriptSetup || parsed.descriptor.script;
  if (!script) throw new Error('AUTHORED_SCRIPT_ABSENT');
  const content = script.content + (parsed.descriptor.scriptSetup ? '\nconst __cafeComponent = {}; export default __cafeComponent;\n' : '');
  const { compilerHost, options, entry, virtualApp, relativeToRoot } = createRendererHost(root, content);
  const program = ts.createProgram([entry, virtualApp], options, compilerHost);
  const diagnostics = ts.getPreEmitDiagnostics(program).map(d => ({ code: d.code,
    file: d.file ? relativeToRoot(d.file.fileName) : null,
    message: ts.flattenDiagnosticMessageText(d.messageText, '\n') }));
  return { schema: 'cafe.renderer-check.v1', status: diagnostics.length ? 'fail' : 'pass', authoredFiles: 2, diagnostics };
}
module.exports = { checkRenderer, createRendererHost, fileIdentity };
if (require.main === module) {
  try { const report = checkRenderer(); console.log(JSON.stringify(report)); process.exitCode = report.status === 'pass' ? 0 : 1; }
  catch { console.log(JSON.stringify({ schema: 'cafe.renderer-check.v1', status: 'fail', error: 'RENDERER_CHECK_UNAVAILABLE' })); process.exitCode = 1; }
}
