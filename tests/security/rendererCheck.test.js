'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { checkRenderer, createRendererHost, fileIdentity } = require('../../tools/check_renderer');

const VUE_MODULES = path.dirname(path.dirname(require.resolve('vue/package.json')));
const MAIN = "import App from './App.vue'; console.log(App);";

function fixture(script, run, { name = 'renderer-check-', main = MAIN, files = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), name));
  try {
    fs.mkdirSync(path.join(root, 'ui/src'), { recursive: true });
    fs.writeFileSync(path.join(root, 'ui/src/App.vue'), '<script setup>\n// @ts-check\n' + script + '\n</script><template><p>Café</p></template>');
    fs.writeFileSync(path.join(root, 'ui/src/main.js'), main);
    // Boundary stubs type against 'vue': link the installed packages (junction needs no elevation on Windows).
    if (Object.keys(files).some(rel => rel.endsWith('.vue'))) fs.symlinkSync(VUE_MODULES, path.join(root, 'node_modules'), 'junction');
    for (const [rel, body] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
      fs.writeFileSync(path.join(root, rel), body);
    }
    return run(root);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

/** The other separator spelling of an absolute path: slash <-> backslash. */
const flip = name => name.includes('\\') ? name.replace(/\\/g, '/') : name.replace(/\//g, '\\');
const UNICODE = 'renderer check café ☕ ñ ';

test('authored script-setup JavaScript is actually checked', () => {
  fixture('/** @type {number} */ const value = 1;', root => assert.equal(checkRenderer(root).status, 'pass'));
  fixture('/** @type {number} */ const value = "wrong";', root => {
    const result = checkRenderer(root); assert.equal(result.status, 'fail');
    assert.ok(result.diagnostics.some(d => d.code === 2322));
  });
});
test('authored renderer entry is actually checked', () => {
  fixture('const value = 1;', root => {
    fs.appendFileSync(path.join(root, 'ui/src/main.js'), '\n/** @type {number} */ const wrong = "bad";');
    assert.equal(checkRenderer(root).status, 'fail');
  });
});

test('file identity is one spelling for both separators', () => {
  assert.equal(fileIdentity('C:\\workspace\\a b\\Café\\ui\\..\\ui\\src\\main.js'), 'C:/workspace/a b/Café/ui/src/main.js');
  assert.equal(fileIdentity('C:/workspace/a b/Café/ui/src/main.js'), 'C:/workspace/a b/Café/ui/src/main.js');
  assert.equal(fileIdentity('C:\\'), 'C:/');
  assert.equal(fileIdentity('\\\\srv\\share\\x\\'), '//srv/share/x');
  assert.equal(fileIdentity('/tmp//a b/./ñ/'), '/tmp/a b/ñ');
});

test('spaces and unicode in the root: valid passes, wrong script and wrong entry fail', () => {
  const opts = { name: UNICODE };
  fixture('/** @type {number} */ const value = 1;', root => {
    assert.match(root, /café ☕ ñ /);
    const result = checkRenderer(root);
    assert.deepEqual(result.diagnostics, []); assert.equal(result.status, 'pass');
    // Redundant and trailing separators name the same root.
    assert.equal(checkRenderer(root + path.sep + '.' + path.sep).status, 'pass');
  }, opts);
  fixture('/** @type {number} */ const value = "wrong";', root => {
    const result = checkRenderer(root);
    assert.equal(result.status, 'fail');
    assert.ok(result.diagnostics.some(d => d.code === 2322 && d.file === 'ui/src/__cafe_app_check.js'), JSON.stringify(result.diagnostics));
    assert.ok(!result.diagnostics.some(d => d.code === 6053 || d.code === 2307));
  }, opts);
  fixture('const value = 1;', root => {
    const result = checkRenderer(root);
    assert.equal(result.status, 'fail');
    assert.ok(result.diagnostics.some(d => d.code === 2322 && d.file === 'ui/src/main.js'), JSON.stringify(result.diagnostics));
  }, { ...opts, main: MAIN + '\n/** @type {number} */ const wrong = "bad";' });
});

test('vendor boundary allowlist holds under a unicode root', () => {
  const files = { 'ui/src/kit/Cafe Button.vue': '<template><b/></template>', 'ui/src/cafe.css': '', 'ui/src/Other.vue': '<template><i/></template>' };
  fixture("import B from './kit/Cafe Button.vue'; import './cafe.css'; console.log(B);", root => {
    assert.deepEqual(checkRenderer(root).diagnostics, []);
  }, { name: UNICODE, files });
  for (const bad of ['./Other.vue', './kit/Missing.vue', '.\\kit\\Cafe Button.vue', '../../outside.vue']) {
    fixture(`import X from '${bad.replace(/\\/g, '\\\\')}'; console.log(X);`, root => {
      const result = checkRenderer(root);
      assert.equal(result.status, 'fail', bad);
      assert.ok(result.diagnostics.some(d => d.code === 2307), bad + ' ' + JSON.stringify(result.diagnostics));
    }, { name: UNICODE, files });
  }
});

test('compiler compilerHost matches virtual files and the entry under either separator', () => {
  fixture('const value = 1;', root => {
    const { compilerHost, entry, virtualApp } = createRendererHost(root, 'export default 1;');
    const nativeEntry = path.join(root, 'ui', 'src', 'main.js');
    const nativeApp = path.join(root, 'ui', 'src', '__cafe_app_check.js');
    for (const spelling of [virtualApp, flip(virtualApp), nativeApp, flip(nativeApp)]) {
      assert.ok(compilerHost.fileExists(spelling), spelling);
      assert.equal(compilerHost.readFile(spelling), 'export default 1;');
      assert.equal(compilerHost.getSourceFile(spelling, 99)?.text, 'export default 1;');
    }
    for (const containing of [entry, flip(entry), nativeEntry, flip(nativeEntry)]) {
      const [app, kit, escape, backslash] = compilerHost.resolveModuleNames(['./App.vue', './kit/Cafe Button.vue', '../../x.vue', '.\\kit\\Cafe Button.vue'], containing, undefined, undefined, {});
      assert.equal(app?.resolvedFileName, virtualApp, containing);
      assert.equal(fileIdentity(kit?.resolvedFileName ?? ''), fileIdentity(path.join(root, 'ui/src/kit/Cafe Button.vue.boundary.js')));
      assert.ok(compilerHost.fileExists(flip(kit.resolvedFileName)));
      assert.equal(escape, undefined); assert.equal(backslash, undefined);
    }
    // Only the entry may import the virtual App.
    const [other] = compilerHost.resolveModuleNames(['./App.vue'], flip(path.join(root, 'ui/src/kit/x.js')), undefined, undefined, {});
    assert.notEqual(other?.resolvedFileName, virtualApp);
  }, { name: UNICODE, files: { 'ui/src/kit/Cafe Button.vue': '<template><b/></template>' } });
});
