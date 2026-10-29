'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { checkRenderer } = require('../../tools/check_renderer');

function fixture(script, run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'renderer-check-'));
  try {
    fs.mkdirSync(path.join(root, 'ui/src'), { recursive: true });
    fs.writeFileSync(path.join(root, 'ui/src/App.vue'), '<script setup>\n// @ts-check\n' + script + '\n</script><template><p>Café</p></template>');
    fs.writeFileSync(path.join(root, 'ui/src/main.js'), "import App from './App.vue'; console.log(App);");
    return run(root);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}
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
