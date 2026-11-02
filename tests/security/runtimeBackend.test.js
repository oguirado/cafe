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


test('external FFmpeg selection accepts the measured binary size with a streamed digest', () => fixture((resources, root) => {
  const file = path.join(root, 'synthetic-ffmpeg.exe');
  const fd = fs.openSync(file, 'wx');
  try { fs.ftruncateSync(fd, 164748800); } finally { fs.closeSync(fd); }
  const chosen = require('../../src/main/runtimeBackend').selectExternalFfmpeg(file);
  assert.equal(chosen.path, file);
  const hash = crypto.createHash('sha256'), block = Buffer.alloc(65536);
  let left = 164748800;
  while (left > 0) { const length = Math.min(left, block.length); hash.update(block.subarray(0, length)); left -= length; }
  assert.equal(chosen.sha256, hash.digest('hex'));
}));
test('the authority limit stays unchanged when external FFmpeg has a larger allowance', () => fixture((resources, root) => {
  const file = path.join(resources, 'backend', 'cafe-core.exe');
  fs.truncateSync(file, 128 * 1024 * 1024 + 1);
  assert.throws(() => createPackagedBackend(options(resources, root)), /AUTHORITY_FILE_REJECTED/);
}));
test('external FFmpeg rejects oversize and empty selections before hashing', () => fixture((resources, root) => {
  const file = path.join(root, 'synthetic-ffmpeg.exe');
  fs.writeFileSync(file, '');
  const select = require('../../src/main/runtimeBackend').selectExternalFfmpeg;
  assert.throws(() => select(file), /AUTHORITY_FILE_REJECTED/);
  fs.truncateSync(file, 512 * 1024 * 1024 + 1);
  assert.throws(() => select(file), /AUTHORITY_FILE_REJECTED/);
}));


test('optional native default is verified and child environment stays pruned', () => fixture((resources, root) => {
  const api = require('node:child_process'), original = api.spawn;
  const file = path.join(root, 'synthetic-default.exe'); fs.writeFileSync(file, 'synthetic external choice');
  const pin = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const receipt = path.join(resources, 'cafe-build.json'), data = JSON.parse(fs.readFileSync(receipt, 'utf8'));
  data.ffmpeg_binary = { path: fs.realpathSync.native(file), sha256: pin }; fs.writeFileSync(receipt, JSON.stringify(data));
  let captured;
  api.spawn = (_command, _args, options) => { captured = options.env; const child = new EventEmitter(); return Object.assign(child, { pid: undefined, stdout: new EventEmitter(), stderr: new EventEmitter(), stdin: Object.assign(new EventEmitter(), { write: () => true, writableLength: 0 }), kill: () => { throw new Error('unexpected signal'); } }); };
  try { const supervisor = createPackagedBackend(options(resources, root)); supervisor.start(); supervisor.dispose(); }
  finally { api.spawn = original; }
  assert.equal(captured.CAFE_FFMPEG_PATH, data.ffmpeg_binary.path); assert.equal(captured.CAFE_FFMPEG_SHA256, pin);
  assert.equal(captured.CAFE_FFMPEG_STARTUP_BLOCKED, undefined); assert.equal(captured.NODE_OPTIONS, undefined); assert.equal(captured.OPENAI_API_KEY, undefined); assert.equal(captured.PATH, undefined);
}));
test('invalid optional native default remains unconfigured without breaking manual setup', () => fixture((resources, root) => {
  const api = require('node:child_process'), original = api.spawn;
  const receipt = path.join(resources, 'cafe-build.json'), data = JSON.parse(fs.readFileSync(receipt, 'utf8'));
  data.ffmpeg_binary = { path: 'relative.exe', sha256: 'f'.repeat(64) }; fs.writeFileSync(receipt, JSON.stringify(data));
  let captured;
  api.spawn = (_command, _args, options) => { captured = options.env; const child = new EventEmitter(); return Object.assign(child, { pid: undefined, stdout: new EventEmitter(), stderr: new EventEmitter(), stdin: Object.assign(new EventEmitter(), { write: () => true, writableLength: 0 }), kill: () => { throw new Error('unexpected signal'); } }); };
  try { const supervisor = createPackagedBackend(options(resources, root)); supervisor.start(); supervisor.dispose(); }
  finally { api.spawn = original; }
  assert.equal(captured.CAFE_FFMPEG_PATH, undefined); assert.equal(captured.CAFE_FFMPEG_SHA256, undefined); assert.equal(captured.CAFE_FFMPEG_STARTUP_BLOCKED, '1');
}));

