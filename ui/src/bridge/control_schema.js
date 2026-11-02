// @ts-check
/**
 * Closed presentation data for one pending control proposal.
 * This module neither approves a proposal nor validates a native capability.
 * The native authority owns identity clocks, expiry and effect admission.
 */

/**
 * @typedef {object} ProposalView
 * @property {string} proposal_id Opaque proposal identifier.
 * @property {'bind_window'|'start_recording'|'stop_recording'|'send_to_obs'} op
 * @property {string} capsule_id Opaque identifier in the capsule namespace.
 * @property {string|null} slot Caller-supplied session alias, never inferred.
 * @property {'game'|'camera'|null} role Null means no role for non-bind operations.
 * @property {number} expires_at_ms Safe integer millisecond deadline.
 */

/**
 * @typedef {{ok: true, view: Readonly<ProposalView>}|{ok: false, view: null}} ProposalViewResult
 */

const VIEW_KEYS = Object.freeze([
  'proposal_id', 'op', 'capsule_id', 'slot', 'role', 'expires_at_ms',
]);

// Unlike $, the final lookahead cannot accept a trailing line terminator.
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}(?![\s\S])/;
const SLOT = /^slot-[0-9]+(?![\s\S])/;

/** @param {unknown} value @returns {value is string} */
function identifier(value) {
  return typeof value === 'string' && IDENTIFIER.test(value);
}

/** @param {unknown} value @returns {value is number} */
function milliseconds(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

/** @param {unknown} value @returns {value is ProposalView['op']} */
function operation(value) {
  return value === 'bind_window' || value === 'start_recording'
    || value === 'stop_recording' || value === 'send_to_obs';
}

/**
 * Validate a complete untrusted view without coercion or partial output.
 *
 * All six own enumerable data fields are required. Extra properties, symbols,
 * accessors and inherited records are rejected. Read descriptors once so input
 * getters cannot run while constructing the detached immutable result.
 *
 * These are numeric presentation milliseconds, not the native decimal-string
 * u64 clocks. Unknown clocks stay unknown. now_ms only inhibits expired views;
 * a successful result never establishes native availability or authorization.
 * The native proposal queue independently enforces its maximum lifetime.
 *
 * @param {unknown} value
 * @param {unknown} now_ms
 * @returns {ProposalViewResult}
 */
export function validateProposalView(value, now_ms) {
  try {
    if (!milliseconds(now_ms) || value === null || typeof value !== 'object') {
      return { ok: false, view: null };
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      return { ok: false, view: null };
    }
    const descriptors = Object.getOwnPropertyDescriptors(value);
    const keys = Reflect.ownKeys(descriptors);
    if (keys.length !== VIEW_KEYS.length
        || keys.some((key) => typeof key !== 'string' || !VIEW_KEYS.includes(key))) {
      return { ok: false, view: null };
    }
    /** @type {Record<string, unknown>} */
    const fields = Object.create(null);
    for (const key of VIEW_KEYS) {
      const descriptor = descriptors[key];
      if (!descriptor || !descriptor.enumerable
          || !Object.prototype.hasOwnProperty.call(descriptor, 'value')) {
        return { ok: false, view: null };
      }
      fields[key] = descriptor.value;
    }
    const { proposal_id, op, capsule_id, slot, role, expires_at_ms } = fields;
    if (!identifier(proposal_id) || !identifier(capsule_id) || !operation(op)
        || !(slot === null || (typeof slot === 'string' && SLOT.test(slot)))
        || !milliseconds(expires_at_ms) || expires_at_ms <= now_ms) {
      return { ok: false, view: null };
    }
    if (op === 'bind_window' ? role !== 'game' && role !== 'camera' : role !== null) {
      return { ok: false, view: null };
    }
    /** @type {ProposalView} */
    const view = {
      proposal_id, op, capsule_id, slot,
      role: op === 'bind_window' ? /** @type {'game'|'camera'} */ (role) : null,
      expires_at_ms,
    };
    return { ok: true, view: Object.freeze(view) };
  } catch {
    // Malformed reflective input never becomes an exception or public text.
    return { ok: false, view: null };
  }
}
