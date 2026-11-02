// @ts-check
'use strict';

// Display only: recording stays in the Rust core. Validated on real Windows.
const LIVE_PREVIEW_ENABLED = true;
const BOOT_URL = 'cafe://app/bootstrap';
const ORIGIN = 'cafe://app';
/** Electron reports this origin with or without the trailing slash. @param {unknown} value */
function appOrigin(value) { return value === ORIGIN || value === ORIGIN + '/'; }
const GRANT_MS = 5000;

/** @param {unknown} value @returns {value is Record<string, any>} */
function record(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}
/** @param {unknown} value @returns {value is string} */
function identifier(value) { return typeof value === 'string' && value === value.trim() && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value); }
/** @param {unknown} value @returns {value is string} */
function positiveClock(value) {
  return typeof value === 'string' && value === value.trim() && /^[1-9][0-9]{0,19}$/.test(value)
    && BigInt(value) <= 18446744073709551615n;
}
/** @param {string} error */
function refused(error) { return Object.freeze({ ok: false, error }); }

/**
 * @typedef {object} PreviewOptions
 * @property {() => import('electron').WebContents|null} getContents
 * @property {() => import('./backendSupervisor').SupervisorStatus|null} getStatus
 * @property {(options: {types: ['window'], thumbnailSize: {width: number, height: number}, fetchWindowIcons: boolean}) => Promise<import('electron').DesktopCapturerSource[]>} getSources
 * @property {() => number} [now] Host clock injection for deterministic expiry tests.
 */
/**
 * @typedef {object} Grant
 * @property {object} frame
 * @property {string} capsule_id
 * @property {string} feed_id
 * @property {string} source_id
 * @property {string} generation
 * @property {string} epoch
 * @property {number} expires
 */

/**
 * @typedef {object} PreviewController
 * @property {(event: unknown, payload: unknown) => Promise<Readonly<Record<string, unknown>>>} request
 * @property {(contents: unknown, permission: string, details: unknown) => boolean} permissionRequest
 * @property {(contents: unknown, permission: string, origin: string, details: unknown) => boolean} permissionCheck
 * @property {(display: import('electron').DisplayMediaRequestHandlerHandlerRequest, callback: (streams: import('electron').Streams|null) => void) => Promise<void>} displayRequest
 * @property {(session: import('electron').Session) => void} install
 * @property {() => void} dispose
 */

/**
 * A single-use, short-lived main-frame selection; source identities never leave
 * main. The renderer serializes each grant and its getDisplayMedia acquisition.
 * The host-only enabled argument tests both policies; production uses the one
 * constant above. No renderer flag can enable a permission.
 * @param {PreviewOptions} options
 * @param {boolean} [enabled]
 * @returns {PreviewController}
 */
