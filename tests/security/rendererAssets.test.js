'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const assets = require('../../src/main/rendererAssets');
const security = require('../../src/main/shellSecurity');

function fixture(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'renderer-assets-'));
  try {
    const out = path.join(root, 'dist', 'renderer'); fs.mkdirSync(out, { recursive: true });
    const files = [];
    for (const [name, bytes] of [['renderer.js', 'window.example = true;'], ['renderer.css', 'body { color: black; }']]) {
      fs.writeFileSync(path.join(out, name), bytes);
      files.push({ path: name, bytes: Buffer.byteLength(bytes), sha256: crypto.createHash('sha256').update(bytes).digest('hex') });
    }
    const manifest = { schema: 'cafe.renderer-assets.v1', files };
    const save = () => fs.writeFileSync(path.join(out, 'manifest.json'), JSON.stringify(manifest)); save();
    return run({ root, out, manifest, save });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

test('verified assets admit exactly two resource URLs, never frame URLs', () => fixture(({ root }) => {
  const bundle = assets.loadRendererBundle(root);
  assert.equal(bundle.javascript.toString(), 'window.example = true;');
  assert.equal(assets.admitsResource('cafe://app/renderer.js', bundle), true);
  assert.equal(assets.admitsResource('cafe://app/renderer.css', bundle), true);
  for (const url of ['https://app/renderer.js', 'cafe://other/renderer.js', 'cafe://app/renderer.js?q=x', 'cafe://app/renderer.js#x', 'cafe://app/%72enderer.js', 'cafe://app/../renderer.js', 'cafe://app:80/renderer.js', 'cafe://user@app/renderer.js']) {
    assert.equal(assets.admitsResource(url, bundle), false, url);
  }
  assert.equal(security.isBootstrapUrl('cafe://app/renderer.js'), false);
}));

test('unbuilt shell has no asset permission and partial output is rejected', () => fixture(({ root, out }) => {
  fs.unlinkSync(path.join(out, 'manifest.json'));
  assert.throws(() => assets.loadRendererBundle(root), /RENDERER_ASSET_REJECTED/);
  fs.unlinkSync(path.join(out, 'renderer.js')); fs.unlinkSync(path.join(out, 'renderer.css'));
  assert.equal(assets.loadRendererBundle(root), undefined);
  assert.equal(assets.admitsResource('cafe://app/renderer.js', undefined), false);
}));

test('same-length byte tampering fails before any source can be served', () => fixture(({ root, out }) => {
  const p = path.join(out, 'renderer.js'); const bytes = fs.readFileSync(p); bytes[0] ^= 1; fs.writeFileSync(p, bytes);
  assert.throws(() => assets.loadRendererBundle(root), /RENDERER_ASSET_REJECTED/);
}));

test('manifest traversal, duplicates, unknown keys and unsafe lengths are rejected', () => {
  for (const mutate of [m => { m.files[0].path = '../renderer.js'; }, m => { m.files[1] = m.files[0]; }, m => { m.extra = true; }, m => { m.files[0].bytes = 0; }, m => { m.files[0].bytes = 9007199254740992; }, m => { m.files[0].sha256 = 'f'.repeat(63); }, m => { m.schema = 'other'; }]) {
    fixture(({ root, manifest, save }) => { mutate(manifest); save(); assert.throws(() => assets.loadRendererBundle(root), /RENDERER_ASSET_REJECTED/); });
  }
});

test('protocol serves the reviewed UI under the original closed bootstrap', async () => {
  const responses = fixture(({ root }) => {
    let handler;
    security.installBootstrapProtocol({ handle: (_scheme, callback) => { handler = callback; } }, assets.loadRendererBundle(root));
    return [handler({ url: security.BOOT_URL, method: 'GET' }), handler({ url: 'cafe://app/renderer.js', method: 'GET' }), handler({ url: 'cafe://app/renderer.css', method: 'POST' }), handler({ url: 'file:///secret', method: 'GET' })];
  });
  assert.match(await responses[0].text(), /data-cafe-theme="dark"/);
  assert.match(await responses[1].text(), /window.example/);
  assert.equal(responses[2].status, 403); assert.equal(responses[3].status, 403);
  assert.equal(responses[0].headers.get('Content-Security-Policy'), security.CSP);
  assert.doesNotMatch(security.CSP, /unsafe-inline|unsafe-eval|https:/);
});

test('session resource permission does not relax exact top-frame IPC admission', () => fixture(({ root }) => {
  let check;
  security.hardenSession({ setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, webRequest: { onBeforeRequest(_filter, callback) { check = callback; }, onHeadersReceived() {} } }, assets.loadRendererBundle(root));
  let decision;
  check({ url: 'cafe://app/renderer.css' }, value => { decision = value; }); assert.deepEqual(decision, { cancel: false });
  check({ url: 'https://example.invalid/x' }, value => { decision = value; }); assert.deepEqual(decision, { cancel: true });
  const frame = { url: 'cafe://app/renderer.js', detached: false, parent: null };
  const contents = { isDestroyed: () => false, mainFrame: frame };
  assert.throws(() => security.admitCaller({ sender: contents, senderFrame: frame }, contents), /IPC_CALLER_DENIED/);
}));
