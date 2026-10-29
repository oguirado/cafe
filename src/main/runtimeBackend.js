// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const childProcess = require('node:child_process');
const { createBackendSupervisor } = require('./backendSupervisor');

/** @param {string} input @returns {string} */
function regular(input) {
  if (!path.isAbsolute(input)) throw new Error('AUTHORITY_PATH_REJECTED');
  const absolute = path.resolve(input);
  let current = path.parse(absolute).root;
  for (const component of absolute.slice(current.length).split(path.sep)) {
    current = path.join(current, component);
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('AUTHORITY_LINK_REJECTED');
  }
  const stat = fs.statSync(absolute);
  if (!stat.isFile() || stat.size === 0 || stat.size > 128 * 1024 * 1024) throw new Error('AUTHORITY_FILE_REJECTED');
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
  const receiptPath = regular(path.join(options.resourcesPath, 'cafe-build.json'));
  if (fs.statSync(receiptPath).size > 1024 * 1024) throw new Error('AUTHORITY_RECEIPT_REJECTED');
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8')); }
  catch { throw new Error('AUTHORITY_RECEIPT_REJECTED'); }
  if (receipt === null || typeof receipt !== 'object' ||
      receipt.schema !== 'cafe.unsigned-windows-app.v1' ||
      typeof receipt.authority_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(receipt.authority_sha256)) throw new Error('AUTHORITY_RECEIPT_REJECTED');
  const command = regular(path.join(options.resourcesPath, 'backend', 'cafe-core.exe'));
  if (digest(command) !== receipt.authority_sha256) throw new Error('AUTHORITY_HASH_REJECTED');
  // No inherited API credentials, OBS password, broker bootstrap or NODE_OPTIONS.
  /** @type {NodeJS.ProcessEnv} */
  const env = { CAFE_RUNTIME_ROOT: options.runtimeRoot };
  for (const key of ['SystemRoot', 'WINDIR', 'TEMP', 'TMP']) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  return createBackendSupervisor({
    command, args: [], authorizedOrigin: options.authorizedOrigin,
    getAuthorizedFrame: options.getAuthorizedFrame, maxLineBytes: 4096, maxQueueBytes: 16384,
    spawn: (executable, args, pipes) => childProcess.spawn(executable, args,
      { ...pipes, env, windowsHide: true }),
    // Raw stderr is counted by the supervisor, never forwarded/persisted.
  });
}
exports.createPackagedBackend = createPackagedBackend;

/** Native dialog result only, never a renderer input.
 * @param {string} file @returns {{path:string,sha256:string}} */
function selectExternalFfmpeg(file) {
  const selected = regular(file);
  return { path: selected, sha256: digest(selected) };
}
exports.selectExternalFfmpeg = selectExternalFfmpeg;
