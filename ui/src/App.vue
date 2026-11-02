<script setup>
// @ts-check
/**
 * Cafe controls workspace (presentation-only).
 *
 * Binds the parent-owned controller facade from `src/renderer/control.js`
 * to truthful product regions: capsules, windows, recording setup, OBS and
 * chat. Authoritative snapshots own all state; this unit never invents
 * capsules, feeds, recordings, progress or errors, and never validates or
 * generates ids. Every request resolves to a new snapshot or the fixed
 * failure note. Unmount only removes renderer listeners; authority and
 * recording are never stopped from here.
 *
 * Diagnostics are read only on an explicit operator click (show/refresh)
 * through the parent read-only helper; only allowlisted report fields are
 * rendered as plain text, never logged, stored, copied or serialized.
 *
 * @module ui/App
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import TopBar from './kit/shell/TopBar.vue';
import StatusBar from './kit/shell/StatusBar.vue';
import CapsuleTopbar from './capsule/topbar.vue';
import CapsuleControl from './capsule/control.vue';
import CapsuleGrid from './capsule/grid.vue';
import CapsulePreview from './capsule/capture.vue';
import OrganizerPlan from './capsule/operator.vue';
import { actionResultLabel, createRecordingTimer, tileState, waitForAuthorityResult, waitForProjectedMatch } from './capsule/control.js';
import { createRegistry } from './capsule/registry.js';
import WindowBrowser from './windows.vue';
import { createWindowCatalogue } from './window_catalogue.js';
import { observeWindowPage } from './window_queries.js';
import './workspace.css';
import { createControls, readDiagnostics } from '../../src/renderer/control.js';

/** Fixed note for any failed controller request; raw errors never surface. */
const REQUEST_FAILED = 'Request failed; refresh and try again';

/** Fixed disabled reasons. */
const REASON_UNAVAILABLE = 'Runtime unavailable';
const REASON_STARTING = 'Runtime starting';
const REASON_PLATFORM = 'Capture needs Windows';
const REASON_FOLDER = 'Choose an output folder first';
const REASON_FFMPEG = 'Choose FFmpeg first';
const REASON_PAIRING = 'Pair a window feed first';
const REASON_CAPSULE = 'Capsule unavailable';
const REASON_SELECT_WINDOW = 'Select a listed window';
const REASON_OBS_PENDING = 'OBS setup pending';
const REASON_OBS_NOT_READY = 'OBS not ready';
const REASON_CHAT_PENDING = 'Chat setup pending';
const REASON_NOT_RECORDING = 'Not recording';
const REASON_NO_PREV = 'No previous page';
const REASON_NO_NEXT = 'No next page';

/** Fixed diagnostics notes; raw errors and unknown values never surface. */
const DIAGNOSTICS_FAILED = 'Diagnostics unavailable';
const DIAGNOSTICS_NONE = 'no data';
const DIAGNOSTICS_UNKNOWN = 'unknown';

/**
 * Allowlisted diagnostic report fields, in display order, with fixed labels.
 * Anything else the report might carry is never rendered.
 */
const DIAGNOSTIC_FIELDS = Object.freeze([
  Object.freeze({ key: 'schema', label: 'Schema' }),
  Object.freeze({ key: 'bridge', label: 'Bridge' }),
  Object.freeze({ key: 'result_state', label: 'Last result state' }),
  Object.freeze({ key: 'result_code', label: 'Last result code' }),
  Object.freeze({ key: 'capsule_count', label: 'Capsules' }),
  Object.freeze({ key: 'paired_feed_count', label: 'Paired feeds' }),
  Object.freeze({ key: 'listed_window_count', label: 'Listed windows' }),
  Object.freeze({ key: 'minimized_window_count', label: 'Minimized windows' }),
  Object.freeze({ key: 'visible_window_count', label: 'Visible windows' }),
]);

/** Pairing roles, exactly the parent contract. */
const pairRoles = ['game', 'camera'];

/**
 * Unavailable view matching the parent stub shape. Displayed until the
 * first authoritative snapshot arrives; no invented content.
 */
const INITIAL_VIEW = Object.freeze({
  availability: 'unavailable',
  platform: 'unknown',
  capsules: [],
  windows: [],
  windowPage: { offset: 0, nextOffset: null, total: 0 },
  recordings: [],
  folderConfigured: false,
  ffmpegConfigured: false,
  obsReady: false,
  obsConfigurable: false,
  chatReady: false,
  chatConfigurable: false,
  lastResult: null,
});

/**
 * @typedef {object} PairForm UI-only pairing data for one capsule role.
 * @property {string} token Selected listed-window token, '' when none.
 * @property {number} x Crop x.
 * @property {number} y Crop y.
 * @property {number} w Crop width.
 * @property {number} h Crop height.
 */

/** @type {import('vue').Ref<any>} Authoritative snapshot, adopted as-is. */
const view = ref(INITIAL_VIEW);
/** @type {import('vue').Ref<'dark' | 'light'>} */
const theme = ref('dark');
/** @type {import('vue').Ref<string>} Fixed failure note, '' when none. */
const notice = ref('');
/** @type {import('vue').Ref<number>} In-flight request count. */
const pendingCount = ref(0);
/** @type {import('vue').Ref<string>} */
const personLabel = ref('');
/** @type {import('vue').Ref<string>} */
const channel = ref('');
/** @type {import('vue').Ref<string>} Cleared after every submit, never shown. */
const password = ref('');
/** @type {import('vue').Ref<number | string>} */
const obsPort = ref('');
/** @type {import('vue').Ref<Record<string, PairForm>>} */
const pairForms = ref({});
/** @type {import('vue').Ref<number[]>} Visited window offsets for paging back. */
const prevOffsets = ref([]);
/** @type {import('vue').Ref<boolean>} Diagnostics panel visibility. */
const diagnosticsOpen = ref(false);
/** @type {import('vue').Ref<boolean>} One explicit diagnostics read in flight. */
const diagnosticsPending = ref(false);
/** @type {import('vue').Ref<Array<{ key: string, label: string, value: string }>>} */
const diagnosticsRows = ref([]);
/** @type {import('vue').Ref<string>} Fixed diagnostics note, '' when none. */
const diagnosticsNote = ref('');
/** @type {import('vue').Ref<'capsules' | 'settings' | 'diagnostics'>} View-only page; switching never stops authority/recorder. */
const activePage = ref('capsules');
const previewsEnabled = ref(true);
/** @type {import('vue').Ref<Record<string, Record<string,string>>>} */
const previewStates = ref({});
/** @type {import('vue').Ref<Record<string,boolean>>} */
const tilePending = ref({});
/** @type {import('vue').Ref<Record<string,string>>} */
const tileResults = ref({});
const batchPending = ref(false);
const batchNote = ref('');
const organizerPending = ref(false);
const organizerResult = ref('');
const actionAbort = new AbortController();
let capsuleQueue = Promise.resolve();
let appDisposed = false;
const helpMenuOpen = ref(false);
const browserOpen = ref(false);
const browserPending = ref(false);
const browserSelected = ref('');
const browserCapsule = ref('');
const browserRole = ref('game');
const browserStatus = ref('idle');
const browserRows = ref([]);
const browserTotal = ref(null);
const browserCrop = ref({ x: 0, y: 0, w: 0, h: 0 });
/** @type {import('vue').Ref<Record<string, string>>} */
const anchoredByCapsuleRole = ref({});
const catalogue = createWindowCatalogue();
let browserSequence = 0;
const hasFirstSnapshot = ref(false);
const timerTick = ref(Date.now());
const recordingTimer = createRecordingTimer(now => {
  timerTick.value = now;
});

/** Component-lifetime registry for display-only slot-N aliases; authority epoch not exposed, explicit ceiling. */
const capsuleRegistry = createRegistry({ maxSlots: 1000 });
/** Read sequence; results of closed or unmounted reads are dropped. */
let diagnosticsSeq = 0;

