// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {checkDesignTokens, DesignTokenError} = require('../../tools/check_design_tokens');
const MAPPING = 'ui/src/design/tokens.cafe.css';
const VENDOR = 'ui/src/design/vendor/open-design-openai/tokens.css';
const COMPONENT = 'ui/src/copied.css';
const BASE = ':root { --ink:#111; --surface:#fff; --vendor-only:#777; --vendor-cycle:var(--text); }';
const MAP = ':root { --text:var(--ink); --background:var(--surface); } :root:not([data-cafe-theme="light"]) { --surface:#111; --ink:#eee; } [data-cafe-theme="light"] { --surface:#fff; --ink:#111; }';
function fixture(t, mapping = MAP, component = '.status { color:var(--text); background:var(--background); }', componentName = COMPONENT) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'design-token-test-'));
  t.after(() => fs.rmSync(root, {recursive:true,force:true}));
  const names = [[VENDOR, BASE], [MAPPING, mapping], [componentName, component]];
  const manifest = {schema:'cafe.design-layers.v1',layers:names.map(([name, content]) => {
    const file = path.join(root,name);fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,content);
    return {path:name,sha256:crypto.createHash('sha256').update(content).digest('hex'),source:{origin:'handwritten-test-fixture'}};
  }).filter(row => row.path === componentName)};
  function save() { fs.writeFileSync(path.join(root,'ui/src/design/layers.json'),JSON.stringify(manifest)); }
  save();return {root,manifest,save};
}
function rejects(root, code) { assert.throws(() => checkDesignTokens(root), e => e instanceof DesignTokenError && e.code === code); }
test('manual root default dark and explicit themes all resolve', t => {
  const f=fixture(t);const result=checkDesignTokens(f.root);
  assert.equal(result.status,'pass');assert.deepEqual(Object.keys(result.themes),['default','dark','light']);
  assert.equal(result.copiedReferences,2);assert.equal(result.files,3);
});
test('missing used token fails even if the vendor defines it', t => {
  const f=fixture(t,MAP,'.status { color:var(--vendor-only); }');rejects(f.root,'MISSING_USED_TOKEN');
});
test('fallback cannot excuse a missing used token', t => {
  const f=fixture(t,MAP,'.status { color:var(--absent, red); }');rejects(f.root,'MISSING_USED_TOKEN');
});
test('nested missing mapping reference fails despite fallback', t => {
  const f=fixture(t,':root { --text:var(--absent, #111); }','.status { color:var(--text); }');rejects(f.root,'MISSING_NESTED_TOKEN');
});
test('cycles across mapping and vendor are rejected', t => {
  const f=fixture(t,':root { --text:var(--vendor-cycle); }','.status{color:var(--text)}');rejects(f.root,'TOKEN_CYCLE');
});
test('comments and quoted strings never become false references', t => {
  const f=fixture(t,MAP,'.status { /* var(--absent) */ content:"var(--missing)"; color:var(/* note */--text); }');
  assert.equal(checkDesignTokens(f.root).copiedReferences,1);
});
test('Vue real parser reads only style blocks, not template/script strings', t => {
  const f=fixture(t,MAP,'<template><p>var(--missing)</p></template><script>const label="var(--absent)";</script><style scoped>.status{color:var(--text)}</style>','ui/src/Status.vue');
  assert.equal(checkDesignTokens(f.root).copiedReferences,1);
});
test('Vue missing style reference fails with actual style content', t => {
  const f=fixture(t,MAP,'<template><p/></template><style>.status{color:var(--absent)}</style>','ui/src/Status.vue');rejects(f.root,'MISSING_USED_TOKEN');
});
test('Vue external style and preprocessor styles explicitly abstain', t => {
  for(const content of ['<template><p/></template><style src="./external.css"></style>','<template><p/></template><style lang="scss">.x{color:red}</style>']) {
    const f=fixture(t,MAP,content,'ui/src/Status.vue');rejects(f.root,'UNSUPPORTED_SFC_STYLE');
  }
});
test('manifest traversal refuses before reading an outside path', t => {
  const f=fixture(t);f.manifest.layers[0].path='../outside.css';f.save();rejects(f.root,'INVALID_LAYER_PATH');
});
test('realpath escape refuses without requiring a privileged symlink', t => {
  const f=fixture(t);const real=fs.realpathSync;
  t.mock.method(fs,'realpathSync', name => String(name)===path.join(f.root,COMPONENT) ? path.join(path.dirname(f.root),'outside.css') : real(name));
  rejects(f.root,'INVALID_LAYER_PATH');
});
test('duplicate explicit layer paths cannot be ambiguous', t => {
  const f=fixture(t);f.manifest.layers.push({...f.manifest.layers[0]});f.save();rejects(f.root,'DUPLICATE_LAYER_PATH');
});
test('removing a mapping token reaches semantic failure rather than hash failure', t => {
  const f=fixture(t);const file=path.join(f.root,MAPPING);fs.writeFileSync(file,fs.readFileSync(file,'utf8').replace('--text:var(--ink);',''));
  rejects(f.root,'MISSING_USED_TOKEN');
});
test('fixed token inputs cannot be replaced by manifest rows and must exist', t => {
  const f=fixture(t);f.manifest.layers[0].path=MAPPING;f.save();rejects(f.root,'RESERVED_LAYER_PATH');
  const g=fixture(t);fs.unlinkSync(path.join(g.root,MAPPING));rejects(g.root,'REQUIRED_TOKEN_LAYER_ABSENT');
});
test('source file hash drift cannot pass', t => {
  const f=fixture(t);fs.appendFileSync(path.join(f.root,COMPONENT),'/* changed */');rejects(f.root,'LAYER_HASH_DRIFT');
});
test('every explicit theme and default must resolve', t => {
  const f=fixture(t,':root:not([data-cafe-theme="light"]){--text:var(--ink)}','.status{color:var(--text)}');
  rejects(f.root,'MISSING_USED_TOKEN');
  const g=fixture(t,':root{--text:var(--ink)} [data-cafe-theme="light"]{--text:var(--absent)}','.status{color:var(--text)}');rejects(g.root,'MISSING_NESTED_TOKEN');
  const h=fixture(t,'[data-cafe-theme="dark"]{--text:var(--ink)} [data-cafe-theme="light"]{--text:var(--ink)}','.status{color:var(--text)}');rejects(h.root,'MISSING_USED_TOKEN');
});
test('unknown escapes, selectors and conditional mapping abstain explicitly', t => {
  const f=fixture(t,MAP,'.status{color:var(--te\\78t)}');rejects(f.root,'UNSUPPORTED_CSS_VALUE');
  const escapedFunction=fixture(t,MAP,String.raw`.status{color:v\61r(--text)}`);rejects(escapedFunction.root,'UNSUPPORTED_CSS_VALUE');
  const escapedName=fixture(t,String.raw`:root{--te\78t:var(--ink)}`,'.status{color:var(--text)}');rejects(escapedName.root,'UNSUPPORTED_CUSTOM_PROPERTY');
  const g=fixture(t,'.unknown{--text:red}', '.status{color:var(--text)}');rejects(g.root,'UNSUPPORTED_MAPPING_SELECTOR');
  const h=fixture(t,'@media (min-width:1px){:root{--text:red}}','.status{color:var(--text)}');rejects(h.root,'UNSUPPORTED_CONDITIONAL_MAPPING');
});
test('explicit inventory ignores an unlisted source and requires copied style declarations', t => {
  const f=fixture(t);fs.writeFileSync(path.join(f.root,'unlisted.css'),'.bad{color:var(--missing)}');assert.equal(checkDesignTokens(f.root).status,'pass');
  const g=fixture(t,MAP,'/* no actual declarations */');rejects(g.root,'EMPTY_COPIED_STYLE_SELECTION');
});
test('nested fallback references remain checked instead of hidden', t => {
  const f=fixture(t,':root{--text:var(--ink, var(--missing))}', '.status{color:var(--text)}');rejects(f.root,'MISSING_NESTED_TOKEN');
});
test('legacy class aliases cannot mask unresolved canonical dark references', t => {
  const f=fixture(t,':root{--text:var(--ink)} [data-cafe-theme="dark"]{--text:var(--missing)} .mv-dark{--text:var(--ink)}','.status{color:var(--text)}');rejects(f.root,'MISSING_NESTED_TOKEN');
  const g=fixture(t,':root{--text:var(--ink)} .mv-dark{--text:var(--missing)}','.status{color:var(--text)}');assert.equal(checkDesignTokens(g.root).status,'pass');
});
test('only the exact once top-level fixed vendor import is admitted', t => {
  for (const quoted of ['"./vendor/open-design-openai/tokens.css"', "'./vendor/open-design-openai/tokens.css'"]) {
    const f=fixture(t,'@import '+quoted+';'+MAP);assert.equal(checkDesignTokens(f.root).status,'pass');
  }
  for (const statement of [
    '@import "../vendor/open-design-openai/tokens.css";',
    '@import "https://example.invalid/tokens.css";',
    '@import url("./vendor/open-design-openai/tokens.css");',
    '@import "./vendor/open-design-openai/tokens.css" screen;',
    '@import "./vendor/open-design-openai/tokens.css" supports(display:grid);',
    '@import "./vendor/open-design-openai/tokens.css" layer(cafe);',
    '@import "./vendor/open-design-openai/tokens.css";@import "./vendor/open-design-openai/tokens.css";',
    '@media screen{@import "./vendor/open-design-openai/tokens.css";}'
  ]) {
    const f=fixture(t,statement+MAP);rejects(f.root,'UNSUPPORTED_EXTERNAL_STYLE');
  }
});
test('CLI reports a closed failure without source stack/host coordinates', t => {
  const f=fixture(t,MAP,'.status{color:var(--missing)}');const command=spawnSync(process.execPath,[path.resolve(__dirname,'../../tools/check_design_tokens.js'),'--repo',f.root],{encoding:'utf8'});
  assert.equal(command.status,1);assert.equal(command.stderr,'');const result=JSON.parse(command.stdout);assert.equal(result.error,'MISSING_USED_TOKEN');assert.equal(result.detail.path,COMPONENT);assert.ok(!command.stdout.includes(f.root));
});
