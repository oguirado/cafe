// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {
  DEFAULT_FFMPEG_SHA256,
  defaultFfmpegCandidates,
  resolveDefaultFfmpeg,
  verifyFfmpegSelection,
} = require('../../src/main/ffmpegSelection');

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

function withApprovedPinForBytes(expectedBytes, body) {
  const original = crypto.createHash;
  crypto.createHash = function (algorithm) {
    const real = original.call(crypto, algorithm);
    const chunks = [];
    const update = real.update.bind(real);
    const digest = real.digest.bind(real);
    real.update = (chunk, ...rest) => {
      chunks.push(Buffer.from(chunk));
      return update(chunk, ...rest);
    };
    real.digest = (encoding) => {
      if (encoding === 'hex' && Buffer.concat(chunks).equals(expectedBytes)) {
        return DEFAULT_FFMPEG_SHA256;
      }
      return digest(encoding);
    };
    return real;
  };
  try {
    return body();
  } finally {
    crypto.createHash = original;
  }
}

test('default FFmpeg candidates follow declared precedence and match the approved pin', t => {
  assert.equal(DEFAULT_FFMPEG_SHA256, 'c6caa1894d3276556c84b36acc1d219d69f972c0a7525b23f1cad2bdf05c4231');
  assert.deepEqual(
    defaultFfmpegCandidates({ LOCALAPPDATA: 'D:\\Synthetic\\AppData\\Local', PATH: 'C:\\bin\\one;D:\\bin\\two' }),
    [
      'D:\\Synthetic\\AppData\\Local\\Cafe\\ffmpeg\\bin\\ffmpeg.exe',
      'C:\\bin\\one\\ffmpeg.exe',
      'D:\\bin\\two\\ffmpeg.exe',
    ],
  );
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-ffmpeg-default-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const localAppData = path.join(root, 'localappdata');
  const localBin = path.join(localAppData, 'Cafe', 'ffmpeg', 'bin');
  const pathFirst = path.join(root, 'path-first');
  const pathSecond = path.join(root, 'path-second');
  fs.mkdirSync(localBin, { recursive: true });
  fs.mkdirSync(pathFirst, { recursive: true });
  fs.mkdirSync(pathSecond, { recursive: true });
  const approvedBytes = Buffer.from('synthetic-pinned-ffmpeg');
  const localCandidate = path.join(localBin, 'ffmpeg.exe');
  const secondCandidate = path.join(pathSecond, 'ffmpeg.exe');
  fs.writeFileSync(localCandidate, approvedBytes);
  fs.writeFileSync(secondCandidate, approvedBytes);
  withApprovedPinForBytes(approvedBytes, () => {
    const firstMatch = resolveDefaultFfmpeg({ LOCALAPPDATA: localAppData, PATH: `${pathFirst};${pathSecond}` });
    assert.deepEqual(firstMatch, { file: localCandidate, sha256: DEFAULT_FFMPEG_SHA256, bytes: approvedBytes.length });
    fs.unlinkSync(localCandidate);
    const pathMatch = resolveDefaultFfmpeg({ LOCALAPPDATA: localAppData, PATH: `${pathFirst};${pathSecond}` });
    assert.deepEqual(pathMatch, { file: secondCandidate, sha256: DEFAULT_FFMPEG_SHA256, bytes: approvedBytes.length });
  });
});

test('default FFmpeg discovery rejects wrong hash, rejects symlinks and returns null when nothing matches', t => {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-ffmpeg-reject-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const localAppData = path.join(root, 'localappdata');
  const localBin = path.join(localAppData, 'Cafe', 'ffmpeg', 'bin');
  const pathSymlinkDir = path.join(root, 'path-symlink');
  const pathValidDir = path.join(root, 'path-valid');
  const targetDir = path.join(root, 'real-target');
  fs.mkdirSync(localBin, { recursive: true });
  fs.mkdirSync(pathSymlinkDir, { recursive: true });
  fs.mkdirSync(pathValidDir, { recursive: true });
  fs.mkdirSync(targetDir, { recursive: true });
  const approvedBytes = Buffer.from('synthetic-pinned-ffmpeg');
  // Wrong hash at %LOCALAPPDATA%\Cafe\ffmpeg\bin\ffmpeg.exe
  fs.writeFileSync(path.join(localBin, 'ffmpeg.exe'), 'wrong-hash-ffmpeg');
  // Symlink in first PATH directory pointing to a pin-matching file
  const realTarget = path.join(targetDir, 'ffmpeg.exe');
  fs.writeFileSync(realTarget, approvedBytes);
  fs.symlinkSync(realTarget, path.join(pathSymlinkDir, 'ffmpeg.exe'));
  withApprovedPinForBytes(approvedBytes, () => {
    // Only wrong-hash and symlink candidates exist -> nothing found (null)
    assert.equal(resolveDefaultFfmpeg({ LOCALAPPDATA: localAppData, PATH: pathSymlinkDir }), null);
    assert.equal(resolveDefaultFfmpeg({ LOCALAPPDATA: path.join(root, 'absent'), PATH: '' }), null);
    // Add valid regular file in later PATH entry -> skips wrong hash and symlink, selects valid file
    const validFile = path.join(pathValidDir, 'ffmpeg.exe');
    fs.writeFileSync(validFile, approvedBytes);
    const resolved = resolveDefaultFfmpeg({ LOCALAPPDATA: localAppData, PATH: `${pathSymlinkDir};${pathValidDir}` });
    assert.deepEqual(resolved, { file: validFile, sha256: DEFAULT_FFMPEG_SHA256, bytes: approvedBytes.length });
  });
});