/** @type {ReturnType<typeof createControls> | null} Parent-owned controller. */
let controls = null;
/** @type {(() => void) | null} */
let stopControls = null;

/** @type {Array<{ label: string, tone?: 'dim' | 'strong' | 'accent' }>} */
const crumbs = [
  { label: 'Café', tone: 'dim' },
  { label: 'Workspace' },
];

/**
 * Read the closed `window.cafe` facade with an optional property read
 * only. No transport is exposed or touched here.
 *
 * @returns {unknown} The facade object, or undefined when absent.
 */
function cafeFacade() {
  if (typeof window === 'undefined') return undefined;
  const scope = /** @type {{ cafe?: unknown }} */ (/** @type {unknown} */ (window));
  if (scope.cafe === null || typeof scope.cafe !== 'object') return undefined;
  return scope.cafe;
}

/**
 * Adopt the authoritative snapshot as-is. Anything that is not an object
 * falls back to the fixed unavailable view; nothing is merged or invented.
 *
 * @param {unknown} snapshot
 */
function applyView(snapshot) {
  if (snapshot !== null && typeof snapshot === 'object') {
    view.value = snapshot;
    recordingTimer.sync(Array.isArray(view.value.recordings) ? view.value.recordings : []);
    if (view.value.lastResult?.code === 'request_or_capacity_conflict') notice.value = actionResultLabel(view.value.lastResult);
    if (browserOpen.value && view.value.availability !== 'available') {
      browserSequence += 1; catalogue.reset(); browserRows.value = [];
      browserTotal.value = null; browserSelected.value = ''; browserPending.value = false;
      browserStatus.value = 'unavailable';
    }
    return;
  }
  view.value = INITIAL_VIEW;
  recordingTimer.sync([]);
}

/**
 * Track one parent-owned request. Snapshots own all state; this only
 * drives the transient pending indicator and the fixed failure note.
 *
 * @param {Promise<unknown>} task
 */
function request(task) {
  pendingCount.value += 1;
  notice.value = '';
  task.then(
    () => {
      pendingCount.value -= 1;
    },
    () => {
      pendingCount.value -= 1;
      notice.value = REQUEST_FAILED;
    },
  );
}

/**
 * Coerce UI number-input data for the wire. Parent validation is
 * authoritative; this only keeps non-numeric input out of requests.
 *
 * @param {unknown} value
 * @returns {number}
 */
