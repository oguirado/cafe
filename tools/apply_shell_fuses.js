// @ts-check
'use strict';
const { peExecutable, inspectShellFuses, REQUIRED } = require('./check_shell_fuses');

/**
 * Only the official API changes fuse bytes. Non-policy Windows fuse states
 * are retained from this exact executable, never copied from a Linux build.
 * @param {string} binary
 */
async function applyShellFuses(binary) {
  const file = peExecutable(binary);
  const api = await import('@electron/fuses');
  const previous = await api.getCurrentFuseWire(file);
  if (previous.version !== api.FuseVersion.V1) throw new Error('FUSE_LAYOUT_REJECTED');
  /** @type {Record<string, unknown>} */
  const config = { version: api.FuseVersion.V1, strictlyRequireAllFuses: true };
  const names = Object.keys(api.FuseV1Options).filter((key) => Number.isNaN(Number(key)));
  for (const name of names) {
    const option = /** @type {any} */ (api.FuseV1Options)[name];
    const actual = /** @type {any} */ (previous)[option];
    if (actual !== api.FuseState.ENABLE && actual !== api.FuseState.DISABLE) throw new Error('FUSE_LAYOUT_REJECTED');
    config[String(option)] = Object.hasOwn(REQUIRED, name) ?
      /** @type {any} */ (REQUIRED)[name] : actual === api.FuseState.ENABLE;
  }
  await api.flipFuses(file, /** @type {import('@electron/fuses').FuseConfig} */ (/** @type {unknown} */ (config)));
  const report = await inspectShellFuses(file);
  if (report.status !== 'pass') throw new Error('FUSE_POLICY_REJECTED');
  const after = await api.getCurrentFuseWire(file);
  for (const name of names) {
    if (!Object.hasOwn(REQUIRED, name)) {
      const option = /** @type {any} */ (api.FuseV1Options)[name];
      if (/** @type {any} */ (after)[option] !== /** @type {any} */ (previous)[option]) throw new Error('UNREQUESTED_FUSE_CHANGE');
    }
  }
  return report;
}

/** @param {string[]} args */
async function main(args) {
  try {
    if (args.length !== 1) throw new Error('BINARY_REJECTED');
    const report = await applyShellFuses(args[0]);
    process.stdout.write(JSON.stringify(report) + '\n');
    return 0;
  } catch {
    process.stdout.write(JSON.stringify({ schema: 'cafe.shell-fuses.v1', status: 'fail', code: 'FUSE_APPLY_REJECTED' }) + '\n');
    return 1;
  }
}

exports.applyShellFuses = applyShellFuses;
exports.main = main;
if (require.main === module) main(process.argv.slice(2)).then((rc) => { process.exitCode = rc; });
