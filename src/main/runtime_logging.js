// @ts-check
'use strict';
const schema = JSON.parse(require('node:fs').readFileSync(require('node:path').join(__dirname, '../contracts/runtime_logging.json'), 'utf8'));
const MAX_BYTES = 2 * 1024 * 1024;
const MAX_ROWS = 4096;
/** @param {unknown} v @returns {v is Record<string, any>} */
function object(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
/** @param {unknown} v @param {number} max */
function count(v, max) { return Number.isSafeInteger(v) && /** @type {number} */(v) >= 0 && /** @type {number} */(v) <= max ? v : null; }
/** @param {readonly string[]} values @param {unknown} v @param {string} fallback */
function fixed(values, v, fallback) { return typeof v === 'string' && values.includes(v) ? v : fallback; }
/** @param {unknown} status */
function projection(status) {
  const s = object(status) ? status : {};
  const st = object(s.state) ? s.state : {};
  const last = object(st.last_result) ? st.last_result : {};
  const caps = object(st.capsules) && Object.keys(st.capsules).length <= 12 ? Object.values(st.capsules) : null;
  const goodCaps = caps !== null && caps.every(c => object(c) && object(c.feeds) && Object.keys(c.feeds).length <= 2);
  const windows = Array.isArray(st.windows) && st.windows.length <= 3 ? st.windows : null;
  const recordings = Array.isArray(st.recordings) && st.recordings.length <= 12 ? st.recordings : null;
  let frames = 0, goodFrames = recordings !== null;
  const states = [];
  if (recordings) for (const capsule of recordings) {
    if (!object(capsule) || !Array.isArray(capsule.feeds) || capsule.feeds.length > 2) { goodFrames = false; break; }
    states.push(fixed(schema.recording_states, capsule.state, 'unknown'));
    for (const feed of capsule.feeds) {
      if (!object(feed) || typeof feed.written_frames !== 'string' || !/^(0|[1-9][0-9]{0,9})$/.test(feed.written_frames)) { goodFrames = false; break; }
      frames += Number(feed.written_frames);
    }
  }
  const reason = typeof s.reason === 'string' ? s.reason : '';
  let reasonCode = 'none';
  if (reason) {
    const reasons = new Map([
      ['backend child exited', 'child_exit'], ['backend child closed', 'child_exit'],
      ['backend stdout ended', 'stream_ended'], ['backend stdin write failed', 'stdin_failed'],
      ['backend stdin error', 'stdin_failed'], ['backend child error', 'backend_error'],
      ['backend stdout error', 'backend_error'], ['backend stderr error', 'backend_error'],
      ['backend spawn failed', 'backend_error'],
    ]);
    reasonCode = reasons.get(reason) || (reason.startsWith('stdout protocol violation:') || reason.startsWith('stale snapshot:') || reason.startsWith('stale delta:') || reason === 'stdout line exceeds byte limit' ? 'stdout_invalid' : 'other_unavailable');
  }
  return {
    bridge: fixed(schema.bridges, s.availability, 'unknown'), reason: reasonCode,
    result_state: fixed(schema.states, last.state, 'unknown'), result_code: fixed(schema.codes, last.code, 'unknown_result'),
    capsule_count: goodCaps && caps ? caps.length : null,
    paired_feed_count: goodCaps && caps ? caps.reduce((n, c) => n + Object.keys(c.feeds).length, 0) : null,
    listed_window_count: windows ? windows.length : null,
    minimized_window_count: windows && windows.every(w => object(w) && typeof w.minimized === 'boolean') ? windows.filter(w => w.minimized).length : null,
    visible_window_count: windows && windows.every(w => object(w) && typeof w.visible === 'boolean') ? windows.filter(w => w.visible).length : null,
    recorded_frames: goodFrames ? count(frames, 4294967295) : null,
    recording_state: recordings && recordings.length === 0 ? 'idle' : states.length === 1 ? states[0] : 'unknown',
  };
}
/** Host-only bounded logger. No raw source object is ever serialized. @param {{openSink:()=>import('node:child_process').ChildProcess,baseCommit?:unknown,authoritySha256:string}} options */
function createRuntimeTrace(options) {
  const start = process.hrtime.bigint();
  const commit = typeof options.baseCommit === 'string' && /^[0-9a-f]{40}$/.test(options.baseCommit) ? options.baseCommit : null;
  if (!/^[0-9a-f]{64}$/.test(options.authoritySha256)) throw new Error('TRACE_BUILD_REJECTED');
  /** @type {import('node:stream').Writable|null} */ let sink = null;
  let started = false;
  let seq = 0, bytes = 0, failed = false, previous = '', sampledAt = -1000;
  let partial = '', dropping = false;
  let latest = projection(null);
  function elapsed() { return Math.min(86400000, Math.max(0, Number((process.hrtime.bigint() - start) / 1000000n))); }
  /** @param {string} event @param {string} [op] @param {string} [stage] */
  function append(event, op = 'none', stage = 'none') {
    if (failed || sink === null || seq >= MAX_ROWS) return;
    const row = { schema: schema.schema, seq: seq + 1, elapsed_ms: elapsed(),
      event: fixed(schema.events, event, 'snapshot'), op: fixed(schema.operations, op, 'none'),
      ...latest, stage: fixed(schema.stages, stage, 'none'), base_commit: commit, authority_sha256: options.authoritySha256 };
    const limit = seq === MAX_ROWS - 1 || bytes > MAX_BYTES - 8192;
    if (limit) {row.event = 'log_limit'; row.op = 'none'; row.stage = 'none';}
    const data = Buffer.from(JSON.stringify(row) + '\n');
    if (data.length > 4096 || bytes + data.length > MAX_BYTES) { failed = true; return; }
    try {
      if (sink.writableLength + data.length > 16384) {failed = true; return;}
      sink.write(data); // false is accepted buffered data, never resend.
      seq++; bytes += data.length; if (limit) failed = true;
    } catch { failed = true; }

  }
  /** @param {unknown} status */
  function snapshot(status) {
    latest = projection(status);
    const { recorded_frames: ignored, ...identity } = latest;
    const key = JSON.stringify(identity), now = elapsed();
    if (key === previous && now - sampledAt < 1000) return;
    previous = key; sampledAt = now; append('snapshot');
  }
  /** Parse only fixed ASCII trace literals; never persist raw stderr. @param {Uint8Array} chunk */
  function stderr(chunk) {
    for (const byte of chunk) {
      if (byte === 10) {
        if (!dropping && partial.startsWith('CAFE_TRACE:')) {
          const stage = partial.slice(11);
          if (schema.stages.includes(stage) && stage !== 'none') append('native_stage', 'none', stage);
        }
        partial = ''; dropping = false;
      } else if (byte < 32 || byte > 126 || partial.length >= 120) { partial = ''; dropping = true; }
      else if (!dropping) partial += String.fromCharCode(byte);
    }
  }
  function startSink() {
    if (started) return;
    started = true;
    try {
      const child = options.openSink();
      child.once('error', () => {failed = true;});
      child.once('exit', () => {failed = true;});
      if (child.stdin === null) throw new Error('TRACE_PIPE_REJECTED');
      sink = child.stdin; sink.on('error', () => {failed = true;});
      append('host_start');
    } catch {failed = true;}
  }
  function close() { if (sink !== null) {try {sink.end();} catch {} sink = null;} }
  return { start: startSink, intent: (/** @type {unknown} */ op) => append('intent_queued', fixed(schema.operations, op, 'none')),
    snapshot, stderr, close, status: () => ({ available: !failed && sink !== null, rows: seq, bytes }) };
}
module.exports = { createRuntimeTrace, projection };
