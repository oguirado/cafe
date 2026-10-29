// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createPackagedBackend } = require('../../src/main/runtimeBackend');
const { createBackendSupervisor } = require('../../src/main/backendSupervisor');
const { EventEmitter } = require('node:events');

/** @param {(resources:string,root:string)=>void} body */
function fixture(body) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runtime-backend-contract-'));
  try {
    const resources = path.join(root, 'resources');
    fs.mkdirSync(path.join(resources, 'backend'), { recursive: true });
    const binary = Buffer.from('synthetic fixture; never executed');
    fs.writeFileSync(path.join(resources, 'backend', 'cafe-core.exe'), binary);
    fs.writeFileSync(path.join(resources, 'cafe-build.json'), JSON.stringify({
      schema: 'cafe.unsigned-windows-app.v1',
      authority_sha256: crypto.createHash('sha256').update(binary).digest('hex'),
    }));
    body(resources, root);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}
/** @param {string} resources @param {string} root */
function options(resources, root) {
  const frame = {};
  return { resourcesPath: resources, runtimeRoot: path.join(root, 'authority'),
    authorizedOrigin: 'cafe://app', getAuthorizedFrame: () => frame };
}

test('host factory verifies the pinned file without spawning before start', () => fixture((resources, root) => {
  const supervisor = createPackagedBackend(options(resources, root));
  assert.equal(supervisor.getStatus().pid, null);
  assert.equal(fs.existsSync(path.join(root, 'authority')), false);
  supervisor.dispose();
}));
test('changed authority bytes fail before process creation', () => fixture((resources, root) => {
  fs.appendFileSync(path.join(resources, 'backend', 'cafe-core.exe'), 'changed');
  assert.throws(() => createPackagedBackend(options(resources, root)), /AUTHORITY_HASH_REJECTED/);
  assert.equal(fs.existsSync(path.join(root, 'authority')), false);
}));
test('missing receipt and relative runtime root fail closed', () => fixture((resources, root) => {
  assert.throws(() => createPackagedBackend({ ...options(resources, root), runtimeRoot: 'relative' }), /AUTHORITY_PATH_REJECTED/);
  fs.unlinkSync(path.join(resources, 'cafe-build.json'));
  assert.throws(() => createPackagedBackend(options(resources, root)));
}));
test('unapproved schema and nonhex pin fail closed', () => fixture((resources, root) => {
  const receipt = path.join(resources, 'cafe-build.json');
  fs.writeFileSync(receipt, JSON.stringify({ schema: 'other', authority_sha256: 'a'.repeat(64) }));
  assert.throws(() => createPackagedBackend(options(resources, root)), /AUTHORITY_RECEIPT_REJECTED/);
  fs.writeFileSync(receipt, JSON.stringify({ schema: 'cafe.unsigned-windows-app.v1', authority_sha256: 'z'.repeat(64) }));
  assert.throws(() => createPackagedBackend(options(resources, root)), /AUTHORITY_RECEIPT_REJECTED/);
}));
test('signal guard rejects dangerous or changed destinations without any kernel signal', () => {
  for (const initial of [-1, 0, 1, process.pid, undefined]) {
    let killed = 0;
    const events = new EventEmitter();
    const child = Object.assign(events, { pid: initial, stdout: new EventEmitter(),
      stderr: new EventEmitter(), stdin: Object.assign(new EventEmitter(), { write: () => true, writableLength: 0 }),
      kill: () => { killed++; return true; } });
    const supervisor = createBackendSupervisor({ spawn: () => child,
      command: 'synthetic-not-spawned', authorizedOrigin: 'cafe://app', getAuthorizedFrame: () => ({}) });
    supervisor.start(); supervisor.dispose();
    assert.equal(killed, 0);
  }
});