test('unconfigured FFmpeg probes default candidates for the approved pin and falls back to manual flow when absent, wrong-hash or symlinked', () => fixture((resources, root) => {
  const api = require('node:child_process'), originalSpawn = api.spawn;
  const originalCreateHash = crypto.createHash;
  const originalLocalAppData = process.env.LOCALAPPDATA;
  const originalPath = process.env.PATH;
  const approvedPin = 'c6caa1894d3276556c84b36acc1d219d69f972c0a7525b23f1cad2bdf05c4231';
  const approvedBytes = Buffer.from('synthetic-approved-default-ffmpeg');
  const realRoot = fs.realpathSync.native(root);
  const localAppData = path.join(realRoot, 'localappdata');
  const localBin = path.join(localAppData, 'Cafe', 'ffmpeg', 'bin');
  const symlinkDir = path.join(realRoot, 'path-symlink');
  const pathDir = path.join(realRoot, 'path-valid');
  const targetDir = path.join(realRoot, 'target-dir');
  fs.mkdirSync(localBin, { recursive: true });
  fs.mkdirSync(symlinkDir, { recursive: true });
  fs.mkdirSync(pathDir, { recursive: true });
  fs.mkdirSync(targetDir, { recursive: true });

  crypto.createHash = function (algorithm) {
    const real = originalCreateHash.call(crypto, algorithm);
    const chunks = [];
    const update = real.update.bind(real);
    const digest = real.digest.bind(real);
    real.update = (chunk, ...rest) => {
      chunks.push(Buffer.from(chunk));
      return update(chunk, ...rest);
    };
    real.digest = (encoding) => {
      if (encoding === 'hex' && Buffer.concat(chunks).equals(approvedBytes)) {
        return approvedPin;
      }
      return digest(encoding);
    };
    return real;
  };

  /** @returns {NodeJS.ProcessEnv} */
  const runAndCaptureEnv = () => {
    /** @type {NodeJS.ProcessEnv | undefined} */
    let captured;
    api.spawn = (_command, _args, spawnOptions) => {
      captured = spawnOptions.env;
      const child = new EventEmitter();
      return Object.assign(child, {
        pid: undefined,
        stdout: new EventEmitter(),
        stderr: new EventEmitter(),
        stdin: Object.assign(new EventEmitter(), { write: () => true, writableLength: 0 }),
        kill: () => { throw new Error('unexpected signal'); },
      });
    };
    const supervisor = createPackagedBackend(options(resources, root));
    supervisor.start();
    supervisor.dispose();
    if (!captured) throw new Error('missing captured env');
    return captured;
  };

  try {
    // 1. Nothing found -> manual flow (no env vars, not startup-blocked)
    process.env.LOCALAPPDATA = localAppData;
    process.env.PATH = `${symlinkDir};${pathDir}`;
    let env = runAndCaptureEnv();
    assert.equal(env.CAFE_FFMPEG_PATH, undefined);
    assert.equal(env.CAFE_FFMPEG_SHA256, undefined);
    assert.equal(env.CAFE_FFMPEG_STARTUP_BLOCKED, undefined);

    // 2. Wrong hash in LOCALAPPDATA and symlink in PATH -> both rejected, manual flow kept
    const localFfmpeg = path.join(localBin, 'ffmpeg.exe');
    fs.writeFileSync(localFfmpeg, 'wrong-hash-bytes');
    const symlinkTarget = path.join(targetDir, 'ffmpeg.exe');
    fs.writeFileSync(symlinkTarget, approvedBytes);
    fs.symlinkSync(symlinkTarget, path.join(symlinkDir, 'ffmpeg.exe'));
    env = runAndCaptureEnv();
    assert.equal(env.CAFE_FFMPEG_PATH, undefined);
    assert.equal(env.CAFE_FFMPEG_SHA256, undefined);
    assert.equal(env.CAFE_FFMPEG_STARTUP_BLOCKED, undefined);

    // 3. Pin match in PATH after wrong-hash LOCALAPPDATA and symlink PATH entry -> selects PATH match
    const pathFfmpeg = path.join(pathDir, 'ffmpeg.exe');
    fs.writeFileSync(pathFfmpeg, approvedBytes);
    env = runAndCaptureEnv();
    assert.equal(env.CAFE_FFMPEG_PATH, pathFfmpeg);
    assert.equal(env.CAFE_FFMPEG_SHA256, approvedPin);
    assert.equal(env.CAFE_FFMPEG_STARTUP_BLOCKED, undefined);

    // 4. Pin match in LOCALAPPDATA -> takes precedence over PATH
    fs.writeFileSync(localFfmpeg, approvedBytes);
    env = runAndCaptureEnv();
    assert.equal(env.CAFE_FFMPEG_PATH, localFfmpeg);
    assert.equal(env.CAFE_FFMPEG_SHA256, approvedPin);
    assert.equal(env.CAFE_FFMPEG_STARTUP_BLOCKED, undefined);
  } finally {
    api.spawn = originalSpawn;
    crypto.createHash = originalCreateHash;
    if (originalLocalAppData === undefined) delete process.env.LOCALAPPDATA;
    else process.env.LOCALAPPDATA = originalLocalAppData;
    if (originalPath === undefined) delete process.env.PATH;
    else process.env.PATH = originalPath;
  }
}));
