// @ts-check
'use strict';

/**
 * P07 backend supervisor: host-controlled boundary that owns exactly one
 * native authority child process over anonymous JSONL pipes and exposes a
 * closed, origin-checked IPC surface. Structural JSDoc only; no platform
 * imports, no renderer-controlled spawn inputs, no lifecycle on the renderer.
 */

/**
 * @typedef {object} SupervisorCaller
 * @property {string} origin Exact origin of the calling frame.
 * @property {object} frame Calling frame object reference.
 * @property {boolean} isMainFrame True only for the main frame.
 */

/**
 * @typedef {object} SupervisorStatus
 * @property {'starting'|'available'|'unavailable'} availability Lifecycle state.
 * @property {number|null} pid Owned child PID, or null before spawn.
 * @property {string} reason Failure reason, '' while healthy.
 * @property {string|null} authority_epoch Canonical u64 clock, or null.
 * @property {string|null} revision Canonical u64 clock, or null.
 * @property {object|null} state Latest projection state, or null.
 * @property {number} stderrBytes Total stderr bytes drained.
 */

/**
 * @typedef {object} QueuedReceipt
 * @property {boolean} queued Always true when returned.
 */

/**
 * Adapter-boundary event source. Listener payloads are untyped here and
 * every byte received is validated at runtime before use.
 * @typedef {object} EventSource
 * @property {(event: string, listener: (...args: any[]) => void) => void} on
 */

/**
 * Adapter-boundary writable stdin. Listener payloads are validated at runtime.
 * @typedef {object} StdinAdapter
 * @property {(chunk: string) => boolean} write
 * @property {(event: string, listener: (...args: any[]) => void) => void} on
 * @property {number} writableLength Bytes buffered inside the stream.
 */

/**
 * Minimal structural child handle returned by the injected spawn.
 * @typedef {object} ChildAdapter
 * @property {number|undefined} pid
 * @property {EventSource} stdout
 * @property {EventSource} stderr
 * @property {StdinAdapter} stdin
 * @property {(event: string, listener: (...args: any[]) => void) => void} on
 * @property {() => boolean} kill
 */

/**
 * @typedef {object} SpawnPipeOptions
 * @property {['pipe','pipe','pipe']} stdio
 * @property {false} shell
 */

/**
 * @typedef {(command: string, args: string[], options: SpawnPipeOptions) => ChildAdapter} SpawnFn
 */

/**
 * @typedef {object} SupervisorOptions
 * @property {SpawnFn} spawn Host-provided child spawn.
 * @property {string} [command] Host-controlled executable, default 'cafe-core'.
 * @property {string[]} [args] Host-controlled arguments, default [].
 * @property {string} authorizedOrigin Exact origin required for IPC callers.
 * @property {() => object} getAuthorizedFrame Current authorized frame reference.
 * @property {number} [maxLineBytes] Default 4096.
 * @property {number} [maxQueueBytes] Default 16384, must be >= maxLineBytes + 1.
 * @property {(chunk: Uint8Array) => void} [onStderr] Optional stderr observer.
 */

const CONTROL_SCHEMA = 'cafe.control.v1';
const INTENT_KIND = 'intent';
const DEFAULT_COMMAND = 'cafe-core';
const DEFAULT_MAX_LINE_BYTES = 4096;
const DEFAULT_MAX_QUEUE_BYTES = 16384;
const U64_MAX = 18446744073709551615n;
const NEWLINE = 10;

/** @type {TextEncoder} */
const UTF8_ENCODER = new TextEncoder();
/** @type {TextDecoder} */
const UTF8_DECODER = new TextDecoder('utf-8', { fatal: true });

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
 * True when the record owns exactly the listed keys.
 * @param {Record<string, unknown>} record
 * @param {string[]} keys
 * @returns {boolean}
 */
function hasExactKeys(record, keys) {
  const actual = Object.keys(record);
  if (actual.length !== keys.length) {
    return false;
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(record, key)) {
      return false;
    }
  }
  return true;
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
 * Creates a host-owned supervisor for one native authority child process.
 * @param {SupervisorOptions} options
 */
