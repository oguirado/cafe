<script setup>
// @ts-check
/**
 * Cafe workspace shell (presentation-only).
 *
 * Composes the unchanged kit TopBar and StatusBar with truthful disabled
 * product control regions. No intent is submitted from this unit: every
 * product effect control stays disabled with a fixed visible reason, even
 * when the bridge reports availability. Theme is a local presentation
 * toggle only (dark default, light toggle).
 *
 * @module ui/App
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import TopBar from './kit/shell/TopBar.vue';
import StatusBar from './kit/shell/StatusBar.vue';

/**
 * @typedef {object} CafeBridgeFacade
 * @property {() => Promise<unknown>} [getSnapshot] Resolves the frozen snapshot clone.
 * @property {(listener: (snapshot: unknown) => void) => unknown} [onSnapshot] Subscribes; returns an unsubscribe cleanup.
 */

/** Fixed visible reason for every disabled product control. */
const UNAVAILABLE_REASON = 'Runtime unavailable';

/** @type {import('vue').Ref<'starting' | 'available' | 'unavailable'>} */
const availability = ref('unavailable');
/** @type {import('vue').Ref<'dark' | 'light'>} */
const theme = ref('dark');
/** @type {(() => void) | null} */
let stopSnapshot = null;

/** @type {Array<{ label: string, tone?: 'dim' | 'strong' | 'accent' }>} */
const crumbs = [
  { label: 'Café', tone: 'dim' },
  { label: 'Workspace' },
];

const statusLabel = computed(() =>
  availability.value === 'available'
    ? 'Available'
    : availability.value === 'starting'
      ? 'Starting'
      : 'Unavailable',
);
const themeLabel = computed(() => (theme.value === 'light' ? 'dark' : 'light'));

/**
 * Structural read of the frozen `window.cafe` facade without extending it.
 *
 * @returns {CafeBridgeFacade | undefined}
 */
function cafeBridge() {
  if (typeof window === 'undefined') return undefined;
  const scope = /** @type {{ cafe?: unknown }} */ (/** @type {unknown} */ (window));
  if (scope.cafe === null || typeof scope.cafe !== 'object') return undefined;
  return /** @type {CafeBridgeFacade} */ (scope.cafe);
}

/**
 * Map a snapshot to the fixed availability label. Unknown shapes, unknown
 * availability values and null (valid unavailable state) all render
 * unavailable. Epochs, revisions, reasons and byte counts are never read.
 *
 * @param {unknown} snapshot
 */
function applySnapshot(snapshot) {
  if (snapshot !== null && typeof snapshot === 'object') {
    const value = /** @type {{ availability?: unknown }} */ (snapshot).availability;
    if (value === 'available' || value === 'starting' || value === 'unavailable') {
      availability.value = value;
      return;
    }
  }
  availability.value = 'unavailable';
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
  const bridge = cafeBridge();
  if (!bridge || typeof bridge.getSnapshot !== 'function') {
    availability.value = 'unavailable';
    return;
  }
  bridge.getSnapshot().then(applySnapshot, () => {
    availability.value = 'unavailable';
  });
  if (typeof bridge.onSnapshot === 'function') {
    try {
      const off = bridge.onSnapshot(applySnapshot);
      if (typeof off === 'function') stopSnapshot = () => { off(); };
    } catch {
      availability.value = 'unavailable';
    }
  }
});

onBeforeUnmount(() => {
  if (stopSnapshot !== null) {
    try {
      stopSnapshot();
    } catch {
      /* Listener cleanup must never surface renderer errors. */
    }
    stopSnapshot = null;
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
        <p class="cafe-text">
          Presentation workspace. Product controls stay disabled until the runtime is available.
        </p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" @click="toggleTheme">
            Switch to {{ themeLabel }} theme
          </button>
        </div>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-capsules-title">
        <h2 id="cafe-capsules-title" class="cafe-heading">Capsules</h2>
        <p class="cafe-text">Saved work units appear here when the runtime is connected.</p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" disabled>New capsule</button>
          <p class="cafe-reason">{{ UNAVAILABLE_REASON }}</p>
        </div>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-chat-title">
        <h2 id="cafe-chat-title" class="cafe-heading">Chat</h2>
        <p class="cafe-text">Conversation is unavailable until the runtime is connected.</p>
        <div class="cafe-controls">
          <label class="cafe-label" for="cafe-chat-input">Message</label>
          <input
            id="cafe-chat-input"
            class="cafe-input"
            type="text"
            placeholder="Unavailable"
            disabled
          />
          <button class="cafe-button" type="button" disabled>Send</button>
          <p class="cafe-reason">{{ UNAVAILABLE_REASON }}</p>
        </div>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-recording-title">
        <h2 id="cafe-recording-title" class="cafe-heading">Recording</h2>
        <p class="cafe-text">Capture controls are unavailable until the runtime is connected.</p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" disabled>Start recording</button>
          <p class="cafe-reason">{{ UNAVAILABLE_REASON }}</p>
        </div>
      </section>
      <section class="cafe-panel" aria-labelledby="cafe-outputs-title">
        <h2 id="cafe-outputs-title" class="cafe-heading">Outputs</h2>
        <p class="cafe-text">External outputs stay disconnected until the runtime is connected.</p>
        <div class="cafe-controls">
          <button class="cafe-button" type="button" disabled>Connect output</button>
          <p class="cafe-reason">{{ UNAVAILABLE_REASON }}</p>
        </div>
      </section>
    </main>
    <StatusBar :shortcuts="[]" :crumbs="crumbs" />
  </div>
</template>
