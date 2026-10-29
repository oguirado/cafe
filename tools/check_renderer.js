// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { parse, compileScript } = require('@vue/compiler-sfc');

/** Check authored JavaScript, including script-setup, without authored TS files.
 * Copied Vue components are dependency boundaries, not silently rewritten.
 * Template syntax is checked separately by the actual Vite/Vue build.
 * @param {string} [root] */
function checkRenderer(root = path.resolve(__dirname, '..')) {
  const app = path.join(root, 'ui/src/App.vue');
  const virtualApp = path.join(root, 'ui/src/__cafe_app_check.js');
  const entry = path.join(root, 'ui/src/main.js');
  const parsed = parse(fs.readFileSync(app, 'utf8'), { filename: app });
  if (parsed.errors.length || parsed.descriptor.script?.lang || parsed.descriptor.scriptSetup?.lang) throw new Error('AUTHORED_JAVASCRIPT_REQUIRED');
  compileScript(parsed.descriptor, { id: 'cafe-check' });
  if (parsed.descriptor.script && parsed.descriptor.scriptSetup) throw new Error('MIXED_SCRIPT_CHECK_UNSUPPORTED');
  // Check the original JS: compiler hoisting can attach its JSDoc to generated exports.
  const script = parsed.descriptor.scriptSetup || parsed.descriptor.script;
  if (!script) throw new Error('AUTHORED_SCRIPT_ABSENT');
  const content = script.content + (parsed.descriptor.scriptSetup ? '\nconst __cafeComponent = {}; export default __cafeComponent;\n' : '');
  const virtual = new Map([[virtualApp, content]]);
  const options = { allowJs: true, checkJs: true, noEmit: true, skipLibCheck: true,
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler, types: [], strict: false };
  const compilerHost = ts.createCompilerHost(options);
  const read = compilerHost.readFile.bind(compilerHost), exists = compilerHost.fileExists.bind(compilerHost), source = compilerHost.getSourceFile.bind(compilerHost);
  compilerHost.fileExists = name => virtual.has(name) || exists(name);
  compilerHost.readFile = name => virtual.get(name) ?? read(name);
  compilerHost.getSourceFile = (name, language, onError, create) => virtual.has(name)
    ? ts.createSourceFile(name, virtual.get(name) || '', typeof language === 'object' ? language.languageVersion : language, true, ts.ScriptKind.JS)
    : source(name, language, onError, create);
  compilerHost.resolveModuleNames = (names, containing) => names.map(name => {
    if (name === './App.vue' && containing === entry) return { resolvedFileName: virtualApp, extension: ts.Extension.Js };
    if (name.endsWith('.vue') || name.endsWith('.css')) {
      const dependency = path.resolve(path.dirname(containing), name);
      const rel = path.relative(root, dependency).split(path.sep).join('/');
      const allowed = rel.startsWith('ui/src/kit/') || ['ui/src/cafe.css', 'ui/src/design/tokens.cafe.css'].includes(rel);
      if (!allowed || !fs.statSync(dependency).isFile()) return undefined;
      const stub = dependency + '.boundary.js';
      virtual.set(stub, name.endsWith('.vue')
        ? "/** @type {import('vue').Component} */ const component = {}; export default component;"
        : 'export {};');
      return { resolvedFileName: stub, extension: ts.Extension.Js };
    }
    return ts.resolveModuleName(name, containing, options, compilerHost).resolvedModule;
  });
  const program = ts.createProgram([entry, virtualApp], options, compilerHost);
  const diagnostics = ts.getPreEmitDiagnostics(program).map(d => ({ code: d.code,
    file: d.file ? path.relative(root, d.file.fileName).split(path.sep).join('/') : null,
    message: ts.flattenDiagnosticMessageText(d.messageText, '\n') }));
  return { schema: 'cafe.renderer-check.v1', status: diagnostics.length ? 'fail' : 'pass', authoredFiles: 2, diagnostics };
}
module.exports = { checkRenderer };
if (require.main === module) {
  try { const report = checkRenderer(); console.log(JSON.stringify(report)); process.exitCode = report.status === 'pass' ? 0 : 1; }
  catch { console.log(JSON.stringify({ schema: 'cafe.renderer-check.v1', status: 'fail', error: 'RENDERER_CHECK_UNAVAILABLE' })); process.exitCode = 1; }
}
