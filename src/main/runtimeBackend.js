// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const childProcess = require('node:child_process');
const { createBackendSupervisor } = require('./backendSupervisor');

/** @param {string} input @param {number} maximumBytes @returns {string} */
function regular(input, maximumBytes) {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes <= 0 || maximumBytes > 512 * 1024 * 1024) throw new Error('AUTHORITY_FILE_REJECTED');
  if (!path.isAbsolute(input)) throw new Error('AUTHORITY_PATH_REJECTED');
  const absolute = path.resolve(input);
  let current = path.parse(absolute).root;
  for (const component of absolute.slice(current.length).split(path.sep)) {
    current = path.join(current, component);
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('AUTHORITY_LINK_REJECTED');
  }
  const stat = fs.statSync(absolute);
  if (!stat.isFile() || stat.size === 0 || stat.size > maximumBytes) throw new Error('AUTHORITY_FILE_REJECTED');
  return absolute;
}
/** @param {string} file @returns {string} */
function digest(file) {
  const hash = crypto.createHash('sha256');
  const fd = fs.openSync(file, 'r');
  try {
    const buffer = Buffer.alloc(65536);
    for (;;) {
      const count = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (count === 0) break;
      hash.update(buffer.subarray(0, count));
    }
    return hash.digest('hex');
  } finally { fs.closeSync(fd); }
}

/** Host-only factory; renderer content never supplies executable, argv, root
 * or environment. Its result is created once by the single-instance shell.
 * @param {{resourcesPath:string,runtimeRoot:string,authorizedOrigin:string,getAuthorizedFrame:()=>object}} options
 * @returns {ReturnType<typeof createBackendSupervisor>}
 */
function createPackagedBackend(options) {
  if (!path.isAbsolute(options.resourcesPath) || !path.isAbsolute(options.runtimeRoot)) throw new Error('AUTHORITY_PATH_REJECTED');
  const receiptPath = regular(path.join(options.resourcesPath, 'cafe-build.json'), 128 * 1024 * 1024);
  if (fs.statSync(receiptPath).size > 1024 * 1024) throw new Error('AUTHORITY_RECEIPT_REJECTED');
  /** @type {Record<string, any>} */
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8')); }
  catch { throw new Error('AUTHORITY_RECEIPT_REJECTED'); }
  if (receipt === null || typeof receipt !== 'object' ||
      receipt.schema !== 'cafe.unsigned-windows-app.v1' ||
      typeof receipt.authority_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(receipt.authority_sha256)) throw new Error('AUTHORITY_RECEIPT_REJECTED');
  const command = regular(path.join(options.resourcesPath, 'backend', 'cafe-core.exe'), 128 * 1024 * 1024);
  if (digest(command) !== receipt.authority_sha256) throw new Error('AUTHORITY_HASH_REJECTED');
  // No inherited API credentials, OBS password, broker bootstrap or NODE_OPTIONS.
  /** @type {NodeJS.ProcessEnv} */
  const env = { CAFE_RUNTIME_ROOT: options.runtimeRoot };
  for (const key of ['SystemRoot', 'WINDIR', 'TEMP', 'TMP']) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  // Optional native provisioning; selection paths never enter renderer state.
  const ffmpegSelection = require('./ffmpegSelection');
  if (receipt.ffmpeg_binary !== undefined) {
    try {
      const selected = ffmpegSelection.verifyFfmpegSelection({ ffmpeg_binary: receipt.ffmpeg_binary });
      env.CAFE_FFMPEG_PATH = selected.file;
      env.CAFE_FFMPEG_SHA256 = selected.sha256;
    } catch {
      env.CAFE_FFMPEG_STARTUP_BLOCKED = '1';
    }
  } else {
    const selected = ffmpegSelection.resolveDefaultFfmpeg(process.env);
    if (selected !== null) {
      env.CAFE_FFMPEG_PATH = selected.file;
      env.CAFE_FFMPEG_SHA256 = selected.sha256;
    }
  }
  // The approved private Python sink pins directories and writes no raw data.
  function openTraceSink() {
    const manifestFile = regular(path.join(options.resourcesPath, 'runtime-manifest.json'), 8 * 1024 * 1024);
    const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
    if (manifest.version !== '3.13.16+20261013' || manifest.archive_sha256 !== '7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06') throw new Error('TRACE_RUNTIME_REJECTED');
    const python = regular(path.join(options.resourcesPath, 'python', 'python.exe'), 128 * 1024 * 1024);
    if (digest(python) !== '9b0ac461f54e03a8385db26269226acbd68d14df3c9b58fee9866d8a0d996534' || manifest.files?.['python/python.exe']?.sha256 !== '9b0ac461f54e03a8385db26269226acbd68d14df3c9b58fee9866d8a0d996534') throw new Error('TRACE_RUNTIME_REJECTED');
    for (const name of ['connectors/cafe/logging_storage.py', 'connectors/cafe/runtime_logging.py', 'src/contracts/runtime_logging.json']) {
      const file = regular(path.join(options.resourcesPath, name), 256 * 1024);
      if (digest(file) !== receipt.source_files?.[name]) throw new Error('TRACE_SOURCE_REJECTED');
    }
    return childProcess.spawn(python, ['-I', '-B', path.join(options.resourcesPath, 'connectors/cafe/logging_storage.py'), '--root', path.join(path.dirname(options.runtimeRoot), 'diagnostics')],
      {stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true, env});
  }
  const trace = require('./runtime_logging').createRuntimeTrace({openSink: openTraceSink, baseCommit: receipt.base_commit, authoritySha256: receipt.authority_sha256});
  const supervisor = createBackendSupervisor({
    command, args: [], authorizedOrigin: options.authorizedOrigin,
    getAuthorizedFrame: options.getAuthorizedFrame, maxLineBytes: 32768, maxQueueBytes: 131072,
    onStatus: trace.snapshot, onIntent: trace.intent, onStderr: trace.stderr,
    spawn: (executable, args, pipes) => childProcess.spawn(executable, args,
      { ...pipes, env, windowsHide: true }),
    // Raw stderr is discarded; only schema-approved native stages persist.
  });
  let started = false;
  const start = supervisor.start, dispose = supervisor.dispose;
  supervisor.start = () => {if (!started) {started = true; trace.start();} return start();};
  supervisor.dispose = () => {try {dispose();} finally {trace.close();}};
  return supervisor;
}
exports.createPackagedBackend = createPackagedBackend;

/** Native dialog result only, never a renderer input.
 * @param {string} file @returns {{path:string,sha256:string}} */
function selectExternalFfmpeg(file) {
  const selected = regular(file, 512 * 1024 * 1024);
  return { path: selected, sha256: digest(selected) };
}
exports.selectExternalFfmpeg = selectExternalFfmpeg;
