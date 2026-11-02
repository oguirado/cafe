// @ts-check
/** Display-only preview session bounded by native source grants. */
const RETRY_DELAYS = Object.freeze([500, 1500, 3000]);
let acquisitionQueue = Promise.resolve();

/** A main-frame grant belongs to one capture call. Never interleave tile grants.
 * @param {()=>Promise<any>} acquire @returns {Promise<any>} */
function serial(acquire) {
  const next = acquisitionQueue.then(acquire, acquire);
  acquisitionQueue = next.then(() => undefined, () => undefined);
  return next;
}

/** @param {any} stream */
export function stopPreviewTracks(stream) {
  for (const track of stream?.getTracks?.() || []) {
    try { track.stop(); } catch { /* Every other track still needs cleanup. */ }
  }
}

/** @param {number} width @param {number} height */
export function previewLimits(width, height) {
  return { width: Math.max(1, Math.min(1920, Math.floor(Number.isFinite(width) ? width : 1))),
    height: Math.max(1, Math.min(1080, Math.floor(Number.isFinite(height) ? height : 1))), frameRate: 15 };
}

/** @param {any} track @param {{width:number,height:number,frameRate:number}} limits */
async function constrain(track, limits) {
  await track.applyConstraints({ width: { max: limits.width }, height: { max: limits.height }, frameRate: { max: limits.frameRate } });
  const actual = track.getSettings();
  if (!Number.isFinite(actual.width) || actual.width <= 0 || actual.width > limits.width
    || !Number.isFinite(actual.height) || actual.height <= 0 || actual.height > limits.height
    || !Number.isFinite(actual.frameRate) || actual.frameRate <= 0 || actual.frameRate > 15) throw new Error('preview_unavailable');
}

/** Lifecycle owns only display tracks, never recording.
 * @param {{bridge:any,getDisplayMedia:(constraints:any)=>Promise<any>,onState:(state:string,reason:string)=>void,onStream:(stream:any)=>void,
 * size:()=>{width:number,height:number},schedule?:(callback:()=>void,delay:number)=>any,cancel?:(timer:any)=>void}} options
 */
