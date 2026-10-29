// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { verifyFfmpegSelection } = require('../../src/main/ffmpegSelection');

function fixture(t) {
  const directory = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-ffmpeg-selection-')));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, 'selected.bin'), bytes = Buffer.from('synthetic hash-only selection');
  fs.writeFileSync(file, bytes);
  return { file, bytes, config: { ffmpeg_binary: { path: file, sha256: crypto.createHash('sha256').update(bytes).digest('hex') } } };
}
function blocked(config) {
  assert.throws(() => verifyFfmpegSelection(config), error => error.message === 'FFMPEG_SELECTION_BLOCKED');
}
test('exact user selection returns frozen hash evidence, never an encoder approval', t => {
  const f = fixture(t), result = verifyFfmpegSelection(f.config);
  assert.deepEqual(result, { file: f.file, sha256: f.config.ffmpeg_binary.sha256, bytes: f.bytes.length });
  assert.ok(Object.isFrozen(result));assert.equal(Object.keys(result).length, 3);
});
test('one closed config key, missing selection and unknown fields fail', t => {
  const f = fixture(t);
  for (const config of [null, {}, { ffmpeg_binary: null }, { ffmpeg_binary: f.config.ffmpeg_binary, fallback: 'software' }, { ffmpeg_binary: { ...f.config.ffmpeg_binary, encoder: 'unreviewed' } }]) blocked(config);
});
test('changed byte and wrong pins never select another encoder', t => {
  const f = fixture(t);fs.writeFileSync(f.file, 'changed source');blocked(f.config);
  blocked({ ffmpeg_binary: { path: f.file, sha256: '*' } });
});
test('relative, absent and nonregular inputs produce a fixed redacted error', t => {
  const f = fixture(t);
  blocked({ ffmpeg_binary: { path: 'relative.bin', sha256: f.config.ffmpeg_binary.sha256 } });
  fs.unlinkSync(f.file);blocked(f.config);
  blocked({ ffmpeg_binary: { path: path.dirname(f.file), sha256: f.config.ffmpeg_binary.sha256 } });
});
test('simulated symlink and resolved-path redirection are rejected on every platform', t => {
  const f = fixture(t), original = fs.lstatSync, resolve = fs.realpathSync.native;
  try {
    fs.lstatSync = () => ({ isSymbolicLink: () => true });blocked(f.config);
    fs.lstatSync = original;fs.realpathSync.native = () => path.dirname(f.file);blocked(f.config);
  } finally { fs.lstatSync = original;fs.realpathSync.native = resolve; }
});