function toCount(value) {
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

/**
 * Render one authority value as text. Unknown shapes fall back to the
 * fixed placeholder; nothing is parsed or reformatted.
 *
 * @param {unknown} value
 * @param {string} [fallback]
 * @returns {string}
 */
function textOf(value, fallback = 'Unknown') {
  if (typeof value === 'string') return value === '' ? fallback : value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return fallback;
}

/**
 * @param {unknown} capsule
 * @returns {string} Authority capsule id, '' when absent.
 */
function cid(capsule) {
  if (capsule === null || typeof capsule !== 'object') return '';
  const id = /** @type {{ capsule_id?: unknown }} */ (capsule).capsule_id;
  return typeof id === 'string' ? id : '';
}

/**
 * @param {unknown} capsule
 * @param {number} index
 * @returns {string} Stable render key; the fallback never leaves the view.
 */
function ckey(capsule, index) {
  const id = cid(capsule);
  return id !== '' ? id : 'capsule-' + index;
}

/**
 * @param {unknown} capsule
 * @returns {string}
 */
function cname(capsule) {
  if (capsule === null || typeof capsule !== 'object') return 'Unnamed capsule';
  return textOf(
    /** @type {{ person_label?: unknown }} */ (capsule).person_label,
    'Unnamed capsule',
  );
}

/**
 * @param {unknown} capsule
 * @returns {any[]}
 */
function capsuleFeeds(capsule) {
  if (capsule === null || typeof capsule !== 'object') return [];
  const feeds = /** @type {{ feeds?: unknown }} */ (capsule).feeds;
  return Array.isArray(feeds) ? feeds : [];
}

/**
 * @param {unknown} capsule
 * @param {string} role
 * @returns {any|undefined}
 */
function feedFor(capsule, role) {
  return capsuleFeeds(capsule).find(
    (feed) => feed !== null && typeof feed === 'object' && feed.role === role,
  );
}

/**
 * @param {unknown} feed
 * @returns {string}
 */
function feedLine(feed) {
  if (feed === null || typeof feed !== 'object') return 'Unknown feed';
  const fields = /** @type {{ feed_id?: unknown, role?: unknown, generation?: unknown }} */ (feed);
  return (
    textOf(fields.role) + ' feed ' + textOf(fields.feed_id) +
    ' (generation ' + textOf(fields.generation) + ')'
  );
}

/**
 * @param {string} role
 * @returns {string}
 */
function slotTitle(role) {
  return role === 'camera' ? 'Camera feed' : 'Game feed';
}

/**
 * @param {string} key Render key from ckey.
 * @param {string} role
 * @returns {PairForm}
 */
function pairForm(key, role) {
  const name = key + ' ' + role;
  const existing = pairForms.value[name];
  if (existing !== undefined) return existing;
  const fresh = { token: '', x: 0, y: 0, w: 0, h: 0 };
  pairForms.value[name] = fresh;
  return fresh;
}

/**
 * Find the selected token in the actual listed windows. Pair requests
 * only ever use the token, generation and layout revision found here.
 *
 * @param {unknown} token
 * @returns {any|undefined}
 */
function listedWindow(token) {
  if (typeof token !== 'string' || token === '') return undefined;
  return windows.value.find(
    (entry) => entry !== null && typeof entry === 'object' && entry.token === token,
  );
}

/**
 * Adopt the actual window rect as an editable crop suggestion. The
 * backend owns the real check; this never constrains the inputs.
 *
 * @param {string} key Render key from ckey.
 * @param {string} role
 */
function suggestCrop(key, role) {
  const form = pairForm(key, role);
  const target = listedWindow(form.token);
  form.x = 0;
  form.y = 0;
  const rect = target === undefined ? null : target.rect;
  form.w = rect !== null && typeof rect === 'object' ? toCount(rect.width) : 0;
  form.h = rect !== null && typeof rect === 'object' ? toCount(rect.height) : 0;
}

/**
 * @param {unknown} capsule
 * @returns {any|undefined}
 */
function recFor(capsule) {
  const id = cid(capsule);
  if (id === '') return undefined;
  return recordings.value.find(
    (entry) => entry !== null && typeof entry === 'object' && entry.capsule_id === id,
  );
}

/**
 * @param {unknown} rec
 * @returns {any[]}
 */
function recFeeds(rec) {
  if (rec === null || typeof rec !== 'object') return [];
  const feeds = /** @type {{ feeds?: unknown }} */ (rec).feeds;
  return Array.isArray(feeds) ? feeds : [];
}

/**
 * @param {unknown} feed
 * @returns {string}
 */
function recFeedLine(feed) {
  if (feed === null || typeof feed !== 'object') return 'Unknown feed';
  const fields = /** @type {{ feed_id?: unknown, state?: unknown, written_frames?: unknown, backpressure?: unknown }} */ (feed);
  return (
    textOf(fields.feed_id) + ' · ' + textOf(fields.state) + ' · ' +
    textOf(fields.written_frames) + ' frames · backpressure ' + textOf(fields.backpressure)
  );
}

/**
 * @param {unknown} entry
 * @returns {string}
 */
function windowTitle(entry) {
  if (entry === null || typeof entry !== 'object') return 'Unknown window';
  return textOf(
    /** @type {{ title?: unknown }} */ (entry).title,
    'Unknown window',
  );
}

/**
 * @param {unknown} entry
 * @returns {string}
 */
function windowMeta(entry) {
  if (entry === null || typeof entry !== 'object') return 'Unknown';
  const fields = /** @type {{ class_name?: unknown, exe?: unknown }} */ (entry);
  return textOf(fields.class_name) + ' · ' + textOf(fields.exe);
}

/**
 * @param {unknown} entry
 * @returns {string}
 */
function windowLabel(entry) {
  if (entry === null || typeof entry !== 'object') return 'Unknown window';
  const exe = textOf(/** @type {{ exe?: unknown }} */ (entry).exe, '');
  const title = windowTitle(entry);
  return exe === '' ? title : title + ' · ' + exe;
}

/**
 * @param {unknown} entry
 * @returns {string} Opaque token text, never parsed.
 */
function wtokenText(entry) {
  if (entry === null || typeof entry !== 'object') return 'Unknown';
  return textOf(/** @type {{ token?: unknown }} */ (entry).token);
}

/**
 * @param {unknown} entry
 * @param {number} index
 * @returns {string}
 */
function wkey(entry, index) {
  if (entry !== null && typeof entry === 'object') {
    const token = /** @type {{ token?: unknown }} */ (entry).token;
    if (typeof token === 'string' && token !== '') return token;
  }
  return 'window-' + index;
}

/**
 * Select value for a listed window. Entries without a usable token get a
 * placeholder value that can never match a listed window.
 *
 * @param {unknown} entry
 * @param {number} index
 * @returns {string}
 */
function wtoken(entry, index) {
  if (entry !== null && typeof entry !== 'object') return 'missing-' + index;
  const token = /** @type {{ token?: unknown }} */ (entry).token;
  if (typeof token === 'string' && token !== '') return token;
  return 'missing-' + index;
}

/**
 * @param {number} index
 * @param {string} role
 * @param {string} field
 * @returns {string}
 */
function pairInputId(index, role, field) {
  return 'cafe-pair-' + index + '-' + role + '-' + field;
}

/**
 * @returns {{ offset: number, nextOffset: unknown, total: unknown }}
 */
function currentPage() {
  const page = view.value.windowPage;
  if (page === null || typeof page !== 'object') {
    return { offset: 0, nextOffset: null, total: 0 };
  }
  return {
    offset: typeof page.offset === 'number' && Number.isFinite(page.offset) ? page.offset : 0,
    nextOffset: page.nextOffset,
    total: page.total,
  };
}

/** @type {import('vue').ComputedRef<'starting' | 'available' | 'unavailable'>} */
const availability = computed(() => {
  const value = view.value.availability;
  return value === 'available' || value === 'starting' ? value : 'unavailable';
});

/** @type {import('vue').ComputedRef<'windows' | 'unsupported' | 'unknown'>} */
const platform = computed(() => {
  const value = view.value.platform;
  return value === 'windows' || value === 'unsupported' ? value : 'unknown';
});

/** @type {import('vue').ComputedRef<any[]>} */
const capsules = computed(() => (Array.isArray(view.value.capsules) ? view.value.capsules : []));
/** @type {import('vue').ComputedRef<any[]>} */
const windows = computed(() => (Array.isArray(view.value.windows) ? view.value.windows : []));
/** @type {import('vue').ComputedRef<any[]>} */
const recordings = computed(() => (
  Array.isArray(view.value.recordings) ? view.value.recordings : []
));

const folderConfigured = computed(() => view.value.folderConfigured === true);
const ffmpegConfigured = computed(() => view.value.ffmpegConfigured === true);
const obsReady = computed(() => view.value.obsReady === true);
const obsConfigurable = computed(() => view.value.obsConfigurable === true);
const chatReady = computed(() => view.value.chatReady === true);
const chatConfigurable = computed(() => view.value.chatConfigurable === true);
const pageTotal = computed(() => currentPage().total);
const isPending = computed(() => pendingCount.value > 0);
/** Exact host-issued id check, no numeric coercion. */
function isExactCapsuleId(value) {
  return typeof value === 'string' && value.length > 0 && value === value.trim() && !/\s/.test(value) && !/^\d+$/.test(value);
}
/** Raw ids in snapshot order; invalid kept so grid reports invalid, never empty success. */
const rawCapsuleIds = computed(() => capsules.value.map((entry) => cid(entry)));
/** Grid availability only when authority is available. */
const gridAvailable = computed(() => availability.value === 'available');
const isStarting = computed(() => !hasFirstSnapshot.value || availability.value === 'starting');
/** @param {string} id @returns {string} */
function elapsedFor(id) {
  void timerTick.value;
  return recordingTimer.elapsedFor(id, timerTick.value);
}
/** Validated ids for registry display only; guards invalid/duplicate/overflow before assign. */
const workspaceIds = computed(() => {
  const ids = rawCapsuleIds.value;
  const seen = new Set();
  for (const id of ids) {
    if (!isExactCapsuleId(id) || seen.has(id)) return [];
    seen.add(id);
  }
  try {
    capsuleRegistry.assign(ids);
  } catch {
    return [];
  }
  return ids.slice();
});
/** Display-only slot alias, unknown on any failure. */
function slotLabelFor(id) {
  void workspaceIds.value;
  try {
    const label = capsuleRegistry.displayFor(id);
    return typeof label === 'string' ? label : 'unknown';
  } catch {
    return 'unknown';
  }
}
/** Display-only tab labels ("slot-N · person label") keyed by capsuleId. */
const capsuleTabLabels = computed(() => {
  /** @type {Record<string, string>} */
  const out = {};
  for (const capsule of capsules.value) {
    const id = cid(capsule);
    if (id !== '') out[id] = `${slotLabelFor(id)} · ${cname(capsule)}`;
  }
  return out;
});
/** Authoritative window tokens currently anchored, mapped to their slot-N label. */
const anchoredWindows = computed(() => {
  /** @type {Record<string, string>} */
  const out = {};
  for (const capsule of capsules.value) {
    const id = cid(capsule);
    if (id === '') continue;
    const slot = slotLabelFor(id);
    for (const feed of capsuleFeeds(capsule)) {
      const token = typeof feed?.window_token === 'string' && feed.window_token !== ''
        ? feed.window_token
        : anchoredByCapsuleRole.value[`${id}:${feed?.role}`];
      if (typeof token === 'string' && token !== '') {
        out[token] = slot;
      }
    }
  }
  return out;
});
/** Current capsule by actual id from latest snapshot; undefined rejects stale/unknown. */
function capsuleById(id) {
  if (!isExactCapsuleId(id)) return undefined;
  const found = capsules.value.filter((entry) => cid(entry) === id);
  return found.length === 1 ? found[0] : undefined;
}
/** Index only for DOM input labels; never an authority identity. */
function capsuleIndexFor(id) { return capsules.value.findIndex((entry) => cid(entry) === id); }
/** Recording truth mapped to topbar allowlist, else unknown. */
function headerStateFor(capsule) {
  return tileState(capsule, recFor(capsule), previewStates.value[cid(capsule)] || {});
}
/** Disabled reasons reuse existing guards plus pending. */
function recordReasonFor(capsule) {
  const base = startBlock(capsule);
  if (base !== '') return base;
  if (recFor(capsule)?.state === 'Running') return 'Already recording';
  return tilePending.value[cid(capsule)] ? 'Request pending…' : '';
}
function stopReasonFor(capsule) {
  const base = stopBlock(capsule);
  if (base !== '') return base;
  return tilePending.value[cid(capsule)] ? 'Request pending…' : '';
}
function obsReasonFor(capsule) {
  const base = sendBlock(capsule);
  if (base !== '') return base;
  return isPending.value || browserPending.value ? 'Request pending…' : '';
}
function onCardRecord(id) {
  const current = capsuleById(id);
  if (current === undefined || recordReasonFor(current) !== '') return;
  onStart(current);
}
function onCardStop(id) {
  const current = capsuleById(id);
  if (current === undefined || stopReasonFor(current) !== '') return;
  onStop(current);
}
function onCardObs(id) {
  const current = capsuleById(id);
  if (current === undefined || obsReasonFor(current) !== '') return;
  onSendToObs(current);
}
/** View-only page switch; never stops authority/recorder. */
function showPage(page) {
  if (page !== 'capsules' && page !== 'settings' && page !== 'diagnostics') return;
  activePage.value = page;
}

const organizerPlan = computed(() => view.value.organizerPlan || null);
const autoAnchor = computed(() => typeof view.value.autoAnchor === 'boolean' ? view.value.autoAnchor : null);
const runningCount = computed(() => recordings.value.filter(row => row.state === 'Running').length);
function previewStateFor(id, feedId, state) {
  if (!['live', 'connecting', 'no_preview'].includes(state)) return;
  previewStates.value[id] = { ...(previewStates.value[id] || {}), [feedId]: state };
}
function resolutionLabel(capsule) {
  const reason = startBlock(capsule);
  return reason === REASON_PAIRING ? 'Choose window' : reason === REASON_FOLDER || reason === REASON_FFMPEG ? 'Open settings'
    : reason === REASON_UNAVAILABLE || reason === REASON_STARTING ? 'Open diagnostics' : '';
}
function resolveRecordingBlock(id) {
  const current = capsuleById(id);
  if (!current) return;
  const reason = startBlock(current);
  if (reason === REASON_PAIRING) openWindowBrowser(id);
  else showPage(reason === REASON_UNAVAILABLE || reason === REASON_STARTING ? 'diagnostics' : 'settings');
}
function failureReasonFor(capsule) {
  const reason = recFor(capsule)?.reason;
  return typeof reason === 'string' && reason !== '' ? reason + ' · review setup and retry.'
    : 'Recording reason unavailable. Open diagnostics before retrying.';
}

/** Serial commands retain independent per-capsule pending/results. Queued is never success. */
function queueCapsuleAction(id, op) {
  if (tilePending.value[id] || appDisposed) return Promise.resolve({ status: 'blocked' });
  tilePending.value[id] = true;
  tileResults.value[id] = '';
  const task = capsuleQueue.then(async () => {
    if (appDisposed || !controls) return { status: 'cancelled' };
    const current = capsuleById(id);
    const blocked = !current || availReason.value !== '' || isPending.value || browserPending.value
      || (op === 'start' && (startBlock(current) !== '' || recFor(current)?.state === 'Running'
        || ['connecting', 'window_minimized', 'window_closed'].includes(headerStateFor(current))))
      || (op === 'stop' && stopBlock(current) !== '')
      || (op === 'remove' && recFor(current)?.state === 'Running');
    if (blocked) return { status: 'blocked' };
    try {
      const reply = await (op === 'start' ? controls.startRecording(id) : op === 'stop' ? controls.stopRecording(id) : controls.removeCapsule(id));
      if (reply.cancelled === true) return { status: 'cancelled' };
      if (reply.queued !== true || typeof reply.request_id !== 'string') return { status: 'unconfirmed' };
      tileResults.value[id] = actionResultLabel({ status: 'queued' });
      return await waitForAuthorityResult(controls, reply.request_id, { signal: actionAbort.signal });
    } catch { return { status: 'failed' }; }
  }).then(result => {
    if (!appDisposed) { tileResults.value[id] = actionResultLabel(result); tilePending.value[id] = false; }
    return result;
  });
  capsuleQueue = task.then(() => undefined, () => undefined);
  return task;
}
function onRemoveCapsule(id) { void queueCapsuleAction(id, 'remove'); }
async function onBatch(op) {
  if (batchPending.value || appDisposed) return;
  batchPending.value = true; batchNote.value = '';
  const ids = capsules.value.filter(capsule => op === 'stop' ? recFor(capsule)?.state === 'Running' : recFor(capsule)?.state !== 'Running').map(cid);
  try {
    for (const id of ids) {
      const result = await queueCapsuleAction(id, op);
      if (appDisposed) return;
      if (result.status === 'unconfirmed' || result.status === 'cancelled' || result.status === 'failed' || ['rejected', 'blocked', 'interrupted'].includes(result?.state)) {
        batchNote.value = 'Batch paused. Review the result on each capsule before retrying.'; return;
      }
    }
    batchNote.value = '';
  } finally { if (!appDisposed) batchPending.value = false; }
}
async function onOrganizer(planId, confirm) {
  if (!controls || organizerPending.value || organizerPlan.value?.plan_id !== planId) return;
  organizerPending.value = true; organizerResult.value = '';
  try {
    const reply = await (confirm ? controls.confirmOrganizerPlan(planId) : controls.rejectOrganizerPlan(planId));
    organizerResult.value = actionResultLabel({ status: 'queued' });
    const result = await waitForAuthorityResult(controls, reply.request_id, { signal: actionAbort.signal });
    if (!appDisposed) organizerResult.value = actionResultLabel(result);
  } catch { organizerResult.value = REQUEST_FAILED; }
  finally { organizerPending.value = false; }
}
function onAutoAnchor(event) {
  const enabled = event?.target?.checked;
  if (!controls || autoAnchor.value === null || typeof enabled !== 'boolean' || isPending.value) return;
  // Reflect only authoritative state while the command awaits its next snapshot.
  event.target.checked = autoAnchor.value;
  request(controls.setAutoAnchor(enabled));
}

const statusLabel = computed(() =>
  availability.value === 'available'
    ? 'Available'
    : availability.value === 'starting'
      ? 'Starting'
      : 'Unavailable',
);
const platformLabel = computed(() =>
  platform.value === 'windows'
    ? 'Windows'
    : platform.value === 'unsupported'
      ? 'Unsupported'
      : 'Unknown',
);
const platformWarning = computed(() =>
  platform.value === 'unsupported'
    ? 'Capture is not supported on this platform.'
    : platform.value === 'windows'
      ? 'Windows hardware is unverified; recording may block.'
      : '',
);
const themeLabel = computed(() => (theme.value === 'light' ? 'dark' : 'light'));
const telemetry = computed(() => [
  { key: 'Bridge', value: statusLabel.value },
  { key: 'Capture', value: platformLabel.value },
]);

/** Fixed availability reason, or '' when requests may be offered. */
const availReason = computed(() =>
  availability.value === 'starting'
    ? REASON_STARTING
    : availability.value === 'available'
      ? ''
      : REASON_UNAVAILABLE,
);
const prevReason = computed(() =>
  (availReason.value !== ''
    ? availReason.value
    : prevOffsets.value.length === 0
      ? REASON_NO_PREV
      : ''),
);
const nextReason = computed(() => {
  if (availReason.value !== '') return availReason.value;
  return typeof currentPage().nextOffset === 'number' ? '' : REASON_NO_NEXT;
});
const obsReason = computed(() =>
  (availReason.value !== ''
    ? availReason.value
    : obsConfigurable.value
      ? ''
      : REASON_OBS_PENDING),
);
const chatReason = computed(() =>
  (availReason.value !== ''
    ? availReason.value
    : chatConfigurable.value
      ? ''
      : REASON_CHAT_PENDING),
);

/**
 * @param {unknown} capsule
 * @returns {string} Fixed reason, or '' when Start may be offered.
 */
function startBlock(capsule) {
  if (availReason.value !== '') return availReason.value;
  if (cid(capsule) === '') return REASON_CAPSULE;
  if (platform.value !== 'windows') return REASON_PLATFORM;
  if (folderConfigured.value !== true) return REASON_FOLDER;
  if (ffmpegConfigured.value !== true) return REASON_FFMPEG;
  if (capsuleFeeds(capsule).length === 0) return REASON_PAIRING;
  return '';
}

/**
 * @param {unknown} capsule
 * @returns {string} Fixed reason, or '' for an actual Running recording.
 */
function stopBlock(capsule) {
  if (availReason.value !== '') return availReason.value;
  if (cid(capsule) === '') return REASON_CAPSULE;
  const rec = recFor(capsule);
  if (rec === undefined || rec.state !== 'Running') return REASON_NOT_RECORDING;
  return '';
}

/**
 * @param {unknown} capsule
 * @returns {string} Fixed reason, or '' when Send may be offered.
 */
function sendBlock(capsule) {
  if (availReason.value !== '') return availReason.value;
  if (cid(capsule) === '') return REASON_CAPSULE;
  if (obsReady.value !== true) return REASON_OBS_NOT_READY;
  return '';
}

/**
 * @param {unknown} capsule
 * @param {number} index
 * @param {string} role
 * @returns {string} Fixed reason, or '' when Pair may be offered.
 */
function pairBlock(capsule, index, role) {
  if (availReason.value !== '') return availReason.value;
  if (cid(capsule) === '') return REASON_CAPSULE;
  if (listedWindow(pairForm(ckey(capsule, index), role).token) === undefined) {
    return REASON_SELECT_WINDOW;
  }
  return '';
}

function nextWindowLabel() {
  const used = new Set(capsules.value.map(c => cname(c)));
  let n = capsules.value.length + 1;
  while (used.has('Window ' + String(n))) n += 1;
  return 'Window ' + String(n);
}

function onCreateCapsule() {
  if (personLabel.value.trim() === '') personLabel.value = nextWindowLabel();
  if (controls === null || browserPending.value || isPending.value) return;
  if (capsules.value.length >= 12) { notice.value = 'The wall limit is 12 capsules. Remove a capsule before adding another.'; return; }
  request(controls.createCapsule(personLabel.value));
}

function onRefreshWindows() {
  if (controls === null || browserPending.value || isPending.value) return;
  prevOffsets.value = [];
  request(controls.refreshWindows(0));
}

function onPrevWindows() {
  if (controls === null || browserPending.value || isPending.value) return;
  const offset = prevOffsets.value.pop();
  if (typeof offset !== 'number') return;
  request(controls.refreshWindows(offset));
}

function onNextWindows() {
  if (controls === null || browserPending.value || isPending.value) return;
  const page = currentPage();
  if (typeof page.nextOffset !== 'number') return;
  prevOffsets.value.push(page.offset);
  request(controls.refreshWindows(page.nextOffset));
}

function onChooseFolder() {
  if (controls === null || browserPending.value || isPending.value) return;
  request(controls.chooseFolder());
}

function onChooseFfmpeg() {
  if (controls === null || browserPending.value || isPending.value) return;
  request(controls.chooseFfmpeg());
}

/**
 * @param {unknown} capsule
 * @param {number} index
 * @param {string} role
 */
function onPair(capsule, index, role) {
  if (controls === null || browserPending.value || isPending.value) return;
  const id = cid(capsule);
  if (id === '') return;
  const form = pairForm(ckey(capsule, index), role);
  const target = listedWindow(form.token);
  if (target === undefined) return;
  request(controls.pairWindow({
    capsuleId: id,
    role,
    windowToken: target.token,
    windowGeneration: target.generation,
    layoutRevision: target.layout_revision,
    crop: {
      x: toCount(form.x),
      y: toCount(form.y),
      width: toCount(form.w),
      height: toCount(form.h),
    },
  }));
}

/**
 * @param {unknown} capsule
 */
function onStart(capsule) {
  const id = cid(capsule);
  if (id === '') return;
  void queueCapsuleAction(id, 'start');
}

/**
 * @param {unknown} capsule
 */
function onStop(capsule) {
  const id = cid(capsule);
  if (id === '') return;
  void queueCapsuleAction(id, 'stop');
}

/**
 * @param {unknown} capsule
 */
function onSendToObs(capsule) {
  if (controls === null || browserPending.value || isPending.value) return;
  const id = cid(capsule);
  if (id === '') return;
  request(controls.sendToObs(id));
}

function onConfigureObs() {
  if (controls === null || browserPending.value || isPending.value) return;
  request(controls.configureObs(toCount(obsPort.value), password.value));
  password.value = '';
}

function onConfigureTwitch() {
  if (controls === null || browserPending.value || isPending.value) return;
  request(controls.configureTwitch(channel.value));
}

/**
 * Apply a local theme. Unknown identifiers are refused; dark is the
 * default and is represented by the absence of the attribute.
 *
 * @param {unknown} value
 */
function applyTheme(value) {
  if (value !== 'dark' && value !== 'light') return;
  theme.value = value;
  if (typeof document === 'undefined') return;
  if (value === 'light') {
    document.documentElement.setAttribute('data-cafe-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-cafe-theme');
  }
}

function toggleTheme() {
  applyTheme(theme.value === 'light' ? 'dark' : 'light');
}

/**
 * Render one diagnostic value. Only fixed lowercase codes and non-negative
 * safe-integer counts pass; null stays "no data", everything else is unknown.
 *
 * @param {unknown} value
 * @returns {string}
 */
function diagnosticValue(value) {
  if (value === null) return DIAGNOSTICS_NONE;
  if (typeof value === 'number') {
    return Number.isSafeInteger(value) && value >= 0 ? String(value) : DIAGNOSTICS_UNKNOWN;
  }
  if (typeof value === 'string' && /^[a-z0-9_.]{1,64}$/.test(value)) return value;
  return DIAGNOSTICS_UNKNOWN;
}

/**
 * Project a report through the field allowlist. Own data only; the report
 * object itself is never kept or serialized.
 *
 * @param {unknown} report
 * @returns {Array<{ key: string, label: string, value: string }>}
 */
function diagnosticRows(report) {
  const fields = /** @type {Record<string, unknown>} */ (
    report !== null && typeof report === 'object' ? report : {}
  );
  return DIAGNOSTIC_FIELDS.map((field) => ({
    key: field.key,
    label: field.label,
    value: diagnosticValue(
      Object.prototype.hasOwnProperty.call(fields, field.key) ? fields[field.key] : null,
    ),
  }));
}

/**
 * Read diagnostics once, on explicit operator request only (show or
 * refresh). The parent helper only uses the read-only snapshot query; no
 * intent is submitted. Failures resolve to the fixed note.
 *
 * @returns {Promise<void>}
 */
async function onReadDiagnostics() {
  if (diagnosticsPending.value) return;
  diagnosticsSeq += 1;
  const seq = diagnosticsSeq;
  diagnosticsOpen.value = true;
  diagnosticsPending.value = true;
  diagnosticsNote.value = '';
  /** @type {Array<{ key: string, label: string, value: string }> | null} */
  let rows = null;
  try {
    rows = diagnosticRows(await readDiagnostics(cafeFacade()));
  } catch {
    rows = null;
  }
  if (seq !== diagnosticsSeq) return;
  diagnosticsPending.value = false;
  if (rows === null) {
    diagnosticsRows.value = [];
    diagnosticsNote.value = DIAGNOSTICS_FAILED;
    return;
  }
  diagnosticsRows.value = rows;
}

function onCloseDiagnostics() {
  diagnosticsSeq += 1;
  diagnosticsOpen.value = false;
  diagnosticsPending.value = false;
  diagnosticsRows.value = [];
  diagnosticsNote.value = '';
}


function closeWindowBrowser() {
  browserSequence += 1; browserOpen.value = false; browserPending.value = false;
  browserSelected.value = ''; browserCapsule.value = '';
}
function publishCatalogue() {
  const state = catalogue.getState(); browserRows.value = state.entries;
  browserTotal.value = state.total; browserStatus.value = state.status;
  return state;
}
/** @param {number} offset @param {number} sequence @returns {Promise<any>} */
async function queryWindowPage(offset, sequence) {
  if (!controls || !browserOpen.value || sequence !== browserSequence) throw new Error('cancelled');
  return observeWindowPage(controls, offset, () => browserOpen.value && sequence === browserSequence);
}
async function refreshBrowser() {
  if (!controls || browserPending.value || view.value.availability !== 'available') return;
  const sequence = ++browserSequence; catalogue.reset(); publishCatalogue();
  browserSelected.value = ''; browserPending.value = true; browserStatus.value = 'loading';
  try {
    let offset = 0;
    for (let page = 0; page < 256; page += 1) {
      const value = await queryWindowPage(offset, sequence);
      if (sequence !== browserSequence) return;
      catalogue.accept(value); const state = publishCatalogue();
      if (state.complete || state.status !== 'loading' || state.nextOffset === null) break;
      offset = state.nextOffset;
    }
  } catch { if (sequence === browserSequence) { catalogue.reset(); browserRows.value = []; browserTotal.value = null; browserSelected.value = ''; browserStatus.value = 'failed'; } }
  finally { if (sequence === browserSequence) browserPending.value = false; }
}
function openWindowBrowser(id = '', role = 'game') {
  if (!controls || availReason.value !== '') return;
  if (id !== '') {
    if (capsuleById(id) === undefined) return;
    browserCapsule.value = id;
  } else {
    if (capsules.value.length >= 12) {
      notice.value = 'The wall limit is 12 capsules. Remove a capsule before adding another.';
      return;
    }
    browserCapsule.value = '';
  }
  browserRole.value = role;
  browserCrop.value = { x: 0, y: 0, w: 0, h: 0 };
  browserOpen.value = true;
  void refreshBrowser();
}
async function anchorBrowserWindow(token) {
  if (!controls || browserPending.value || isPending.value) return;
  if (anchoredWindows.value[token]) return;
  const matches = browserRows.value.filter(row => row.token === token);
  if (matches.length !== 1) return;
  const cached = matches[0];
  const sequence = browserSequence;
  browserSelected.value = token;
  browserPending.value = true;
  notice.value = '';
  try {
    let targetCapsuleId = browserCapsule.value;
    if (!targetCapsuleId) {
      if (capsules.value.length >= 12) {
        notice.value = 'The wall limit is 12 capsules. Remove a capsule before adding another.';
        return;
      }
      const beforeIds = new Set(capsules.value.map(cid));
      const label = nextWindowLabel();
      const createReply = await controls.createCapsule(label);
      if (sequence !== browserSequence) return;
      if (!createReply || createReply.queued !== true || typeof createReply.request_id !== 'string') {
        browserStatus.value = 'failed';
        notice.value = REQUEST_FAILED;
        return;
      }
      const createResult = await waitForAuthorityResult(controls, createReply.request_id, { signal: actionAbort.signal });
      if (sequence !== browserSequence) return;
      if (!['committed', 'observed', 'configured'].includes(createResult?.state)) {
        browserStatus.value = 'failed';
        notice.value = actionResultLabel(createResult);
        return;
      }
      // The authority result can arrive just before the snapshot that lists the capsule.
      const created = await waitForProjectedMatch(() => capsules.value.find(c => !beforeIds.has(cid(c))), () => sequence === browserSequence);
      if (!created) {
        browserStatus.value = 'failed';
        notice.value = REQUEST_FAILED;
        return;
      }
      targetCapsuleId = cid(created);
    }
    let currentWin = listedWindow(cached.token);
    if (!currentWin || currentWin.generation !== cached.generation || currentWin.layout_revision !== cached.layout_revision) {
      await queryWindowPage(cached.pageOffset, sequence);
      if (sequence !== browserSequence) return;
      currentWin = listedWindow(cached.token);
    }
    const live = capsuleById(targetCapsuleId);
    if (!live || !currentWin || currentWin.generation !== cached.generation || currentWin.layout_revision !== cached.layout_revision) {
      browserStatus.value = 'selection_stale';
      browserSelected.value = '';
      return;
    }
    const customCrop = toCount(browserCrop.value.w) > 0 && toCount(browserCrop.value.h) > 0;
    const crop = customCrop
      ? {
          x: toCount(browserCrop.value.x),
          y: toCount(browserCrop.value.y),
          width: toCount(browserCrop.value.w),
          height: toCount(browserCrop.value.h),
        }
      : {
          x: 0,
          y: 0,
          width: toCount(currentWin.rect.width),
          height: toCount(currentWin.rect.height),
        };
    const pairReply = await controls.pairWindow({
      capsuleId: targetCapsuleId,
      role: browserRole.value,
      windowToken: currentWin.token,
      windowGeneration: currentWin.generation,
      layoutRevision: currentWin.layout_revision,
      crop,
    });
    if (sequence !== browserSequence) return;
    if (!pairReply || pairReply.queued !== true || typeof pairReply.request_id !== 'string') {
      browserStatus.value = 'failed';
      notice.value = REQUEST_FAILED;
      return;
    }
    const pairResult = await waitForAuthorityResult(controls, pairReply.request_id, { signal: actionAbort.signal });
    if (sequence !== browserSequence) return;
    if (!['committed', 'observed', 'configured'].includes(pairResult?.state)) {
      browserStatus.value = 'failed';
      notice.value = actionResultLabel(pairResult);
      return;
    }
    anchoredByCapsuleRole.value = {
      ...anchoredByCapsuleRole.value,
      [`${targetCapsuleId}:${browserRole.value}`]: currentWin.token,
    };
    closeWindowBrowser();
  } catch {
    if (sequence === browserSequence) {
      browserStatus.value = 'selection_stale';
      notice.value = REQUEST_FAILED;
    }
  } finally {
    if (sequence === browserSequence) browserPending.value = false;
  }
}
function resetBrowserCrop() {
  browserCrop.value = { x: 0, y: 0, w: 0, h: 0 };
}

onMounted(() => {
  applyTheme(theme.value);
  const facade = cafeFacade();
  if (facade === undefined) {
    hasFirstSnapshot.value = true;
    applyView(INITIAL_VIEW);
    return;
  }
  try {
    controls = createControls(facade);
  } catch {
    controls = null;
    hasFirstSnapshot.value = true;
    applyView(INITIAL_VIEW);
    return;
  }
  try {
    applyView(controls.getState());
  } catch {
    applyView(INITIAL_VIEW);
  }
  try {
    let subscribing = true;
    const off = controls.subscribe(snap => {
      if (!subscribing) hasFirstSnapshot.value = true;
      applyView(snap);
    });
    subscribing = false;
    if (typeof off === 'function') stopControls = off;
  } catch {
    hasFirstSnapshot.value = true;
    applyView(INITIAL_VIEW);
  }
  const connection = controls.connect();
  connection.then(
    () => { hasFirstSnapshot.value = true; },
    () => { hasFirstSnapshot.value = true; },
  );
  request(connection);
});

onBeforeUnmount(() => {
  appDisposed = true; actionAbort.abort();
  recordingTimer.dispose();
  closeWindowBrowser();
  diagnosticsSeq += 1;
  if (stopControls !== null) {
    try {
      stopControls();
    } catch {
      /* Listener cleanup must never surface renderer errors. */
    }
    stopControls = null;
  }
  if (controls !== null) {
    try {
      controls.dispose();
    } catch {
      /* Dispose removes renderer listeners only; authority is untouched. */
    }
    controls = null;
  }
});
</script>
<template>
  <div class="cafe-app">
    <TopBar product="Café">
      <template #nav><span aria-hidden="true"></span></template>
      <template #search><span aria-hidden="true"></span></template>
      <template #actions><span aria-hidden="true"></span></template>
      <template #primary><span aria-hidden="true"></span></template>
    </TopBar>
    <nav class="cafe-desktop-toolbar" aria-label="Workspace">
      <button class="cafe-button" type="button" :disabled="availReason !== '' || capsules.length >= 12" @click="openWindowBrowser()">Add window</button>
      <button class="cafe-button cafe-button-secondary" type="button" :disabled="availReason !== '' || capsules.length === 0 || batchPending" @click="onBatch('start')">Record all</button>
      <button class="cafe-button cafe-button-secondary" type="button" :disabled="availReason !== '' || runningCount === 0 || batchPending" @click="onBatch('stop')">Stop all</button>
      <button class="cafe-button cafe-button-secondary" type="button" :aria-pressed="previewsEnabled" @click="previewsEnabled = !previewsEnabled">{{ previewsEnabled ? 'Pause previews' : 'Show previews' }}</button>
      <template v-if="!folderConfigured">
        <span class="cafe-toolbar-warning" role="status">No output folder set</span>
        <button class="cafe-button cafe-button-secondary" type="button" :disabled="availReason !== '' || isPending" @click="onChooseFolder">Choose folder</button>
      </template>
      <span class="cafe-toolbar-space"></span>
      <button class="cafe-button cafe-button-secondary" type="button" @click="showPage(activePage === 'capsules' ? 'settings' : 'capsules')">{{ activePage === 'capsules' ? 'Settings' : 'Back to wall' }}</button>
      <button class="cafe-button cafe-button-secondary" type="button" @click="helpMenuOpen = !helpMenuOpen">Help</button>
      <div v-if="helpMenuOpen" class="cafe-desktop-help" role="menu">
        <button class="cafe-button" type="button" @click="showPage('diagnostics'); helpMenuOpen = false">Diagnostics</button>
        <button class="cafe-button" type="button" @click="showPage('settings'); helpMenuOpen = false">Settings</button>
      </div>
    </nav>
    <main class="cafe-main cafe-desktop-main">
      <OrganizerPlan v-if="organizerPlan" :plan="organizerPlan" :busy="organizerPending" :result="organizerResult" @confirm="onOrganizer($event, true)" @reject="onOrganizer($event, false)" />
      <section v-if="activePage === 'capsules'" class="cafe-desktop-workspace" aria-label="Capsule wall">
        <p v-if="!isStarting && availReason !== ''" class="cafe-warning" role="status">{{ availReason }} · Check that Café is connected before recording.</p>
        <p v-if="notice !== ''" class="cafe-error" role="alert">{{ notice }}</p>
        <p v-if="!isStarting && capsules.length > 0" class="cafe-wall-guidance">Keep each pop-out open and not fully covered: Discord stops painting covered windows.</p>
        <p v-if="batchNote" class="cafe-batch-result" role="status">{{ batchNote }}</p>
        <div :inert="browserOpen ? true : undefined">
        <CapsuleGrid :capsuleIDs="rawCapsuleIds" :available="gridAvailable" :starting="isStarting" :tabLabels="capsuleTabLabels" @add-capsule="openWindowBrowser()">
          <template #capsule="{ id, visible }">
            <article v-if="capsuleById(id) !== undefined" class="cafe-capsule cafe-desktop-capsule" :data-tile-state="headerStateFor(capsuleById(id))">
              <CapsuleTopbar :slotLabel="slotLabelFor(id)" :capsuleLabel="cname(capsuleById(id))" :state="headerStateFor(capsuleById(id))" :elapsed="elapsedFor(id)" :busy="tilePending[id] === true" @remove="onRemoveCapsule(id)" />
              <div class="cafe-desktop-media" :data-feed-count="capsuleFeeds(capsuleById(id)).length">
                <CapsulePreview v-for="feed in capsuleFeeds(capsuleById(id))" :key="feed.feed_id" :capsule-id="id" :feed="feed" :enabled="previewsEnabled" :visible="visible" @state="previewStateFor(id, feed.feed_id, $event)" />
                <div v-if="capsuleFeeds(capsuleById(id)).length === 0" class="cafe-preview-placeholder"><strong>No preview</strong><span>Choose a window for this capsule.</span></div>
              </div>
              <CapsuleControl :recordReason="recordReasonFor(capsuleById(id))" :stopReason="stopReasonFor(capsuleById(id))" :obsReason="obsReasonFor(capsuleById(id))" :busy="tilePending[id] === true" :state="headerStateFor(capsuleById(id))" :resolve-label="resolutionLabel(capsuleById(id))" :failure-reason="failureReasonFor(capsuleById(id))" :result="tileResults[id] || ''" @record="onCardRecord(id)" @stop="onCardStop(id)" @obs="onCardObs(id)" @resolve="resolveRecordingBlock(id)" @remove="onRemoveCapsule(id)" />
            </article>
          </template>
        </CapsuleGrid>
        </div>
      </section>
      <WindowBrowser v-if="browserOpen" :notice="notice" :windows="browserRows" :currentPage="{ offset: 0, nextOffset: null }" :total="browserTotal" :status="browserStatus" :busy="browserPending" :selectedId="browserSelected" :anchored="anchoredWindows" @select="anchorBrowserWindow" @refresh="refreshBrowser" @close="closeWindowBrowser">
        <template #advanced>
          <details class="cafe-desktop-crop">
            <summary>Advanced crop</summary>
            <p>Full window by default. Adjust these values only to exclude borders or a region of the window.</p>
            <button class="cafe-button" type="button" :disabled="browserPending" @click="resetBrowserCrop">Full window</button>
            <div class="cafe-crop">
              <label v-for="field in ['x', 'y', 'w', 'h']" :key="field">{{ { x: 'Left', y: 'Top', w: 'Width', h: 'Height' }[field] }}<input class="cafe-input" type="number" v-model.number="browserCrop[field]" :disabled="browserPending" /></label>
            </div>
          </details>
        </template>
      </WindowBrowser>
      <div v-if="activePage === 'diagnostics' || activePage === 'settings'" class="cafe-desktop-diagnostics">
        <button class="cafe-button" type="button" @click="showPage('capsules')">Back to wall</button>
      <section class="cafe-panel" aria-labelledby="cafe-setup-title">
        <h1 id="cafe-setup-title" class="cafe-title">Settings</h1>
        <label v-if="autoAnchor !== null" class="cafe-auto-anchor"><input type="checkbox" :checked="autoAnchor === true" :disabled="availReason !== '' || isPending" @change="onAutoAnchor" /> Auto-anchor Discord pop-outs</label>
        <h2 class="cafe-heading">Folder</h2>
        <p class="cafe-text">Output folder: {{ folderConfigured ? 'Configured' : 'Not chosen' }}</p>
        <div class="cafe-controls">
          <button
            class="cafe-button"
            type="button"
            :disabled="availReason !== ''"
            @click="onChooseFolder"
          >
            Choose folder
          </button>
          <p v-if="availReason !== ''" class="cafe-reason">{{ availReason }}</p>
        </div>
        <h2 class="cafe-heading">FFmpeg</h2>
        <p class="cafe-text">FFmpeg: {{ ffmpegConfigured ? 'Configured' : 'Not chosen' }}</p>
        <div class="cafe-controls">
          <button
            class="cafe-button"
            type="button"
            :disabled="availReason !== ''"
            @click="onChooseFfmpeg"
          >
            Choose FFmpeg
          </button>
          <p v-if="availReason !== ''" class="cafe-reason">{{ availReason }}</p>
        </div>
        <h2 id="cafe-obs-title" class="cafe-heading">OBS</h2>
        <p class="cafe-text">OBS: {{ obsReady ? 'Ready' : 'Not ready' }}</p>
        <div class="cafe-field">
          <label class="cafe-label" for="cafe-obs-port">Port</label>
          <input
            id="cafe-obs-port"
            v-model.number="obsPort"
            class="cafe-input"
            type="number"
            autocomplete="off"
            :disabled="obsReason !== ''"
          />
        </div>
        <div class="cafe-field">
          <label class="cafe-label" for="cafe-obs-password">Password</label>
          <input
            id="cafe-obs-password"
            v-model="password"
            class="cafe-input"
            type="password"
            autocomplete="off"
            :disabled="obsReason !== ''"
          />
        </div>
        <div class="cafe-controls">
          <button
            class="cafe-button"
            type="button"
            :disabled="obsReason !== ''"
            @click="onConfigureObs"
          >
            Configure OBS
          </button>
          <p v-if="obsReason !== ''" class="cafe-reason">{{ obsReason }}</p>
        </div>
        <p v-if="capsules.length === 0" class="cafe-text">No capsules to send.</p>
        <ul v-else class="cafe-list">
          <li v-for="(capsule, index) in capsules" :key="'obs-' + ckey(capsule, index)" class="cafe-row">
            <span class="cafe-row-main">{{ cname(capsule) }}</span>
            <button
              class="cafe-button"
              type="button"
              :disabled="sendBlock(capsule) !== ''"
              @click="onSendToObs(capsule)"
            >
              Send to OBS
            </button>
            <p v-if="sendBlock(capsule) !== ''" class="cafe-reason">{{ sendBlock(capsule) }}</p>
          </li>
        </ul>
        <h2 id="cafe-chat-title" class="cafe-heading">Twitch</h2>
        <p class="cafe-text">Chat: {{ chatReady ? 'Ready' : 'Pending' }}</p>
        <div class="cafe-field">
          <label class="cafe-label" for="cafe-chat-channel">Channel</label>
          <input
            id="cafe-chat-channel"
            v-model="channel"
            class="cafe-input"
            type="text"
            autocomplete="off"
            :disabled="chatReason !== ''"
          />
        </div>
        <div class="cafe-controls">
          <button
            class="cafe-button"
            type="button"
            :disabled="chatReason !== ''"
            @click="onConfigureTwitch"
          >
            Configure chat
          </button>
          <p v-if="chatReason !== ''" class="cafe-reason">{{ chatReason }}</p>
        </div>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-capsules-title">
        <h2 id="cafe-capsules-title" class="cafe-heading">Capsules</h2>
        <p class="cafe-text">One capsule per person. Names only; the runtime owns every id.</p>
        <div class="cafe-controls">
          <label class="cafe-label" for="cafe-person-input">Person name</label>
          <input
            id="cafe-person-input"
            v-model="personLabel"
            class="cafe-input"
            type="text"
            autocomplete="off"
            :disabled="availReason !== ''"
          />
          <button class="cafe-button" type="button" :disabled="availReason !== '' || capsules.length >= 12" @click="onCreateCapsule">
            New capsule
          </button>
          <p v-if="availReason !== ''" class="cafe-reason">{{ availReason }}</p>
        </div>
        <p v-if="capsules.length === 0" class="cafe-text">No capsules yet.</p>
        <article
          v-for="(capsule, index) in capsules"
          :key="ckey(capsule, index)"
          class="cafe-capsule"
          :aria-labelledby="'cafe-capsule-' + index"
        >
          <h3 :id="'cafe-capsule-' + index" class="cafe-subheading">{{ cname(capsule) }}</h3>
          <p v-if="capsuleFeeds(capsule).length === 0" class="cafe-text">No paired feeds.</p>
          <ul v-else class="cafe-list">
            <li v-for="(feed, fi) in capsuleFeeds(capsule)" :key="'feed-' + fi" class="cafe-row">
              <span class="cafe-row-main">{{ feedLine(feed) }}</span>
            </li>
          </ul>
          <div class="cafe-pair-grid">
            <div v-for="role in pairRoles" :key="role" class="cafe-pair">
              <h4 class="cafe-slot">{{ slotTitle(role) }}</h4>
              <p v-if="feedFor(capsule, role) === undefined" class="cafe-text">Not paired.</p>
              <p v-else class="cafe-text">Paired: {{ feedLine(feedFor(capsule, role)) }}</p>
              <div class="cafe-controls">
                <button
                  class="cafe-button"
                  type="button"
                  :disabled="availReason !== '' || feedFor(capsule, role) !== undefined"
                  @click="openWindowBrowser(cid(capsule), role)"
                >
                  Pair {{ role }}
                </button>
              </div>
            </div>
          </div>
          <p v-if="recFor(capsule) === undefined" class="cafe-text">Not recording.</p>
          <div v-else>
            <p class="cafe-text">Recording state: {{ textOf(recFor(capsule).state) }}</p>
            <ul v-if="recFeeds(recFor(capsule)).length > 0" class="cafe-list">
              <li v-for="(rfeed, ri) in recFeeds(recFor(capsule))" :key="'rec-' + ri" class="cafe-row">
                <span class="cafe-row-main">{{ recFeedLine(rfeed) }}</span>
              </li>
            </ul>
          </div>
          <div class="cafe-controls">
            <button
              class="cafe-button"
              type="button"
              :disabled="startBlock(capsule) !== ''"
              @click="onStart(capsule)"
            >
              Start
            </button>
            <button
              class="cafe-button"
              type="button"
              :disabled="stopBlock(capsule) !== ''"
              @click="onStop(capsule)"
            >
              Stop
            </button>
            <p v-if="startBlock(capsule) !== ''" class="cafe-reason">{{ startBlock(capsule) }}</p>
            <p v-if="stopBlock(capsule) !== ''" class="cafe-reason">{{ stopBlock(capsule) }}</p>
          </div>
          <p class="cafe-text">Start checks hardware; real hardware may still block.</p>
        </article>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-windows-title">
        <h2 id="cafe-windows-title" class="cafe-heading">Windows</h2>
        <p class="cafe-text">Listed windows only.</p>
        <div class="cafe-controls">
          <button
            class="cafe-button"
            type="button"
            :disabled="availReason !== ''"
            @click="onRefreshWindows"
          >
            Refresh
          </button>
          <button
            class="cafe-button"
            type="button"
            :disabled="prevReason !== ''"
            @click="onPrevWindows"
          >
            Previous
          </button>
          <button
            class="cafe-button"
            type="button"
            :disabled="nextReason !== ''"
            @click="onNextWindows"
          >
            Next
          </button>
          <p v-if="availReason !== ''" class="cafe-reason">{{ availReason }}</p>
        </div>
        <p class="cafe-text">{{ windows.length }} shown · total {{ textOf(pageTotal) }}</p>
        <p v-if="windows.length === 0" class="cafe-text">No windows listed.</p>
        <ul v-else class="cafe-list">
          <li v-for="(entry, wi) in windows" :key="wkey(entry, wi)" class="cafe-row">
            <span class="cafe-row-main">{{ windowTitle(entry) }}</span>
            <span class="cafe-row-sub">{{ windowMeta(entry) }}</span>
          </li>
        </ul>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-status-title">
        <h2 id="cafe-status-title" class="cafe-heading">Diagnostics</h2>
        <p class="cafe-status" role="status">Bridge status: {{ statusLabel }}</p>
        <p v-if="platformWarning !== ''" class="cafe-warning" role="status">{{ platformWarning }}</p>
        <p v-if="isPending" class="cafe-status" role="status">Request pending…</p>
        <p v-if="notice !== ''" class="cafe-error" role="alert">{{ notice }}</p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" @click="toggleTheme">
            Switch to {{ themeLabel }} theme
          </button>
          <button
            v-if="!diagnosticsOpen"
            class="cafe-button"
            type="button"
            :disabled="diagnosticsPending"
            @click="onReadDiagnostics"
          >
            Show diagnostics
          </button>
          <template v-else>
            <button
              class="cafe-button"
              type="button"
              :disabled="diagnosticsPending"
              @click="onReadDiagnostics"
            >
              Refresh diagnostics
            </button>
            <button class="cafe-button" type="button" @click="onCloseDiagnostics">
              Close diagnostics
            </button>
          </template>
        </div>
        <div
          v-if="diagnosticsOpen"
          id="cafe-diagnostics-panel"
          role="region"
          aria-labelledby="cafe-diagnostics-title"
        >
          <h3 id="cafe-diagnostics-title" class="cafe-subheading">Diagnostics report</h3>
          <p class="cafe-text">Read-only: fixed codes and counts, without titles, accounts, passwords or paths.</p>
          <p v-if="diagnosticsPending" class="cafe-status" role="status">Reading diagnostics…</p>
          <p v-if="diagnosticsNote !== ''" class="cafe-error" role="alert">{{ diagnosticsNote }}</p>
          <ul v-if="diagnosticsRows.length > 0" class="cafe-list">
            <li v-for="row in diagnosticsRows" :key="row.key" class="cafe-row">
              <span class="cafe-row-main">{{ row.label }}</span>
              <span class="cafe-row-sub">{{ row.key }}: {{ row.value }}</span>
            </li>
          </ul>
        </div>
      </section>
      </div>
    </main>
    <StatusBar :shortcuts="[]" :crumbs="crumbs" :telemetry="telemetry" />
  </div>
</template>
<style scoped>
.cafe-controls { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
.cafe-capsule:not(.cafe-desktop-capsule) { display: grid; gap: var(--space-2); }
@media (max-width: 30rem) { .cafe-controls { gap: var(--space-2); } }
</style>
