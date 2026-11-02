// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const DEFAULT_FFMPEG_SHA256 = 'c6caa1894d3276556c84b36acc1d219d69f972c0a7525b23f1cad2bdf05c4231';

/**
 * Host-only user selection, never a download, bundled default or license claim.
 * The Rust preflight still approves the exact encoder and production policy.
 * @param {unknown} config
 * @returns {Readonly<{file: string, sha256: string, bytes: number}>}
 */
function verifyFfmpegSelection(config) {
  let fd;
  try {
    if (!config || typeof config !== 'object' || Array.isArray(config) ||
        Object.keys(config).join(',') !== 'ffmpeg_binary') throw new Error();
    const selected = /** @type {{ffmpeg_binary: unknown}} */ (config).ffmpeg_binary;
    if (!selected || typeof selected !== 'object' || Array.isArray(selected)) throw new Error();
    const row = /** @type {Record<string, unknown>} */ (selected);
    if (Object.keys(row).sort().join(',') !== 'path,sha256' || typeof row.path !== 'string' ||
        typeof row.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.sha256) ||
        !path.isAbsolute(row.path) || path.normalize(row.path) !== row.path || row.path.includes('\0')) throw new Error();
    if (fs.realpathSync.native(row.path) !== row.path) throw new Error();
    let current = row.path;
    for (;;) {
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error();
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
    fd = fs.openSync(row.path, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const before = fs.fstatSync(fd);
    if (!before.isFile() || before.size === 0 || before.size > 512 * 1024 * 1024) throw new Error();
    const hash = crypto.createHash('sha256'), buffer = Buffer.alloc(1024 * 1024);
    let total = 0;
    for (;;) {
      const count = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (count === 0) break;
      total += count;
      if (total > before.size) throw new Error();
      hash.update(buffer.subarray(0, count));
    }
    const after = fs.fstatSync(fd), actual = hash.digest('hex');
    if (actual !== row.sha256 || total !== before.size || before.size !== after.size ||
        before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) throw new Error();
    return Object.freeze({ file: row.path, sha256: actual, bytes: total });
  } catch {
    throw new Error('FFMPEG_SELECTION_BLOCKED');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

/**
 * Ordered default probe locations when no explicit FFmpeg selection is configured:
 * %LOCALAPPDATA%\Cafe\ffmpeg\bin\ffmpeg.exe, then each directory of the machine PATH.
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string[]}
 */
function defaultFfmpegCandidates(env = process.env) {
  /** @type {string[]} */
  const candidates = [];
  const source = env && typeof env === 'object' ? env : {};
  const localAppData = typeof source.LOCALAPPDATA === 'string' ? source.LOCALAPPDATA.trim() : '';
  if (localAppData.length > 0) {
    const joiner = /^[A-Za-z]:[\\/]/.test(localAppData) || localAppData.includes('\\') ? path.win32 : path;
    candidates.push(joiner.join(localAppData, 'Cafe', 'ffmpeg', 'bin', 'ffmpeg.exe'));
  }
  const rawPath = typeof source.PATH === 'string'
    ? source.PATH
    : (typeof source.Path === 'string' ? source.Path : '');
  if (rawPath.length > 0) {
    const delimiter = path.sep === '\\' || rawPath.includes(';') ? ';' : path.delimiter;
    for (const part of rawPath.split(delimiter)) {
      const dir = part.trim().replace(/^"+|"+$/g, '').trim();
      if (dir.length > 0) {
        const joiner = /^[A-Za-z]:[\\/]/.test(dir) || dir.includes('\\') ? path.win32 : path;
        candidates.push(joiner.join(dir, 'ffmpeg.exe'));
      }
    }
  }
  return candidates;
}

/**
 * Selects the first regular non-symlink default candidate matching the approved pin.
 * Returns null when no candidate matches so manual selection remains available.
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {Readonly<{file: string, sha256: string, bytes: number}> | null}
 */
function resolveDefaultFfmpeg(env = process.env) {
  for (const candidate of defaultFfmpegCandidates(env)) {
    try {
      return verifyFfmpegSelection({
        ffmpeg_binary: { path: candidate, sha256: DEFAULT_FFMPEG_SHA256 },
      });
    } catch {
      // Candidate absent, symlinked, or wrong hash; continue in order.
    }
  }
  return null;
}

exports.DEFAULT_FFMPEG_SHA256 = DEFAULT_FFMPEG_SHA256;
exports.defaultFfmpegCandidates = defaultFfmpegCandidates;
exports.resolveDefaultFfmpeg = resolveDefaultFfmpeg;
exports.verifyFfmpegSelection = verifyFfmpegSelection;
