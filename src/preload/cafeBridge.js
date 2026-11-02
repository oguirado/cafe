// @ts-check
'use strict';

/**
 * P07 preload bridge: narrow frozen facade over trusted host adapters.
 * Fixed channels only; no raw transport, no generic channel passthrough,
 * no PID or executable paths, no child lifecycle controls. Structural
 * JSDoc only; no platform imports.
 */

/**
 * @typedef {object} BridgeTransport
 * @property {(channel: string, payload?: unknown) => Promise<unknown>} invoke Trusted host invoke.
 * @property {(channel: string, listener: (value: unknown) => void) => () => void} subscribe Trusted host subscribe.
 */

/**
 * @typedef {object} BridgeStatus
 * @property {'starting'|'available'|'unavailable'} availability Lifecycle state.
 * @property {number|null} pid Owned child PID, or null before spawn.
 * @property {string} reason Failure reason, '' while healthy.
 * @property {string|null} authority_epoch Canonical u64 clock, or null.
 * @property {string|null} revision Canonical u64 clock, or null.
 * @property {object|null} state Latest projection state, or null.
 * @property {number} stderrBytes Total stderr bytes drained.
 */

/**
 * @typedef {object} CafeBridge
 * @property {() => Promise<BridgeStatus>} getSnapshot
 * @property {(intent: unknown) => Promise<unknown>} submitIntent
 * @property {(listener: (status: BridgeStatus) => void) => () => void} onSnapshot
 * @property {(request: unknown) => Promise<Readonly<Record<string, unknown>>>} previewSource
 */

const GET_SNAPSHOT_CHANNEL = 'cafe:getSnapshot';
const SUBMIT_INTENT_CHANNEL = 'cafe:submitIntent';
const SNAPSHOT_CHANNEL = 'cafe:snapshot';
const PREVIEW_SOURCE_CHANNEL = 'cafe:previewSource';
const PREVIEW_ERRORS = new Set(['preview_disabled', 'preview_denied', 'preview_unavailable', 'preview_busy', 'no_preview']);
const U64_MAX = 18446744073709551615n;

/**
 * True for canonical u64 decimal strings: "0" or ASCII digits without
 * leading zeros, within 0..=18446744073709551615. Never a JS number.
 * @param {unknown} value
 * @returns {boolean}
 */
function isCanonicalU64(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return false;
  }
  if (value.length > 1 && value.charCodeAt(0) === 48) {
    return false;
  }
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 48 || code > 57) {
      return false;
    }
  }
  /** @type {bigint} */
  let parsed = 0n;
  try {
    parsed = BigInt(value);
  } catch {
    return false;
  }
  return parsed >= 0n && parsed <= U64_MAX;
}

/**
 * True for non-array objects with Object.prototype or null prototype.
 * @param {unknown} value
 * @returns {boolean}
 */
