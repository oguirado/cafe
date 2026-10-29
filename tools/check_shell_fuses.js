// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const REQUIRED = Object.freeze({
  RunAsNode: false,
  EnableNodeOptionsEnvironmentVariable: false,
  EnableNodeCliInspectArguments: false,
  EnableEmbeddedAsarIntegrityValidation: true,
  OnlyLoadAppFromAsar: true,
  GrantFileProtocolExtraPrivileges: false,
});

/** @param {unknown} value @returns {string} */
function peExecutable(value) {
  if (typeof value !== 'string' || value.includes(String.fromCharCode(0))) throw new Error('BINARY_REJECTED');
  const target = path.resolve(value);
  if (fs.realpathSync(target) !== target || !fs.lstatSync(target).isFile()) throw new Error('BINARY_REJECTED');
  const fd = fs.openSync(target, 'r');
  try {
    const dos = Buffer.alloc(64);
    if (fs.readSync(fd, dos, 0, 64, 0) !== 64 || dos.toString('ascii', 0, 2) !== 'MZ') throw new Error('BINARY_REJECTED');
    const offset = dos.readUInt32LE(60);
    const header = Buffer.alloc(6);
    if (fs.readSync(fd, header, 0, 6, offset) !== 6 || !header.subarray(0, 4).equals(Buffer.from([80, 69, 0, 0])) || header.readUInt16LE(4) !== 0x8664) throw new Error('BINARY_REJECTED');
  } finally { fs.closeSync(fd); }
  return target;
}

/** @param {string} binary */
async function inspectShellFuses(binary) {
  const file = peExecutable(binary);
  const api = await import('@electron/fuses');
  const wire = await api.getCurrentFuseWire(file);
  if (wire.version !== api.FuseVersion.V1) throw new Error('FUSE_LAYOUT_REJECTED');
  const checks = Object.entries(REQUIRED).map(([name, expected]) => {
    const option = /** @type {any} */ (api.FuseV1Options)[name];
    const actual = /** @type {any} */ (wire)[option];
    const required = expected ? api.FuseState.ENABLE : api.FuseState.DISABLE;
    return { name, expected, actual, pass: actual === required };
  });
  const pass = checks.every((check) => check.pass);
  return { schema: 'cafe.shell-fuses.v1', status: pass ? 'pass' : 'fail',
    code: pass ? 'FUSES_VERIFIED' : 'FUSE_POLICY_REJECTED', checks };
}


/** @param {string} archive */
async function inspectAsar(archive) {
  const target = path.resolve(archive);
  if (fs.realpathSync(target) !== target || !fs.lstatSync(target).isFile()) throw new Error('ASAR_CONFIG_REJECTED');
  const asar = await import('@electron/asar');
  const required = ['package.json', 'src/main/index.js', 'src/main/shellSecurity.js',
    'src/main/backendSupervisor.js', 'dist/preload/index.cjs'];
  for (const name of required) {
    const row = asar.statFile(target, name, false);
    if ('link' in row || 'files' in row || row.unpacked === true || !('size' in row) || row.size > 1024 * 1024) throw new Error('ASAR_CONFIG_REJECTED');
  }
  const manifest = JSON.parse(asar.extractFile(target, 'package.json', false).toString('utf8'));
  if (manifest.main !== 'src/main/index.js') throw new Error('ASAR_CONFIG_REJECTED');
  const header = asar.getRawHeader(target);
  const hash = require('node:crypto').createHash('sha256').update(header.headerString).digest('hex');
  return { status: 'pass', main: manifest.main, required_packed_files: required,
    header_sha256: hash, unpacked_main_fallback: false };
}

/** @param {string} binary @param {string} archive */
async function inspectShellPackage(binary, archive) {
  const asar = await inspectAsar(archive);
  const fuses = await inspectShellFuses(binary);
  return { ...fuses, schema: 'cafe.shell-package.v1', structure: 'pass', asar };
}

/** @param {string[]} args */
async function main(args) {
  try {
    if (args.length !== 1 && !(args.length === 3 && args[1] === '--asar')) throw new Error('BINARY_REJECTED');
    const result = args.length === 1 ? await inspectShellFuses(args[0]) : await inspectShellPackage(args[0], args[2]);
    process.stdout.write(JSON.stringify(result) + '\n');
    return result.status === 'pass' ? 0 : 1;
  } catch {
    process.stdout.write(JSON.stringify({ schema: 'cafe.shell-fuses.v1', status: 'fail', code: 'BINARY_OR_FUSE_LAYOUT_REJECTED' }) + '\n');
    return 1;
  }
}

exports.REQUIRED = REQUIRED;
exports.peExecutable = peExecutable;
exports.inspectShellFuses = inspectShellFuses;
exports.inspectAsar = inspectAsar;
exports.inspectShellPackage = inspectShellPackage;
exports.main = main;
if (require.main === module) main(process.argv.slice(2)).then((rc) => { process.exitCode = rc; });
