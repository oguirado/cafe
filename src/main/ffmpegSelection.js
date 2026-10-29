// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

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

exports.verifyFfmpegSelection = verifyFfmpegSelection;