function isPlainObject(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * Deep-clones JSON-shaped data, preserving numbers exactly.
 * @param {unknown} value
 * @returns {unknown}
 */
function deepCloneJson(value) {
  if (Array.isArray(value)) {
    /** @type {unknown[]} */
    const clonedItems = [];
    for (const item of value) {
      clonedItems.push(deepCloneJson(item));
    }
    return clonedItems;
  }
  if (isPlainObject(value)) {
    const record = /** @type {Record<string, unknown>} */ (value);
    /** @type {Record<string, unknown>} */
    const clonedRecord = {};
    for (const key of Object.keys(record)) {
      clonedRecord[key] = deepCloneJson(record[key]);
    }
    return clonedRecord;
  }
  return value;
}

/**
 * @param {unknown} value
 * @returns {void}
 */
function deepFreeze(value) {
  if (Array.isArray(value)) {
    for (const item of value) {
      deepFreeze(item);
    }
    Object.freeze(value);
    return;
  }
  if (isPlainObject(value)) {
    const record = /** @type {Record<string, unknown>} */ (value);
    for (const key of Object.keys(record)) {
      deepFreeze(record[key]);
    }
    Object.freeze(value);
  }
}

/**
 * Validates a host status projection before exposing it to the renderer
 * and returns a sanitized, frozen clone.
 * @param {unknown} value
 * @returns {BridgeStatus}
 */
function toBridgeStatus(value) {
  if (!isPlainObject(value)) {
    throw new Error('invalid snapshot: projection must be a plain object');
  }
  const record = /** @type {Record<string, unknown>} */ (value);
  const availability = record.availability;
  if (availability !== 'starting' && availability !== 'available' && availability !== 'unavailable') {
    throw new Error('invalid snapshot: unknown availability');
  }
  const pidValue = record.pid;
  if (pidValue !== null && (typeof pidValue !== 'number' || !Number.isInteger(pidValue))) {
    throw new Error('invalid snapshot: pid must be an integer or null');
  }
  if (typeof record.reason !== 'string') {
    throw new Error('invalid snapshot: reason must be a string');
  }
  const epochValue = record.authority_epoch;
  if (epochValue !== null && !isCanonicalU64(epochValue)) {
    throw new Error('invalid snapshot: authority_epoch must be a canonical u64 string or null');
  }
  const revisionValue = record.revision;
  if (revisionValue !== null && !isCanonicalU64(revisionValue)) {
    throw new Error('invalid snapshot: revision must be a canonical u64 string or null');
  }
  const stateValue = record.state;
  if (stateValue !== null && !isPlainObject(stateValue)) {
    throw new Error('invalid snapshot: state must be a plain object or null');
  }
  if (typeof record.stderrBytes !== 'number' || !Number.isFinite(record.stderrBytes)) {
    throw new Error('invalid snapshot: stderrBytes must be a finite number');
  }
  /** @type {BridgeStatus} */
  const status = {
    availability: /** @type {'starting'|'available'|'unavailable'} */ (availability),
    pid: /** @type {number|null} */ (pidValue),
    reason: record.reason,
    authority_epoch: /** @type {string|null} */ (epochValue),
    revision: /** @type {string|null} */ (revisionValue),
    state: stateValue === null ? null : /** @type {object} */ (deepCloneJson(stateValue)),
    stderrBytes: record.stderrBytes,
  };
  if (isPlainObject(status.state)) delete /** @type {Record<string, unknown>} */ (status.state).preview_sources;
  deepFreeze(status);
  return status;
}

/**
 * Creates the frozen renderer bridge over trusted host adapters.
 * @param {BridgeTransport} transport
 * @returns {CafeBridge}
 */
function createCafeBridge(transport) {
  if (typeof transport !== 'object' || transport === null) {
    throw new Error('createCafeBridge requires a transport object');
  }
  if (typeof transport.invoke !== 'function') {
    throw new Error('createCafeBridge requires an invoke function');
  }
  if (typeof transport.subscribe !== 'function') {
    throw new Error('createCafeBridge requires a subscribe function');
  }
  const invoke = transport.invoke;
  const subscribeTransport = transport.subscribe;

  /**
   * @returns {Promise<BridgeStatus>}
   */
  async function getSnapshot() {
    const value = await invoke(GET_SNAPSHOT_CHANNEL);
    return toBridgeStatus(value);
  }

  /**
   * @param {unknown} intent
   * @returns {Promise<unknown>}
   */
  async function submitIntent(intent) {
    return invoke(SUBMIT_INTENT_CHANNEL, intent);
  }

  /** Only a capsule/feed pair is accepted; source identities never cross preload.
   * @param {unknown} request @returns {Promise<Readonly<Record<string, unknown>>>} */
  async function previewSource(request) {
    if (!isPlainObject(request)) return Object.freeze({ ok: false, error: 'preview_denied' });
    const pair = /** @type {Record<string, unknown>} */ (request);
    if (Object.keys(pair).sort().join(',') !== 'capsule_id,feed_id'
        || typeof pair.capsule_id !== 'string' || pair.capsule_id !== pair.capsule_id.trim() || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(pair.capsule_id)
        || typeof pair.feed_id !== 'string' || pair.feed_id !== pair.feed_id.trim() || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(pair.feed_id)) {
      return Object.freeze({ ok: false, error: 'preview_denied' });
    }
    try {
      const response = await invoke(PREVIEW_SOURCE_CHANNEL, { capsule_id: pair.capsule_id, feed_id: pair.feed_id });
      if (isPlainObject(response)) {
        const value = /** @type {Record<string, unknown>} */ (response);
        const keys = Object.keys(value).sort().join(',');
        if (keys === 'capsule_id,feed_id,ok' && value.ok === true
            && value.capsule_id === pair.capsule_id && value.feed_id === pair.feed_id) {
          return Object.freeze({ ok: true, capsule_id: pair.capsule_id, feed_id: pair.feed_id });
        }
        if (keys === 'error,ok' && value.ok === false && typeof value.error === 'string' && PREVIEW_ERRORS.has(value.error)) {
          return Object.freeze({ ok: false, error: value.error });
        }
      }
    } catch { /* Provider errors are never returned to the renderer. */ }
    return Object.freeze({ ok: false, error: 'preview_unavailable' });
  }

  /**
   * @param {(status: BridgeStatus) => void} listener
   * @returns {() => void}
   */
  function onSnapshot(listener) {
    if (typeof listener !== 'function') {
      throw new Error('onSnapshot requires a listener function');
    }
    return subscribeTransport(SNAPSHOT_CHANNEL, (value) => {
      listener(toBridgeStatus(value));
    });
  }

  return Object.freeze({ getSnapshot, submitIntent, onSnapshot, previewSource });
}

module.exports = { createCafeBridge };