function createBackendSupervisor(options) {
  if (typeof options !== 'object' || options === null) {
    throw new Error('createBackendSupervisor requires an options object');
  }
  if (typeof options.spawn !== 'function') {
    throw new Error('createBackendSupervisor requires a spawn function');
  }
  const spawnFn = options.spawn;
  const commandText = options.command === undefined ? DEFAULT_COMMAND : options.command;
  if (typeof commandText !== 'string' || commandText.length === 0) {
    throw new Error('createBackendSupervisor requires a non-empty command string');
  }
  const configuredArgs = options.args === undefined ? [] : options.args;
  if (!Array.isArray(configuredArgs)) {
    throw new Error('createBackendSupervisor requires args to be a string array');
  }
  for (const arg of configuredArgs) {
    if (typeof arg !== 'string') {
      throw new Error('createBackendSupervisor requires args to be a string array');
    }
  }
  /** @type {string[]} */
  const hostArgs = configuredArgs.slice();
  if (typeof options.authorizedOrigin !== 'string' || options.authorizedOrigin.length === 0) {
    throw new Error('createBackendSupervisor requires a non-empty authorizedOrigin');
  }
  const authorizedOrigin = options.authorizedOrigin;
  if (typeof options.getAuthorizedFrame !== 'function') {
    throw new Error('createBackendSupervisor requires a getAuthorizedFrame callback');
  }
  const getAuthorizedFrame = options.getAuthorizedFrame;
  const maxLineBytes = options.maxLineBytes === undefined ? DEFAULT_MAX_LINE_BYTES : options.maxLineBytes;
  if (!Number.isSafeInteger(maxLineBytes) || maxLineBytes <= 0) {
    throw new Error('createBackendSupervisor requires maxLineBytes to be a positive safe integer');
  }
  const maxQueueBytes = options.maxQueueBytes === undefined ? DEFAULT_MAX_QUEUE_BYTES : options.maxQueueBytes;
  if (!Number.isSafeInteger(maxQueueBytes) || maxQueueBytes < maxLineBytes + 1) {
    throw new Error('createBackendSupervisor requires maxQueueBytes >= maxLineBytes + 1');
  }
  if (options.onStderr !== undefined && typeof options.onStderr !== 'function') {
    throw new Error('createBackendSupervisor requires onStderr to be a function');
  }
  const onStderr = options.onStderr;

  /** @type {'starting'|'available'|'unavailable'} */
  let availability = 'starting';
  /** @type {number|null} */
  let pid = null;
  let reason = '';
  /** @type {string|null} */
  let authorityEpoch = null;
  /** @type {string|null} */
  let revision = null;
  /** @type {unknown} */
  let projectionState = null;
  let stderrBytes = 0;
  let started = false;
  /** @type {ChildAdapter|null} */
  let child = null;
  let childAlive = false;
  let stdoutFailed = false;
  /** @type {number[]} */
  let pendingBytes = [];
  /** @type {{text: string, bytes: number}[]} */
  let writeQueue = [];
  let writeQueueBytes = 0;
  let waitingDrain = false;
  /** @type {Set<(status: SupervisorStatus) => void>} */
  const listeners = new Set();

  /**
   * @returns {SupervisorStatus}
   */
  function getStatus() {
    const stateCopy = projectionState === null
      ? null
      : /** @type {object} */ (deepCloneJson(projectionState));
    /** @type {SupervisorStatus} */
    const projection = {
      availability,
      pid,
      reason,
      authority_epoch: authorityEpoch,
      revision,
      state: stateCopy,
      stderrBytes,
    };
    deepFreeze(projection);
    return projection;
  }

  /**
   * @returns {void}
   */
  function notify() {
    const projection = getStatus();
    const current = Array.from(listeners);
    for (const listener of current) {
      try {
        listener(projection);
      } catch {
        // A failing listener must not break other subscribers.
      }
    }
  }

  /**
   * First failure wins: the reason is never overwritten, queued intents are
   * cleared, and the projection is never mutated here.
   * @param {string} failureReason
   * @returns {void}
   */
  function setUnavailable(failureReason) {
    if (availability === 'unavailable') {
      return;
    }
    availability = 'unavailable';
    reason = failureReason;
    writeQueue = [];
    writeQueueBytes = 0;
    notify();
  }

  /**
   * @param {unknown} caller
   * @returns {void}
   */
  function authorize(caller) {
    if (typeof caller !== 'object' || caller === null) {
      throw new Error('unauthorized IPC caller');
    }
    const record = /** @type {Record<string, unknown>} */ (caller);
    if (record.origin !== authorizedOrigin) {
      throw new Error('unauthorized IPC origin');
    }
    if (record.frame !== getAuthorizedFrame()) {
      throw new Error('unauthorized IPC frame');
    }
    if (record.isMainFrame !== true) {
      throw new Error('unauthorized IPC caller: not the main frame');
    }
  }

  /**
   * @param {SupervisorCaller} caller
   * @param {string} method
   * @param {unknown} [payload]
   * @returns {SupervisorStatus|QueuedReceipt}
   */
  function handle(caller, method, payload) {
    authorize(caller);
    if (method === 'getSnapshot') {
      return getStatus();
    }
    if (method === 'submitIntent') {
      return submitIntent(payload);
    }
    throw new Error(`unknown IPC method: ${method}`);
  }

  /**
   * Admits one closed intent envelope into the bounded stdin queue.
   * @param {unknown} payload
   * @returns {QueuedReceipt}
   */
  function submitIntent(payload) {
    if (availability !== 'available' || authorityEpoch === null || revision === null) {
      throw new Error('backend unavailable: submitIntent requires a fresh snapshot');
    }
    if (!isPlainObject(payload)) {
      throw new Error('invalid intent: envelope must be a plain object');
    }
    const envelope = /** @type {Record<string, unknown>} */ (payload);
    if (!hasExactKeys(envelope, ['schema', 'kind', 'request_id', 'authority_epoch', 'expected_revision', 'body'])) {
      throw new Error('invalid intent: unexpected envelope keys');
    }
    if (envelope.schema !== CONTROL_SCHEMA) {
      throw new Error('invalid intent: unexpected schema');
    }
    if (envelope.kind !== INTENT_KIND) {
      throw new Error('invalid intent: unexpected kind');
    }
    if (typeof envelope.request_id !== 'string' || envelope.request_id.length === 0) {
      throw new Error('invalid intent: request_id must be a non-empty string');
    }
    if (!isCanonicalU64(envelope.authority_epoch)) {
      throw new Error('invalid intent: authority_epoch must be a canonical u64 string');
    }
    if (!isCanonicalU64(envelope.expected_revision)) {
      throw new Error('invalid intent: expected_revision must be a canonical u64 string');
    }
    if (envelope.authority_epoch !== authorityEpoch) {
      throw new Error('stale intent: authority_epoch mismatch');
    }
    if (envelope.expected_revision !== revision) {
      throw new Error('stale intent: expected_revision mismatch');
    }
    if (!isPlainObject(envelope.body)) {
      throw new Error('invalid intent: body must be a plain object');
    }
    /** @type {string} */
    let line = '';
    try {
      line = `${JSON.stringify(payload)}\n`;
    } catch {
      throw new Error('invalid intent: body is not JSON serializable');
    }
    if (child === null || !childAlive) {
      throw new Error('backend unavailable: child is not running');
    }
    const stdin = child.stdin;
    const newBytes = UTF8_ENCODER.encode(line).length;
    const buffered = stdin.writableLength;
    if (writeQueueBytes + buffered + newBytes > maxQueueBytes) {
      throw new Error('intent queue capacity exceeded (backpressure)');
    }
    if (waitingDrain) {
      writeQueue.push({ text: line, bytes: newBytes });
      writeQueueBytes += newBytes;
      return { queued: true };
    }
    let accepted = true;
    try {
      accepted = stdin.write(line);
    } catch {
      setUnavailable('backend stdin write failed');
      throw new Error('backend unavailable: stdin write failed');
    }
    if (accepted === false) {
      // The chunk was accepted into the stream buffer; wait for drain and
      // never resend it.
      waitingDrain = true;
    }
    return { queued: true };
  }

  /**
   * @param {SupervisorCaller} caller
   * @param {(status: SupervisorStatus) => void} listener
   * @returns {() => void}
   */
  function subscribe(caller, listener) {
    authorize(caller);
    if (typeof listener !== 'function') {
      throw new Error('subscribe requires a listener function');
    }
    listeners.add(listener);
    try {
      listener(getStatus());
    } catch {
      // Immediate-emission failures must not break subscribe.
    }
    let active = true;
    return function unsubscribe() {
      if (!active) {
        return;
      }
      active = false;
      listeners.delete(listener);
    };
  }

  /**
   * Idempotent for the full supervisor lifetime: at most one spawn, no
   * respawn after EOF, error, exit, or dispose.
   * @returns {SupervisorStatus}
   */
  function start() {
    if (started) {
      return getStatus();
    }
    started = true;
    /** @type {SpawnPipeOptions} */
    const pipeOptions = { stdio: ['pipe', 'pipe', 'pipe'], shell: false };
    /** @type {ChildAdapter|null} */
    let spawned = null;
    try {
      const result = spawnFn(commandText, hostArgs, pipeOptions);
      if (typeof result === 'object' && result !== null) {
        spawned = result;
      }
    } catch {
      spawned = null;
    }
    if (spawned === null) {
      setUnavailable('backend spawn failed');
      return getStatus();
    }
    child = spawned;
    childAlive = true;
    if (typeof spawned.pid === 'number') {
      pid = spawned.pid;
    }
    spawned.stdout.on('data', onStdoutData);
    spawned.stdout.on('end', onStdoutEnd);
    spawned.stdout.on('error', onStdoutError);
    spawned.stderr.on('data', onStderrData);
    spawned.stderr.on('error', onStderrError);
    spawned.stdin.on('drain', onStdinDrain);
    spawned.stdin.on('error', onStdinError);
    spawned.on('error', onChildError);
    spawned.on('exit', onChildExit);
    spawned.on('close', onChildClose);
    return getStatus();
  }

  /**
   * Trusted host teardown. Kills only the owned, still-live child via its
   * own handle. Safe to call twice.
   * @returns {void}
   */
  function dispose() {
    if (child !== null && childAlive) {
      childAlive = false;
      try {
        child.kill();
      } catch {
        // Ignore kill failures during teardown.
      }
    }
    listeners.clear();
    setUnavailable('supervisor disposed');
  }

  /**
   * @param {unknown} chunk
   * @returns {void}
   */
  function onStdoutData(chunk) {
    if (!(chunk instanceof Uint8Array)) {
      setUnavailable('stdout protocol violation: chunk is not bytes');
      return;
    }
    if (stdoutFailed) {
      return;
    }
    for (let index = 0; index < chunk.length; index += 1) {
      pendingBytes.push(chunk[index]);
    }
    drainPendingLines();
  }

  /**
   * Splits complete lines off the pending byte buffer. The byte bound is
   * enforced incrementally, even before any newline arrives.
   * @returns {void}
   */
  function drainPendingLines() {
    for (;;) {
      const newline = pendingBytes.indexOf(NEWLINE);
      if (newline === -1) {
        if (pendingBytes.length > maxLineBytes) {
          failOversizedLine();
        }
        return;
      }
      const lineBytes = pendingBytes.slice(0, newline);
      pendingBytes = pendingBytes.slice(newline + 1);
      if (lineBytes.length > maxLineBytes) {
        failOversizedLine();
        return;
      }
      handleStdoutLine(lineBytes);
    }
  }

  /**
   * @returns {void}
   */
  function failOversizedLine() {
    stdoutFailed = true;
    pendingBytes = [];
    setUnavailable('stdout line exceeds byte limit');
  }

  /**
   * @param {number[]} lineBytes
   * @returns {void}
   */
  function handleStdoutLine(lineBytes) {
    /** @type {unknown} */
    let message = null;
    try {
      const text = UTF8_DECODER.decode(new Uint8Array(lineBytes));
      if (text.length === 0) {
        setUnavailable('stdout protocol violation: empty line');
        return;
      }
      message = JSON.parse(text);
    } catch {
      setUnavailable('stdout protocol violation: line is not valid utf-8 json');
      return;
    }
    applyInboundMessage(message);
  }

  /**
   * @param {unknown} message
   * @returns {void}
   */
  function applyInboundMessage(message) {
    if (!isPlainObject(message)) {
      setUnavailable('stdout protocol violation: malformed envelope');
      return;
    }
    const envelope = /** @type {Record<string, unknown>} */ (message);
    if (envelope.kind === 'snapshot') {
      applySnapshot(envelope);
      return;
    }
    if (envelope.kind === 'delta') {
      applyDelta(envelope);
      return;
    }
    setUnavailable('stdout protocol violation: unknown message kind');
  }

  /**
   * A fresh valid snapshot establishes or replaces the projection and
   * restores availability. Same-epoch snapshots must not roll back.
   * @param {Record<string, unknown>} envelope
   * @returns {void}
   */
  function applySnapshot(envelope) {
    if (!hasExactKeys(envelope, ['kind', 'authority_epoch', 'revision', 'state'])) {
      setUnavailable('stdout protocol violation: invalid snapshot keys');
      return;
    }
    if (!isCanonicalU64(envelope.authority_epoch) || !isCanonicalU64(envelope.revision)) {
      setUnavailable('stdout protocol violation: invalid snapshot clock');
      return;
    }
    if (!isPlainObject(envelope.state)) {
      setUnavailable('stdout protocol violation: invalid snapshot state');
      return;
    }
    const epoch = /** @type {string} */ (envelope.authority_epoch);
    const nextRevision = /** @type {string} */ (envelope.revision);
    if (authorityEpoch !== null && epoch === authorityEpoch && revision !== null) {
      if (BigInt(nextRevision) < BigInt(revision)) {
        setUnavailable('stale snapshot: revision moved backwards');
        return;
      }
    }
    authorityEpoch = epoch;
    revision = nextRevision;
    projectionState = envelope.state;
    availability = 'available';
    reason = '';
    notify();
  }

  /**
   * A delta carries a complete replacement state bound to the same epoch
   * and the exact next revision. Stale or gapped deltas never overwrite
   * the existing projection; they require a fresh snapshot.
   * @param {Record<string, unknown>} envelope
   * @returns {void}
   */
  function applyDelta(envelope) {
    if (!hasExactKeys(envelope, ['kind', 'authority_epoch', 'base_revision', 'revision', 'state'])) {
      setUnavailable('stdout protocol violation: invalid delta keys');
      return;
    }
    if (!isCanonicalU64(envelope.authority_epoch) || !isCanonicalU64(envelope.base_revision) || !isCanonicalU64(envelope.revision)) {
      setUnavailable('stdout protocol violation: invalid delta clock');
      return;
    }
    if (!isPlainObject(envelope.state)) {
      setUnavailable('stdout protocol violation: invalid delta state');
      return;
    }
    if (availability !== 'available' || authorityEpoch === null || revision === null) {
      setUnavailable('stale delta: no fresh snapshot');
      return;
    }
    const epoch = /** @type {string} */ (envelope.authority_epoch);
    const base = /** @type {string} */ (envelope.base_revision);
    const nextRevision = /** @type {string} */ (envelope.revision);
    if (epoch !== authorityEpoch || base !== revision || BigInt(nextRevision) !== BigInt(base) + 1n) {
      setUnavailable('stale delta: epoch, base, or revision gap');
      return;
    }
    revision = nextRevision;
    projectionState = envelope.state;
    notify();
  }

  /**
   * Stderr is drained independently of stdout framing; only a byte counter
   * is kept, never an unbounded buffer.
   * @param {unknown} chunk
   * @returns {void}
   */
  function onStderrData(chunk) {
    if (chunk instanceof Uint8Array) {
      stderrBytes += chunk.length;
      if (onStderr !== undefined) {
        try {
          onStderr(chunk);
        } catch {
          // Observer failures must not affect supervision.
        }
      }
    }
  }

  /**
   * @returns {void}
   */
  function onStderrError() {
    setUnavailable('backend stderr error');
  }

  /**
   * Flushes chunks that were queued while waiting for drain. Chunks already
   * accepted by the stream are never resent.
   * @returns {void}
   */
  function onStdinDrain() {
    waitingDrain = false;
    if (child === null || !childAlive) {
      writeQueue = [];
      writeQueueBytes = 0;
      return;
    }
    const stdin = child.stdin;
    while (writeQueue.length > 0) {
      const next = writeQueue[0];
      let accepted = true;
      try {
        accepted = stdin.write(next.text);
      } catch {
        writeQueue = [];
        writeQueueBytes = 0;
        setUnavailable('backend stdin write failed');
        return;
      }
      writeQueue.shift();
      writeQueueBytes -= next.bytes;
      if (writeQueueBytes < 0) {
        writeQueueBytes = 0;
      }
      if (accepted === false) {
        waitingDrain = true;
        return;
      }
    }
  }

  /**
   * @returns {void}
   */
  function onStdinError() {
    setUnavailable('backend stdin error');
  }

  /**
   * @returns {void}
   */
  function onStdoutEnd() {
    setUnavailable('backend stdout ended');
  }

  /**
   * @returns {void}
   */
  function onStdoutError() {
    setUnavailable('backend stdout error');
  }

  /**
   * @returns {void}
   */
  function onChildError() {
    setUnavailable('backend child error');
  }

  /**
   * @returns {void}
   */
  function onChildExit() {
    childAlive = false;
    setUnavailable('backend child exited');
  }

  /**
   * @returns {void}
   */
  function onChildClose() {
    childAlive = false;
    setUnavailable('backend child closed');
  }

  return { start, getStatus, handle, subscribe, dispose };
}

module.exports = { createBackendSupervisor };
