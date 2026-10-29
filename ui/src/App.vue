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
 * @module ui/App
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import TopBar from './kit/shell/TopBar.vue';
import StatusBar from './kit/shell/StatusBar.vue';
import { createControls } from '../../src/renderer/control.js';

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
    return;
  }
  view.value = INITIAL_VIEW;
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

function onCreateCapsule() {
  if (controls === null) return;
  request(controls.createCapsule(personLabel.value));
}

function onRefreshWindows() {
  if (controls === null) return;
  prevOffsets.value = [];
  request(controls.refreshWindows(0));
}

function onPrevWindows() {
  if (controls === null) return;
  const offset = prevOffsets.value.pop();
  if (typeof offset !== 'number') return;
  request(controls.refreshWindows(offset));
}

function onNextWindows() {
  if (controls === null) return;
  const page = currentPage();
  if (typeof page.nextOffset !== 'number') return;
  prevOffsets.value.push(page.offset);
  request(controls.refreshWindows(page.nextOffset));
}

function onChooseFolder() {
  if (controls === null) return;
  request(controls.chooseFolder());
}

function onChooseFfmpeg() {
  if (controls === null) return;
  request(controls.chooseFfmpeg());
}

/**
 * @param {unknown} capsule
 * @param {number} index
 * @param {string} role
 */
function onPair(capsule, index, role) {
  if (controls === null) return;
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
  if (controls === null) return;
  const id = cid(capsule);
  if (id === '') return;
  request(controls.startRecording(id));
}

/**
 * @param {unknown} capsule
 */
function onStop(capsule) {
  if (controls === null) return;
  const id = cid(capsule);
  if (id === '') return;
  request(controls.stopRecording(id));
}

/**
 * @param {unknown} capsule
 */
function onSendToObs(capsule) {
  if (controls === null) return;
  const id = cid(capsule);
  if (id === '') return;
  request(controls.sendToObs(id));
}

function onConfigureObs() {
  if (controls === null) return;
  request(controls.configureObs(toCount(obsPort.value), password.value));
  password.value = '';
}

function onConfigureTwitch() {
  if (controls === null) return;
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

onMounted(() => {
  applyTheme(theme.value);
  const facade = cafeFacade();
  if (facade === undefined) {
    applyView(INITIAL_VIEW);
    return;
  }
  try {
    controls = createControls(facade);
  } catch {
    controls = null;
    applyView(INITIAL_VIEW);
    return;
  }
  try {
    applyView(controls.getState());
  } catch {
    applyView(INITIAL_VIEW);
  }
  try {
    const off = controls.subscribe(applyView);
    if (typeof off === 'function') stopControls = off;
  } catch {
    applyView(INITIAL_VIEW);
  }
  request(controls.connect());
});

onBeforeUnmount(() => {
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
    <main class="cafe-main">
      <section class="cafe-panel" aria-labelledby="cafe-status-title">
        <h1 id="cafe-status-title" class="cafe-title">Café workspace</h1>
        <p class="cafe-status" role="status">Bridge status: {{ statusLabel }}</p>
        <p v-if="platformWarning !== ''" class="cafe-warning" role="status">{{ platformWarning }}</p>
        <p v-if="isPending" class="cafe-status" role="status">Request pending…</p>
        <p v-if="notice !== ''" class="cafe-error" role="alert">{{ notice }}</p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" @click="toggleTheme">
            Switch to {{ themeLabel }} theme
          </button>
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
          <button class="cafe-button" type="button" :disabled="availReason !== ''" @click="onCreateCapsule">
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
              <div class="cafe-field">
                <label class="cafe-label" :for="pairInputId(index, role, 'window')">Window</label>
                <select
                  :id="pairInputId(index, role, 'window')"
                  v-model="pairForm(ckey(capsule, index), role).token"
                  class="cafe-select"
                  :disabled="availReason !== ''"
                  @change="suggestCrop(ckey(capsule, index), role)"
                >
                  <option value="">Select a window</option>
                  <option
                    v-for="(entry, wi) in windows"
                    :key="wkey(entry, wi)"
                    :value="wtoken(entry, wi)"
                  >
                    {{ windowLabel(entry) }}
                  </option>
                </select>
              </div>
              <div class="cafe-crop">
                <div class="cafe-field">
                  <label class="cafe-label" :for="pairInputId(index, role, 'x')">Crop x</label>
                  <input
                    :id="pairInputId(index, role, 'x')"
                    v-model.number="pairForm(ckey(capsule, index), role).x"
                    class="cafe-input"
                    type="number"
                    :disabled="availReason !== ''"
                  />
                </div>
                <div class="cafe-field">
                  <label class="cafe-label" :for="pairInputId(index, role, 'y')">Crop y</label>
                  <input
                    :id="pairInputId(index, role, 'y')"
                    v-model.number="pairForm(ckey(capsule, index), role).y"
                    class="cafe-input"
                    type="number"
                    :disabled="availReason !== ''"
                  />
                </div>
                <div class="cafe-field">
                  <label class="cafe-label" :for="pairInputId(index, role, 'w')">Crop width</label>
                  <input
                    :id="pairInputId(index, role, 'w')"
                    v-model.number="pairForm(ckey(capsule, index), role).w"
                    class="cafe-input"
                    type="number"
                    :disabled="availReason !== ''"
                  />
                </div>
                <div class="cafe-field">
                  <label class="cafe-label" :for="pairInputId(index, role, 'h')">Crop height</label>
                  <input
                    :id="pairInputId(index, role, 'h')"
                    v-model.number="pairForm(ckey(capsule, index), role).h"
                    class="cafe-input"
                    type="number"
                    :disabled="availReason !== ''"
                  />
                </div>
              </div>
              <div class="cafe-controls">
                <button
                  class="cafe-button"
                  type="button"
                  :disabled="pairBlock(capsule, index, role) !== ''"
                  @click="onPair(capsule, index, role)"
                >
                  Pair {{ role }}
                </button>
                <p v-if="pairBlock(capsule, index, role) !== ''" class="cafe-reason">
                  {{ pairBlock(capsule, index, role) }}
                </p>
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
        <p class="cafe-text">Listed windows only. Tokens stay opaque.</p>
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
            <span class="cafe-token">{{ wtokenText(entry) }}</span>
          </li>
        </ul>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-setup-title">
        <h2 id="cafe-setup-title" class="cafe-heading">Recording setup</h2>
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
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-obs-title">
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
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-chat-title">
        <h2 id="cafe-chat-title" class="cafe-heading">Chat</h2>
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
    </main>
    <StatusBar :shortcuts="[]" :crumbs="crumbs" :telemetry="telemetry" />
  </div>
</template>
