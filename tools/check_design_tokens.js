// @ts-check
'use strict';
/**
 * Explicit CSS inventory gate. PostCSS parses CSS and Vue compiler-sfc extracts
 * SFC style blocks. This is not a complete CSS value/selector implementation:
 * the bounded value lexer rejects escapes outside strings and non-ASCII names;
 * mapping custom properties permit only the documented root theme selectors.
 * Only one exact top-level mapping import of the fixed vendor path is admitted;
 * all other imports, external/preprocessed SFC styles and conditional mappings abstain.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const postcss = require('postcss');
const sfc = require('@vue/compiler-sfc');
const MAPPING = 'ui/src/design/tokens.cafe.css';
const VENDOR = 'ui/src/design/vendor/open-design-openai/tokens.css';
const THEMES = ['default', 'dark', 'light'];
class DesignTokenError extends Error {
  constructor(code, detail = {}) { super(code); this.code = code; this.detail = detail; }
}
function fail(code, detail = {}) { throw new DesignTokenError(code, detail); }
function sha(raw) { return crypto.createHash('sha256').update(raw).digest('hex'); }
function safeFile(root, name) {
  if (typeof name !== 'string' || !name || name.includes('\\') || name.includes('\0') || name.includes(':') || path.posix.isAbsolute(name) || name.split('/').some(p => !p || p === '.' || p === '..')) fail('INVALID_LAYER_PATH');
  const target = path.join(root, name);
  try { if (fs.realpathSync(target) !== target || !fs.statSync(target).isFile()) fail('INVALID_LAYER_PATH', { path: name }); }
  catch (error) { if (error instanceof DesignTokenError) throw error; fail('LAYER_UNAVAILABLE', { path: name }); }
  return target;
}
/** Bounded lexical tree, never interpreting comments/quoted strings as calls. */
function references(value) {
  let at = 0;
  function tokens(end) {
    const out = [];
    while (at < value.length) {
      const ch = value[at];
      if (end && ch === end) { at++; return out; }
      if (/\s/.test(ch)) { at++; continue; }
      if (value.startsWith('/*', at)) { const close = value.indexOf('*/', at + 2); if (close < 0) fail('UNSUPPORTED_CSS_VALUE'); at = close + 2; continue; }
      if (ch === '"' || ch === "'") {
        const quote = ch; at++; let closed = false;
        while (at < value.length) { const c = value[at++]; if (c === '\\') { if (at >= value.length) fail('UNSUPPORTED_CSS_VALUE'); at++; } else if (c === quote) { closed = true; break; } }
        if (!closed) fail('UNSUPPORTED_CSS_VALUE'); out.push({kind: 'string'}); continue;
      }
      if (ch === '\\' || ch.charCodeAt(0) > 127) fail('UNSUPPORTED_CSS_VALUE');
      if (ch === ')' || ch === ']') fail('UNSUPPORTED_CSS_VALUE');
      if (/[A-Za-z_-]/.test(ch)) {
        const start = at++; while (at < value.length && /[A-Za-z0-9_-]/.test(value[at])) at++;
        const name = value.slice(start, at);
        if (value[at] === '(') { at++; out.push({kind: 'function', name, children: tokens(')')}); }
        else out.push({kind: 'ident', name});
        continue;
      }
      if (ch === '(' || ch === '[') { at++; out.push({kind: 'block', children: tokens(ch === '(' ? ')' : ']')}); continue; }
      at++; out.push({kind: 'punct', name: ch});
    }
    if (end) fail('UNSUPPORTED_CSS_VALUE'); return out;
  }
  const found = new Set();
  function walk(nodes) {
    for (const node of nodes) {
      if (node.kind === 'function' && node.name.toLowerCase() === 'var') {
        const comma = node.children.findIndex(t => t.kind === 'punct' && t.name === ',');
        const first = comma < 0 ? node.children : node.children.slice(0, comma);
        if (first.length !== 1 || first[0].kind !== 'ident' || !/^--[A-Za-z0-9_-]+$/.test(first[0].name)) fail('UNSUPPORTED_VAR_SYNTAX');
        found.add(first[0].name);
      }
      if (node.children) walk(node.children);
    }
  }
  walk(tokens(null)); return [...found];
}
/** Canonical root selector subset, not a general selector parser. */
function states(selector) {
  const result = new Set();
  for (const part of selector.split(',')) {
    const s = part.trim().replace(/\s*=\s*/g, '=');
    if (s === ':root' || s === 'html') { THEMES.forEach(x => result.add(x)); continue; }
    // A legacy class alias cannot establish or override canonical attribute states.
    if (s === '.mv-dark') continue;
    const match = /^(?::root|html)?\[data-cafe-theme=(?:'([^']+)'|"([^"]+)"|([A-Za-z]+))\]$/.exec(s);
    if (match && ['dark', 'light'].includes(match[1] || match[2] || match[3])) { result.add(match[1] || match[2] || match[3]); continue; }
    const notLight = /^(?::root|html):not\(\[data-cafe-theme=(?:'light'|"light"|light)\]\)$/.test(s);
    if (notLight) { result.add('default'); result.add('dark'); continue; }
    fail('UNSUPPORTED_MAPPING_SELECTOR');
  }
  return result;
}
function parseStyles(name, text) {
  if (name.endsWith('.css')) return [text];
  if (!name.endsWith('.vue')) fail('UNSUPPORTED_LAYER_TYPE', { path: name });
  const parsed = sfc.parse(text, { filename: name });
  if (parsed.errors.length) fail('SFC_SYNTAX', { path: name });
  return parsed.descriptor.styles.map(style => {
    if (style.src || (style.lang && style.lang !== 'css')) fail('UNSUPPORTED_SFC_STYLE', { path: name });
    return style.content;
  });
}
function checkDesignTokens(repo) {
  let root;
  try { root = fs.realpathSync(repo); } catch { fail('REPO_UNAVAILABLE'); }
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(safeFile(root, 'ui/src/design/layers.json'), 'utf8')); }
  catch (error) { if (error instanceof DesignTokenError) throw error; fail('INVALID_LAYERS_MANIFEST'); }
  if (!manifest || manifest.schema !== 'cafe.design-layers.v1' || !Array.isArray(manifest.layers) || !manifest.layers.length) fail('INVALID_LAYERS_MANIFEST');
  const names = new Set(), declarations = [], copiedRefs = [];
  let copiedDeclarations = 0, vendorImportSeen = false;
  // These two exact paths are the fixed token inputs, not copied-layer rows.
  // Mapping edits must be tested semantically, not rejected by a stale hash.
  const rows = [[{path: VENDOR}, true], [{path: MAPPING}, true], ...manifest.layers.map(row => [row, false])];
  for (const [row, fixed] of rows) {
    if (!fixed && (!row || typeof row !== 'object' || !/^[a-f0-9]{64}$/.test(row.sha256) || !(typeof row.source === 'string' && row.source.length || row.source && typeof row.source === 'object' && !Array.isArray(row.source)))) fail('INVALID_LAYER_RECORD');
    if (!fixed && [MAPPING, VENDOR].includes(row.path)) fail('RESERVED_LAYER_PATH');
    let file;
    try { file = safeFile(root, row.path); }
    catch (error) { if (fixed && error instanceof DesignTokenError && error.code === 'LAYER_UNAVAILABLE') fail('REQUIRED_TOKEN_LAYER_ABSENT', {path:row.path}); throw error; }
    if (names.has(row.path)) fail('DUPLICATE_LAYER_PATH'); names.add(row.path);
    const raw = fs.readFileSync(file); if (!fixed && sha(raw) !== row.sha256) fail('LAYER_HASH_DRIFT', { path: row.path });
    for (const style of parseStyles(row.path, raw.toString('utf8'))) {
      let ast; try { ast = postcss.parse(style, { from: row.path }); } catch { fail('CSS_SYNTAX', { path: row.path }); }
      ast.walkAtRules(rule => {
        if (!['import', 'use', 'forward'].includes(rule.name.toLowerCase())) return;
        const exact = ['"./vendor/open-design-openai/tokens.css"', "'./vendor/open-design-openai/tokens.css'"].includes(rule.params.trim());
        if (rule.name.toLowerCase() === 'import' && row.path === MAPPING && rule.parent.type === 'root' && exact && !vendorImportSeen) { vendorImportSeen = true; return; }
        fail('UNSUPPORTED_EXTERNAL_STYLE', {path:row.path});
      });
      ast.walkDecls(decl => {
        const refs = references(decl.value);
        if (row.path === MAPPING || row.path === VENDOR) {
          if (!decl.prop.startsWith('--')) { declarations.push({ path: row.path, refs, prop: null, themes: THEMES }); return; }
          if (!/^--[A-Za-z0-9_-]+$/.test(decl.prop)) fail('UNSUPPORTED_CUSTOM_PROPERTY');
          if (!decl.parent || decl.parent.type !== 'rule' || decl.parent.parent.type !== 'root') fail('UNSUPPORTED_CONDITIONAL_MAPPING', { path: row.path });
          declarations.push({path: row.path, prop: decl.prop, refs, themes: [...states(decl.parent.selector)]});
        } else { copiedDeclarations++; copiedRefs.push({path: row.path, refs}); }
      });
    }
  }
  if (!names.has(MAPPING) || !names.has(VENDOR)) fail('REQUIRED_TOKEN_LAYER_ABSENT');
  if (!copiedDeclarations) fail('EMPTY_COPIED_STYLE_SELECTION');
  const resolution = {};
  for (const theme of THEMES) {
    const mapping = new Map(), vendor = new Map();
    // Root defaults first, then theme-specific selectors, respecting local order.
    for (const specificity of ['base', 'theme']) for (const decl of declarations) {
      if (!decl.prop || !decl.themes.includes(theme) || (decl.themes.length === 3 ? 'base' : 'theme') !== specificity) continue;
      (decl.path === MAPPING ? mapping : vendor).set(decl.prop, decl.refs);
    }
    for (const use of copiedRefs) for (const token of use.refs) if (!mapping.has(token)) fail('MISSING_USED_TOKEN', { path: use.path, token, theme });
    const done = new Set(), active = new Set();
    function resolve(token) {
      if (done.has(token)) return;
      if (active.has(token)) fail('TOKEN_CYCLE', { token, theme });
      if (!mapping.has(token) && !vendor.has(token)) fail('MISSING_NESTED_TOKEN', { token, theme });
      active.add(token); for (const next of mapping.has(token) ? mapping.get(token) : vendor.get(token)) resolve(next); active.delete(token); done.add(token);
    }
    for (const token of mapping.keys()) resolve(token);
    for (const decl of declarations) if (!decl.prop && decl.themes.includes(theme)) decl.refs.forEach(resolve);
    resolution[theme] = {mapping: mapping.size, resolved: done.size};
  }
  return {schema:'cafe.design-token-check.v1', status:'pass', files:names.size, themes:resolution, copiedReferences:copiedRefs.reduce((n, row) => n + row.refs.length, 0)};
}
module.exports = { checkDesignTokens, DesignTokenError };
if (require.main === module) {
  try { if (process.argv.length !== 4 || process.argv[2] !== '--repo') fail('USAGE'); console.log(JSON.stringify(checkDesignTokens(process.argv[3]))); }
  catch (error) { console.log(JSON.stringify({schema:'cafe.design-token-check.v1',status:'fail',error:error instanceof DesignTokenError ? error.code : 'CHECK_UNAVAILABLE',detail:error instanceof DesignTokenError ? error.detail : {}})); process.exitCode = 1; }
}
