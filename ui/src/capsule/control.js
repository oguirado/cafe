// @ts-check
/** Presentation helpers and display-only recording timer. */

/** @param {unknown} value @returns {string} */
export function elapsedLabel(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    value = String(Math.floor(value));
  }
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value)) return '—';
  const seconds = BigInt(value) / 1000n;
  const hours = seconds / 3600n;
  const minutes = (seconds / 60n) % 60n;
  const rest = seconds % 60n;
  return (hours > 0n ? hours.toString().padStart(2, '0') + ':' : '')
    + minutes.toString().padStart(2, '0') + ':' + rest.toString().padStart(2, '0');
}

/**
 * Shared display-only recording timer. Starts when a capsule's recording first
 * appears Running in a snapshot and resets when it stops. Uses one shared
 * interval while any tile records.
 *
 * @param {(now: number) => void} onTick
 * @param {{ now?: () => number, intervalMs?: number }} [options]
 */
export function createRecordingTimer(onTick, options = {}) {
  const clock = options.now || (() => Date.now());
  const intervalMs = options.intervalMs ?? 1000;
  /** @type {Map<string, number>} */
  const startedAt = new Map();
  /** @type {ReturnType<typeof setInterval> | null} */
  let intervalId = null;

  function stopInterval() {
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  }

  /**
   * @param {readonly any[]} recordings
   * @param {number} [now]
   */
  function sync(recordings, now = clock()) {
    const runningIds = new Set();
    if (Array.isArray(recordings)) {
      for (const rec of recordings) {
        if (rec && typeof rec === 'object' && rec.state === 'Running' && typeof rec.capsule_id === 'string' && rec.capsule_id !== '') {
          runningIds.add(rec.capsule_id);
          if (!startedAt.has(rec.capsule_id)) {
            startedAt.set(rec.capsule_id, now);
          }
        }
      }
    }
    for (const id of [...startedAt.keys()]) {
      if (!runningIds.has(id)) {
        startedAt.delete(id);
      }
    }
    if (startedAt.size > 0 && intervalId === null) {
      intervalId = setInterval(() => {
        onTick(clock());
      }, intervalMs);
    } else if (startedAt.size === 0 && intervalId !== null) {
      stopInterval();
    }
    onTick(now);
  }

  /**
   * @param {string} capsuleId
   * @param {number} [now]
   * @returns {string}
   */
  function elapsedFor(capsuleId, now = clock()) {
    const start = startedAt.get(capsuleId);
    if (start === undefined) return '—';
    return elapsedLabel(Math.max(0, now - start));
  }

  function dispose() {
    stopInterval();
    startedAt.clear();
  }

  return Object.freeze({ sync, elapsedFor, dispose });
}

/** @param {unknown} expiry @param {number} now @returns {number|null} */
export function planSecondsRemaining(expiry, now) {
  if (typeof expiry !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(expiry)
    || !Number.isSafeInteger(now) || now < 0) return null;
  const remaining = BigInt(expiry) - BigInt(now);
  const seconds = remaining <= 0n ? 0n : remaining / 1000n + (remaining % 1000n === 0n ? 0n : 1n);
  return seconds > BigInt(Number.MAX_SAFE_INTEGER) ? null : Number(seconds);
}

/** @param {any} capsule @param {any} recording @param {Record<string,string>} previews */
export function tileState(capsule, recording, previews) {
  if (recording?.state === 'Running') return 'recording';
  const feeds = Array.isArray(capsule?.feeds) ? capsule.feeds : [];
  if (feeds.some(feed => feed.window_state === 'window_closed') || recording?.reason === 'recording_capture_closed') return 'window_closed';
  if (feeds.some(feed => feed.window_state === 'window_minimized') || recording?.reason === 'recording_capture_minimized') return 'window_minimized';
  if (['Failed', 'Interrupted', 'Blocked'].includes(recording?.state)
    || (typeof recording?.reason === 'string' && recording.reason !== '')) return 'recording_failed';
  const states = feeds.map(feed => previews[feed.feed_id]);
  if (states.includes('live')) return 'live';
  if (states.includes('connecting')) return 'connecting';
  return 'no_preview';
}

/** @param {string} state @returns {string} */
export function tileStateLabel(state) {
  return ({ live: 'Live preview', connecting: 'Connecting preview', window_minimized: 'Window minimized',
    window_closed: 'Window closed', no_preview: 'No preview', recording: 'Recording',
    recording_failed: 'Recording failed' })[state] || 'No preview';
}

/** Fixed actionable descriptions; never display exception messages. Only failures show text. @param {any} result */
export function actionResultLabel(result) {
  if (['observed', 'committed', 'configured'].includes(result?.state) || result?.status === 'queued') return '';
  if (result?.code === 'request_or_capacity_conflict') return 'Core refused the request or capsule limit. Wait for the core update, then retry.';
  if (result?.status === 'unconfirmed') return 'No authority confirmation. Refresh diagnostics before retrying.';
  if (result?.status === 'cancelled') return 'Request cancelled. Retry the action when ready.';
  if (result?.status === 'blocked') return 'Request not sent. Resolve the requirement shown for this capsule, then retry.';
  if (['rejected', 'blocked', 'interrupted'].includes(result?.state)) return 'Authority refused the request. Review the capsule state and diagnostics, then retry.';
  return 'Request failed. Refresh diagnostics and try again.';
}

/** Wait for the matching authority result, never infer success from queued.
 * @param {{getState:()=>any,subscribe:(listener:(view:any)=>void)=>()=>void}} controls
 * @param {string} requestId
 * @param {{signal?:AbortSignal,timeoutMs?:number}} [options]
 * @returns {Promise<any>}
 */
export function waitForAuthorityResult(controls, requestId, options = {}) {
  return new Promise(resolve => {
    let settled = false;
    let unsubscribe = () => {};
    let timer;
    const finish = result => {
      if (settled) return;
      settled = true; clearTimeout(timer); unsubscribe();
      options.signal?.removeEventListener('abort', cancel);
      resolve(result);
    };
    const cancel = () => finish({ status: 'cancelled' });
    const accept = view => {
      if (view?.availability !== 'available') { finish({ status: 'unconfirmed' }); return; }
      const result = view?.lastResult;
      if (result?.requestId === requestId && result.state !== 'pending') finish(result);
    };
    if (options.signal?.aborted) { cancel(); return; }
    options.signal?.addEventListener('abort', cancel, { once: true });
    timer = setTimeout(() => finish({ status: 'unconfirmed' }), options.timeoutMs ?? 8000);
    try {
      unsubscribe = controls.subscribe(accept);
      if (settled) unsubscribe();
      else accept(controls.getState());
    } catch { finish({ status: 'unconfirmed' }); }
  });
}

/**
 * Wait briefly for a snapshot projection predicate to match.
 * @param {() => any} match
 * @param {() => boolean} active
 * @returns {Promise<any>}
 */
export async function waitForProjectedMatch(match, active) {
  let found = match();
  for (let wait = 0; !found && wait < 60 && active(); wait += 1) {
    await new Promise(resolve => { setTimeout(resolve, 50); });
    found = match();
  }
  return found;
}
