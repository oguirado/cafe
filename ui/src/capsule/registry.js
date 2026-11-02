/** @file ui/src/capsule/registry.js: stable slot-N aliases for one authority lifetime. */
// Pure: no bridge/IPC/MCP. Create one registry per authority epoch; never reuse across epochs.
/** Default slot ceiling per lifetime. */
export const MAX_SLOTS = 1000;
export const ERR_INVALID_ID = 'ERR_INVALID_CAPSULE_ID';
export const ERR_OVERFLOW = 'ERR_SLOT_OVERFLOW';
export const ERR_INVALID_OPTIONS = 'ERR_INVALID_OPTIONS';
/**
 * @param {string} message
 * @param {string} code
 * @returns {Error & { code: string }}
 */
function fail(message, code) {
  const err = /** @type {Error & { code: string }} */ (new Error(message));
  err.code = code;
  return err;
}
/**
 * @param {unknown} value
 * @returns {string}
 */
function assertId(value) {
  if (typeof value !== 'string' || value.length === 0 || value !== value.trim() || /\s/.test(value) || /^\d+$/.test(value)) {
    throw fail('CapsuleRegistry: invalid capsule id', ERR_INVALID_ID);
  }
  return value;
}
/**
 * Monotonic registry: reorder/delete-readd retain aliases; overflow/invalid reject.
 * @param {{ maxSlots?: number }} [options]
 */
export function createRegistry(options = {}) {
  if (typeof options !== 'object' || options === null || Array.isArray(options)) {
    throw fail('CapsuleRegistry: invalid options', ERR_INVALID_OPTIONS);
  }
  let max = MAX_SLOTS;
  if (options.maxSlots !== undefined) {
    if (!Number.isSafeInteger(options.maxSlots) || options.maxSlots <= 0) {
      throw fail('CapsuleRegistry: invalid maxSlots', ERR_INVALID_OPTIONS);
    }
    max = options.maxSlots;
  }
  /** @type {Map<string, number>} */
  const aliases = new Map();
  /** @type {string[]} */
  let active = [];
  let next = 1;
  return Object.freeze({
    /**
     * @param {readonly unknown[]} ids authoritative order for display
     * @returns {string[]}
     */
    assign(ids) {
      if (!Array.isArray(ids)) throw fail('CapsuleRegistry: ids must be an array', ERR_INVALID_ID);
      /** @type {string[]} */
      const ordered = [];
      const seen = new Set();
      for (const value of ids) { const id = assertId(value); if (!seen.has(id)) { seen.add(id); ordered.push(id); } }
      const fresh = ordered.filter((id) => !aliases.has(id)).length;
      if (next + fresh - 1 > max) throw fail('CapsuleRegistry: slot overflow', ERR_OVERFLOW);
      for (const id of ordered) { if (!aliases.has(id)) { aliases.set(id, next); next += 1; } }
      active = ordered.slice();
      return active.slice();
    },
    /** @param {unknown} id */
    remove(id) { assertId(id); active = active.filter((v) => v !== id); },
    /** @param {unknown} id @returns {number|null} */
    aliasFor(id) { const valid = assertId(id); return aliases.has(valid) ? aliases.get(valid) : null; },
    /** @param {unknown} id @returns {string|null} */
    displayFor(id) { const valid = assertId(id); return aliases.has(valid) ? `slot-${aliases.get(valid)}` : null; },
    /** @returns {string[]} */
    activeIds() { return active.slice(); },
  });
}