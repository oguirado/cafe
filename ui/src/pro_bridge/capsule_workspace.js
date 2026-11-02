// @ts-check
/**
 * Closed presentation rows. Identities are opaque and clocks stay decimal
 * strings. Slots are caller-owned session aliases, never authorization keys.
 * The parent must supply the current session and authoritative generations;
 * this validator neither constructs identities nor reads a host bridge.
 * @typedef {{feed_id:string, role:'game'|'camera', generation:string}} CapsuleFeed
 * @typedef {{capsule_id:string, slot:string|null, person_label:string|null,
 *   generation:string, feeds:CapsuleFeed[]}} CapsuleRow
 */

const MAX_CLOCK = '18446744073709551615';
/** @param {unknown} value @returns {value is string} */
function identifier(value) {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value);
}
/** @param {unknown} value @returns {value is string} */
function clock(value) {
  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) &&
    (value.length < MAX_CLOCK.length || value <= MAX_CLOCK);
}
/** Read only own, enumerable data fields; accessors never get invoked.
 * @param {unknown} value @param {string[]} keys
 * @returns {Record<string, unknown>|null} */
function fields(value, keys) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return null;
  if (Reflect.ownKeys(value).length !== keys.length) return null;
  /** @type {Record<string, unknown>} */
  const result = Object.create(null);
  for (const key of keys) {
    const field = Object.getOwnPropertyDescriptor(value, key);
    if (!field || !field.enumerable || !Object.hasOwn(field, 'value')) return null;
    result[key] = field.value;
  }
  return result;
}
/** Reject sparse arrays, extra properties, subclasses and index accessors.
 * @param {unknown} value @param {number} maximum @returns {unknown[]|null} */
function rows(value, maximum) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
  const length = Object.getOwnPropertyDescriptor(value, 'length')?.value;
  if (!Number.isInteger(length) || length < 0 || length > maximum ||
      Reflect.ownKeys(value).length !== length + 1) return null;
  /** @type {unknown[]} */
  const result = [];
  for (let index = 0; index < length; index += 1) {
    const entry = Object.getOwnPropertyDescriptor(value, String(index));
    if (!entry || !entry.enumerable || !Object.hasOwn(entry, 'value')) return null;
    result.push(entry.value);
  }
  return result;
}

/**
 * Validate the entire capsules input, preserving the existing controller's
 * two-capsule/two-feed bounds. Invalid input refuses the whole snapshot, never
 * a partial success. The returned rows are detached, closed value objects.
 * No caller getter, exception text, identifier prefix or array position is
 * used to infer identity. Session tombstones belong to the parent session.
 * @param {unknown} value
 * @returns {{ok:boolean, rows:CapsuleRow[]}}
 */
export function validateCapsules(value) {
  /** @returns {{ok:false, rows:CapsuleRow[]}} */
  const refuse = () => ({ ok: false, rows: [] });
  try {
    const input = rows(value, 2);
    if (input === null) return refuse();
    const identities = new Set();
    const slots = new Set();
    /** @type {CapsuleRow[]} */
    const normalized = [];
    for (const candidate of input) {
      const row = fields(candidate, ['capsule_id', 'slot', 'person_label', 'generation', 'feeds']);
      if (!row || !identifier(row.capsule_id) || identities.has(row.capsule_id) ||
          !clock(row.generation) ||
          !(row.person_label === null || typeof row.person_label === 'string') ||
          !(row.slot === null || typeof row.slot === 'string' && /^slot-[0-9]+$/.test(row.slot))) {
        return refuse();
      }
      identities.add(row.capsule_id);
      if (row.slot !== null) {
        if (slots.has(row.slot)) return refuse();
        slots.add(row.slot);
      }
      const inputFeeds = rows(row.feeds, 2);
      if (inputFeeds === null) return refuse();
      const roles = new Set();
      /** @type {CapsuleFeed[]} */
      const feeds = [];
      for (const candidateFeed of inputFeeds) {
        const feed = fields(candidateFeed, ['feed_id', 'role', 'generation']);
        if (!feed || !identifier(feed.feed_id) || identities.has(feed.feed_id) ||
            (feed.role !== 'game' && feed.role !== 'camera') || roles.has(feed.role) ||
            !clock(feed.generation)) return refuse();
        identities.add(feed.feed_id);
        roles.add(feed.role);
        feeds.push({ feed_id: feed.feed_id, role: feed.role, generation: feed.generation });
      }
      normalized.push({ capsule_id: row.capsule_id, slot: row.slot,
        person_label: row.person_label, generation: row.generation, feeds });
    }
    return { ok: true, rows: normalized };
  } catch {
    return refuse();
  }
}
