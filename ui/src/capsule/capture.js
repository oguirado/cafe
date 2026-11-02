// @ts-check
/** Display-only preview session bounded by native source grants. */
const FRAME_WAIT_MS = 3000;
const RETRY_DELAYS = Object.freeze([500, 1500, 3000]);
let acquisitionQueue = Promise.resolve();

/** A main-frame grant belongs to one capture call. Never interleave tile grants.
 * @param {()=>Promise<any>} acquire @returns {Promise<any>} */
function serial(acquire) {
  const next = acquisitionQueue.then(acquire, acquire);
  acquisitionQueue = next.then(() => undefined, () => undefined);
  return next;
}

/** Closed operator text only; adapter details, paths and titles never enter it.
 * @param {string} reason @param {string} state */
export function previewReasonText(reason, state) {
  /** @type {Record<string,string>} */
  const messages = {
    preview_disabled: 'Turn previews on to see this feed.',
    no_preview: 'Restore the pop-out. If it was replaced, anchor it again.',
    preview_denied: 'Preview permission unavailable. Check that the window is still open.',
    preview_busy: 'Another preview is being acquired. Wait for it to finish.',
    preview_no_frames: 'No video frames are arriving. Keep the window visible and uncovered.',
    preview_unavailable: 'Preview could not start. Restore the pop-out; if it was replaced, anchor it again.',
  };
  return state === 'connecting' ? 'Waiting for an approved window and its first video frame.'
    : messages[reason] || 'Recording can still run while preview is unavailable.';
}

/** Observe actual presented-frame events, with a decoded-frame counter fallback.
 * A stream/element switch invalidates every callback before it can publish.
 * @param {any} video @param {any} stream @param {(count:number)=>void} onFrame
 * @param {{schedule?:(callback:()=>void,delay:number)=>any,cancel?:(timer:any)=>void}} [timers] */
export function observePreviewFrames(video, stream, onFrame, timers = {}) {
  let stopped = false, callback = null, timer = null;
  const schedule = timers.schedule || ((run, delay) => setTimeout(run, delay));
  const cancel = timers.cancel || (handle => clearTimeout(handle));
  const current = () => !stopped && video?.srcObject === stream;
  if (typeof video?.requestVideoFrameCallback === 'function') {
    /** @param {number} _now @param {any} metadata */
    const presented = (_now, metadata) => {
      callback = null;
      if (!current()) return;
      if (Number.isSafeInteger(metadata?.presentedFrames) && metadata.presentedFrames > 0) onFrame(metadata.presentedFrames);
      if (current()) callback = video.requestVideoFrameCallback(presented);
    };
    callback = video.requestVideoFrameCallback(presented);
  } else if (typeof video?.getVideoPlaybackQuality === 'function') {
    const count = () => { try { return video.getVideoPlaybackQuality().totalVideoFrames; } catch { return null; } };
    let previous = count();
    const poll = () => {
      timer = null;
      if (!current()) return;
      const next = count();
      if (Number.isSafeInteger(next) && Number.isSafeInteger(previous) && next > previous) onFrame(next);
      previous = next;
      timer = schedule(poll, 250);
    };
    timer = schedule(poll, 250);
  }
  return () => {
    stopped = true;
    if (callback !== null) video?.cancelVideoFrameCallback?.(callback);
    if (timer !== null) cancel(timer);
  };
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

/** Fit the cropped video with one scale factor, using decoded pixel dimensions.
 * @param {number} width @param {number} height @param {number} videoWidth @param {number} videoHeight @param {number} trim */
export function previewGeometry(width, height, videoWidth, videoHeight, trim) {
  if (![width, height, videoWidth, videoHeight].every(n => Number.isFinite(n) && n > 0)) return null;
  const top = Number.isInteger(trim) && trim >= 0 && trim <= 2160 && trim < videoHeight ? trim : 0;
  const scale = Math.min(width / videoWidth, height / (videoHeight - top));
  return { width: videoWidth * scale, height: (videoHeight - top) * scale, videoHeight: videoHeight * scale, top: -top * scale };
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
 * size:()=>{width:number,height:number},observeFrames?:(stream:any,onFrame:(presentedFrames:number)=>void)=>(()=>void),frameSchedule?:(callback:()=>void,delay:number)=>any,frameCancel?:(timer:any)=>void,schedule?:(callback:()=>void,delay:number)=>any,cancel?:(timer:any)=>void}} options
 */