function createPreviewSources(options, enabled = LIVE_PREVIEW_ENABLED) {
  const now = options.now || Date.now;
  /** @type {Grant|null} */ let pending = null;
  /** @type {Grant|null} */ let acquiring = null;
  let requesting = false;
  let disposed = false;
  let installed = false;

  /** @param {unknown} contents @param {unknown} frame */
  function currentFrame(contents, frame) {
    try {
      const current = options.getContents();
      return !!current && !current.isDestroyed() && contents === current
        && frame === current.mainFrame && current.mainFrame.parent === null
        && current.mainFrame.detached === false && current.mainFrame.url === BOOT_URL;
    } catch { return false; }
  }

  /** @param {unknown} payload */
  function binding(payload) {
    if (!record(payload) || Object.keys(payload).sort().join(',') !== 'capsule_id,feed_id'
        || !identifier(payload.capsule_id) || !identifier(payload.feed_id)) return null;
    try {
      const status = options.getStatus();
      if (!status || status.availability !== 'available' || !positiveClock(status.authority_epoch) || !record(status.state)) return null;
      const state = status.state;
      if (state.platform !== 'windows' || state.runtime !== 'authority_ready') return null;
      if (!record(state.capsules) || !Array.isArray(state.preview_sources) || state.preview_sources.length > 24) return null;
      const seen = new Set();
      for (const row of state.preview_sources) {
        if (!record(row) || Object.keys(row).sort().join(',') !== 'capsule_id,feed_id,source_id'
            || !identifier(row.capsule_id) || !identifier(row.feed_id)
            || typeof row.source_id !== 'string' || row.source_id.length > 96 || row.source_id !== row.source_id.trim()
            || !/^window:[0-9]+:0$/.test(row.source_id)) return null;
        const key = JSON.stringify([row.capsule_id, row.feed_id]);
        if (seen.has(key)) return null;
        seen.add(key);
      }
      const rows = state.preview_sources.filter(row => row.capsule_id === payload.capsule_id && row.feed_id === payload.feed_id);
      const capsules = Object.values(state.capsules).filter(row => record(row) && row.capsule_id === payload.capsule_id);
      if (rows.length !== 1 || capsules.length !== 1 || !record(capsules[0].feeds)) return null;
      const feeds = Object.values(capsules[0].feeds).filter(row => record(row) && row.feed_id === payload.feed_id);
      if (feeds.length !== 1 || !positiveClock(feeds[0].generation)) return null;
      return { capsule_id: payload.capsule_id, feed_id: payload.feed_id,
        source_id: rows[0].source_id, generation: feeds[0].generation, epoch: status.authority_epoch };
    } catch { return null; }
  }

  /** @param {Grant} grant */
  function stillCurrent(grant) {
    try {
      if (disposed || !installed || enabled !== true || now() >= grant.expires
          || !currentFrame(options.getContents(), grant.frame)) return false;
      const latest = binding({ capsule_id: grant.capsule_id, feed_id: grant.feed_id });
      return latest !== null && latest.source_id === grant.source_id
        && latest.generation === grant.generation && latest.epoch === grant.epoch;
    } catch { return false; }
  }

  /** @returns {Promise<import('electron').DesktopCapturerSource[]>} */
  async function sources() {
    /** @type {ReturnType<typeof setTimeout>|undefined} */ let timer;
    try {
      const values = await Promise.race([
        options.getSources({ types: ['window'], thumbnailSize: { width: 0, height: 0 }, fetchWindowIcons: false }),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('PREVIEW_UNAVAILABLE')), 3000); }),
      ]);
      return Array.isArray(values) ? values : [];
    } finally { if (timer !== undefined) clearTimeout(timer); }
  }

  /** @param {unknown} event @param {unknown} payload */
  async function request(event, payload) {
    if (typeof event !== 'object' || event === null) return refused('preview_denied');
    const caller = /** @type {import('electron').IpcMainInvokeEvent} */ (event);
    if (!currentFrame(caller.sender, caller.senderFrame)) return refused('preview_denied');
    if (enabled !== true) return refused('preview_disabled');
    if (disposed || !installed) return refused('preview_unavailable');
    if (!record(payload) || Object.keys(payload).sort().join(',') !== 'capsule_id,feed_id'
        || !identifier(payload.capsule_id) || !identifier(payload.feed_id)) return refused('preview_denied');
    if (pending && !stillCurrent(pending)) pending = null;
    if (requesting || pending || acquiring) return refused('preview_busy');
    const selected = binding(payload);
    if (!selected) return refused('no_preview');
    const candidate = { ...selected, frame: /** @type {object} */ (caller.senderFrame), expires: now() + GRANT_MS };
    requesting = true;
    try {
      const available = await sources();
      if (!stillCurrent(candidate) || !currentFrame(caller.sender, caller.senderFrame)) return refused('no_preview');
      if (available.filter(source => source && source.id === candidate.source_id).length !== 1) return refused('no_preview');
      pending = candidate;
      return Object.freeze({ ok: true, capsule_id: candidate.capsule_id, feed_id: candidate.feed_id });
    } catch { return refused('preview_unavailable'); }
    finally { requesting = false; }
  }

  /** @param {unknown} contents @param {unknown} details */
  function permissionContext(contents, details) {
    const grant = pending || acquiring;
    return !!grant && stillCurrent(grant) && currentFrame(contents, grant.frame)
      && record(details) && details.isMainFrame === true && details.requestingUrl === BOOT_URL
      && (details.securityOrigin === undefined || appOrigin(details.securityOrigin));
  }

  /**
   * Electron 44 reports desktop capture as the generic media permission with no
   * media types; a typed request can be a camera or microphone and stays denied.
   * The same permission could admit a legacy desktop getUserMedia call, so it
   * opens only while main holds a live single-use grant for this exact frame.
   * @param {unknown} contents @param {string} permission @param {unknown} details
   */
  function permissionRequest(contents, permission, details) {
    return permission === 'media' && permissionContext(contents, details) && record(details)
      && Array.isArray(details.mediaTypes) && details.mediaTypes.length === 0;
  }

  /** @param {unknown} contents @param {string} permission @param {string} origin @param {unknown} details */
  function permissionCheck(contents, permission, origin, details) {
    return permission === 'media' && appOrigin(origin) && permissionContext(contents, details)
      && record(details) && details.mediaType === undefined;
  }

  /**
   * @param {import('electron').DisplayMediaRequestHandlerHandlerRequest} display
   * @param {(streams: import('electron').Streams|null) => void} callback
   */
  async function displayRequest(display, callback) {
    let answered = false;
    /** @param {import('electron').Streams|null} streams */
    const finish = (streams) => { if (answered) return; answered = true; try { callback(streams); } catch {} };
    const grant = pending;
    // A malformed request cannot steal a different document's pending grant.
    if (!grant || !stillCurrent(grant) || !display || display.frame !== grant.frame
        || !appOrigin(display.securityOrigin) || display.videoRequested !== true || display.audioRequested !== false) {
      finish(null); return;
    }
    pending = null;
    acquiring = grant;
    try {
      const available = await sources();
      const selected = available.filter(source => source && source.id === grant.source_id);
      if (acquiring !== grant || !stillCurrent(grant) || selected.length !== 1) { finish(null); return; }
      // Only the exact DesktopCapturerSource returned by this enumeration enters
      // Electron. No audio, system picker or renderer-selected id is accepted.
      finish({ video: selected[0] });
    } catch { finish(null); }
    finally { if (acquiring === grant) acquiring = null; }
  }

  /** @param {import('electron').Session} session */
  function install(session) {
    if (disposed || installed || typeof session.setDisplayMediaRequestHandler !== 'function') return;
    try {
      session.setDisplayMediaRequestHandler((display, callback) => { void displayRequest(display, callback); }, { useSystemPicker: false });
      installed = true;
    } catch { installed = false; }
  }

  function dispose() { disposed = true; pending = null; acquiring = null; }
  return Object.freeze({ request, permissionRequest, permissionCheck, displayRequest, install, dispose });
}

module.exports = { LIVE_PREVIEW_ENABLED, createPreviewSources };