export function createPreviewSession(options) {
  const schedule = options.schedule || ((callback, delay) => setTimeout(callback, delay));
  const cancel = options.cancel || (timer => clearTimeout(timer));
  let binding = null;
  let bindingKey = '';
  let enabled = false, visible = false, disposed = false;
  let generation = 0, retry = 0, timer = null;
  let stream = null;
  let endListeners = [];
  const acquisitionTimers = new Set();
  let resizeQueue = Promise.resolve();
  const active = () => !disposed && enabled && visible && binding !== null;
  const state = (value, reason = '') => { if (!disposed) options.onState(value, reason); };
  function clearStream() {
    for (const [track, listener] of endListeners) track.removeEventListener?.('ended', listener);
    endListeners = [];
    stopPreviewTracks(stream); stream = null; options.onStream(null);
  }
  function stop() {
    generation += 1;
    if (timer !== null) { cancel(timer); timer = null; }
    for (const timeout of acquisitionTimers) clearTimeout(timeout);
    acquisitionTimers.clear();
    clearStream();
  }
  function later(reason) {
    state('no_preview', reason);
    if (!active() || retry >= RETRY_DELAYS.length || ['preview_disabled', 'preview_denied', 'no_preview'].includes(reason)) return;
    const delay = RETRY_DELAYS[retry++];
    timer = schedule(() => { timer = null; void acquire(); }, delay);
  }
  async function acquire() {
    if (!active()) return;
    const attempt = generation;
    const target = { ...binding };
    state('connecting');
    await serial(async () => {
      if (!active() || attempt !== generation) return;
      let captured = null;
      let timedOut = false;
      // A delayed getDisplayMedia call has no grant id. Keep its queue slot until
      // it settles, so it cannot consume another tile's later grant after timeout.
      const timeout = setTimeout(() => {
        acquisitionTimers.delete(timeout);
        if (!active() || attempt !== generation) return;
        timedOut = true; state('no_preview', 'preview_unavailable');
      }, 8000);
      acquisitionTimers.add(timeout);
      try {
        if (typeof options.bridge?.previewSource !== 'function') { later('preview_disabled'); return; }
        const grant = await options.bridge.previewSource({ capsule_id: target.capsule_id, feed_id: target.feed_id });
        if (!active() || attempt !== generation) return;
        if (timedOut) { later('preview_unavailable'); return; }
        if (grant?.ok !== true || grant.capsule_id !== target.capsule_id || grant.feed_id !== target.feed_id) {
          const reason = ['preview_disabled', 'no_preview', 'preview_busy', 'preview_denied', 'preview_unavailable'].includes(grant?.error) ? grant.error : 'preview_denied';
          later(reason); return;
        }
        const size = options.size(), limits = previewLimits(size.width, size.height);
        // getDisplayMedia rejects max/min constraints initially; enforce caps on the returned track before display.
        captured = await options.getDisplayMedia({ audio: false,
          video: { width: { ideal: limits.width }, height: { ideal: limits.height }, frameRate: { ideal: 15 } } });
        if (!active() || attempt !== generation) { stopPreviewTracks(captured); return; }
        if (timedOut) { stopPreviewTracks(captured); later('preview_unavailable'); return; }
        const tracks = captured?.getVideoTracks?.() || [];
        if (tracks.length !== 1 || (captured?.getAudioTracks?.() || []).length !== 0) throw new Error('preview_denied');
        const currentSize = options.size();
        await constrain(tracks[0], previewLimits(currentSize.width, currentSize.height));
        if (!active() || attempt !== generation || tracks[0].readyState === 'ended') { stopPreviewTracks(captured); return; }
        if (timedOut) { stopPreviewTracks(captured); later('preview_unavailable'); return; }
        clearStream(); stream = captured;
        const ended = () => {
          if (attempt !== generation || disposed) return;
          clearStream(); later('preview_unavailable');
        };
        for (const track of captured.getTracks()) {
          track.addEventListener?.('ended', ended, { once: true }); endListeners.push([track, ended]);
        }
        options.onStream(stream); state('live');
      } catch (error) {
        stopPreviewTracks(captured);
        if (active() && attempt === generation) {
          const denied = error?.name === 'NotAllowedError' || error?.message === 'preview_denied';
          later(denied ? 'preview_denied' : 'preview_unavailable');
        }
      } finally {
        clearTimeout(timeout); acquisitionTimers.delete(timeout);
      }
    });
  }
  /** @param {{capsule_id:string,feed_id:string,generation:string}|null} value */
  function setBinding(value) {
    const key = value === null ? '' : JSON.stringify([value.capsule_id, value.feed_id, value.generation]);
    if (key === bindingKey) return;
    stop(); bindingKey = key; binding = value === null ? null : { ...value }; retry = 0;
    state('no_preview'); if (active()) void acquire();
  }
  /** @param {boolean} show @param {boolean} allow */
  function setVisibility(show, allow) {
    if (visible === show && enabled === allow) return;
    stop(); visible = show; enabled = allow; retry = 0;
    state('no_preview', allow ? '' : 'preview_disabled'); if (active()) void acquire();
  }
  function resize() {
    const work = resizeQueue.then(async () => {
      if (!stream || !active()) return;
      const selected = stream, attempt = generation;
      const size = options.size();
      try { await constrain(selected.getVideoTracks()[0], previewLimits(size.width, size.height)); }
      catch {
        if (attempt === generation && stream === selected) { clearStream(); later('preview_unavailable'); }
      }
    });
    resizeQueue = work.then(() => undefined, () => undefined);
    return work;
  }
  function dispose() { if (disposed) return; stop(); disposed = true; }
  return Object.freeze({ setBinding, setVisibility, resize, dispose });
}