export function createPreviewSession(options) {
  const schedule = options.schedule || ((callback, delay) => setTimeout(callback, delay));
  const cancel = options.cancel || (timer => clearTimeout(timer));
  let binding = null;
  let bindingKey = '', identityKey = '';
  let enabled = false, visible = false, disposed = false;
  let generation = 0, retry = 0, timer = null;
  let acquisitionPending = false;
  let stream = null;
  let frameTimer = null, cancelFrames = null;
  const frameSchedule = options.frameSchedule || ((callback, delay) => setTimeout(callback, delay));
  const frameCancel = options.frameCancel || (handle => clearTimeout(handle));
  let endListeners = [];
  const acquisitionTimers = new Set();
  let resizeQueue = Promise.resolve();
  const active = () => !disposed && enabled && visible && binding !== null && binding.preview_available !== false;
  const state = (value, reason = '') => { if (!disposed) options.onState(value, reason); };
  function clearStream() {
    if (frameTimer !== null) { frameCancel(frameTimer); frameTimer = null; }
    if (cancelFrames) { try { cancelFrames(); } catch {} cancelFrames = null; }
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
  function watchFrames(selected, attempt) {
    let presented = 0;
    const current = () => active() && attempt === generation && stream === selected;
    const arm = () => {
      if (frameTimer !== null) frameCancel(frameTimer);
      frameTimer = frameSchedule(() => {
        frameTimer = null;
        if (!current()) return;
        clearStream(); later('preview_no_frames');
      }, FRAME_WAIT_MS);
    };
    arm();
    const off = options.observeFrames?.(selected, count => {
      if (!current() || !Number.isSafeInteger(count) || count <= presented) return;
      if (presented === 0) state('live');
      presented = count; arm();
    });
    if (typeof off === 'function') cancelFrames = off;
  }
  async function acquire() {
    if (!active() || acquisitionPending) return;
    acquisitionPending = true;
    const attempt = generation;
    const target = { ...binding };
    state('connecting');
    try { await serial(async () => {
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
        options.onStream(stream); watchFrames(stream, attempt);
      } catch (error) {
        if (captured && captured === stream) clearStream();
        else stopPreviewTracks(captured);
        if (active() && attempt === generation) {
          const denied = error?.name === 'NotAllowedError' || error?.message === 'preview_denied';
          later(denied ? 'preview_denied' : 'preview_unavailable');
        }
      } finally {
        clearTimeout(timeout); acquisitionTimers.delete(timeout);
      }
    }); } finally {
      acquisitionPending = false;
      // Keep only the latest wakeup for this session while another native call
      // owns the global queue. That call still retains its slot until settled.
      if (active() && attempt !== generation && timer === null) {
        timer = schedule(() => { timer = null; void acquire(); }, RETRY_DELAYS[0]);
      }
    }
  }
  /** @param {{capsule_id:string,feed_id:string,generation:string,preview_available?:boolean|null,preview_revision?:string|null}|null} value */
  function setBinding(value) {
    const identity = value === null ? '' : JSON.stringify([value.capsule_id, value.feed_id, value.generation]);
    const key = value === null ? '' : JSON.stringify([identity, value.preview_available ?? null, value.preview_revision ?? null]);
    if (key === bindingKey) return false;
    const identityChanged = identity !== identityKey;
    stop(); bindingKey = key; identityKey = identity; binding = value === null ? null : { ...value }; retry = 0;
    state('no_preview', value?.preview_available === false ? 'no_preview' : '');
    // A changed current projection is a wakeup, not an approval. Coalesce rapid
    // layout changes and always ask main for a fresh single-use grant.
    if (active()) {
      if (identityChanged) void acquire();
      else timer = schedule(() => { timer = null; void acquire(); }, RETRY_DELAYS[0]);
    }
    return true;
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
