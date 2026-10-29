// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildWindowsApp, main } = require('../../tools/build_windows_app');

test('malformed and unknown package requests fail before effects with fixed errors', async () => {
  for (const value of [null, [], {}, { source: 'relative' }, { extra: true }]) {
    await assert.rejects(() => buildWindowsApp(value), error => error.message === 'WINDOWS_PACKAGE_BLOCKED');
  }
});
test('missing authority and invalid inventory do not produce a shell-only success', async () => {
  const request = { source: 'absent', output: 'absent', electronZipDir: 'absent', pythonTool: 'absent', pythonArchive: 'absent', websocketsWheel: 'absent', authorityExe: 'absent', authoritySha256: '0'.repeat(64), inventory: [] };
  await assert.rejects(() => buildWindowsApp(request), error => error.message === 'WINDOWS_PACKAGE_BLOCKED');
  await assert.rejects(() => buildWindowsApp({ ...request, inventory: ['../escape'] }), error => error.message === 'WINDOWS_PACKAGE_BLOCKED');
});

test('noncanonical output parent is rejected before reading the Electron cache', async t => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const temporary = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'package-parent-test-')));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const archive = path.join(temporary, 'electron-v44.9.2-win32-x64.zip');
  fs.writeFileSync(archive, 'untrusted cache must not be opened');
  const parent = path.join(temporary, 'alias');
  const original = fs.realpathSync.native;
  t.mock.method(fs.realpathSync, 'native', file => String(file) === parent ? temporary : original(file));
  const open = t.mock.method(fs, 'openSync', () => { throw new Error('cache access forbidden'); });
  const request = { source: path.resolve(__dirname, '../..'), output: path.join(parent, 'app.zip'), electronZipDir: temporary, pythonTool: 'absent', pythonArchive: 'absent', websocketsWheel: 'absent', authorityExe: 'absent', authoritySha256: '0'.repeat(64), inventory: [] };
  await assert.rejects(() => buildWindowsApp(request), error => error.message === 'WINDOWS_PACKAGE_BLOCKED');
  assert.equal(open.mock.callCount(), 0);
  assert.equal(fs.existsSync(path.join(temporary, 'app.zip')), false);
});
