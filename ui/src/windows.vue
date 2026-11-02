<script setup>
// @ts-check
import { computed, ref } from 'vue';
const props = defineProps({
  notice: { type: String, default: '' },
  windows: { type: Array, required: true },
  currentPage: { type: Object, default: () => ({ offset: 0, nextOffset: null }) },
  total: { type: Number, default: null },
  status: { type: String, default: 'idle' },
  busy: { type: Boolean, default: false },
  selectedId: { type: String, default: '' },
  anchored: { type: Object, default: () => ({}) },
});
const emit = defineEmits(['select', 'refresh', 'close']);
const query = ref('');
const showAll = ref(false);

/**
 * @param {unknown} exe
 * @returns {string}
 */
function exeBase(exe) {
  if (typeof exe !== 'string') return '';
  const parts = exe.trim().split(/[\\/]/);
  return (parts[parts.length - 1] || '').toLocaleLowerCase();
}

/**
 * @param {any} entry
 * @returns {boolean}
 */
function isDiscordWindow(entry) {
  return exeBase(entry?.exe).includes('discord');
}

/**
 * @param {any} entry
 * @returns {boolean}
 */
function isHiddenNoiseWindow(entry) {
  const title = typeof entry?.title === 'string' ? entry.title.trim() : '';
  if (title === '') return true;
  const base = exeBase(entry?.exe);
  if (base === 'explorer.exe' || base === 'explorer') return true;
  if (base === 'cafe.exe') return true;
  if (base === 'textinputhost.exe' || base === 'textinputhost') return true;
  if (base.startsWith('powertoys')) return true;
  return false;
}

/**
 * @param {any} entry
 * @returns {string}
 */
function anchoredSlotFor(entry) {
  if (!entry || typeof entry.token !== 'string') return '';
  if (props.anchored && typeof props.anchored === 'object' && typeof props.anchored[entry.token] === 'string' && props.anchored[entry.token] !== '') {
    return props.anchored[entry.token];
  }
  return typeof entry.anchoredSlot === 'string' ? entry.anchoredSlot : '';
}

const admitted = computed(() => {
  if (!Array.isArray(props.windows)) return [];
  const seen = new Set();
  for (const entry of props.windows) {
    if (entry === null || typeof entry !== 'object' || typeof entry.token !== 'string'
      || entry.token.length === 0 || new TextEncoder().encode(entry.token).length > 128
      || entry.token.includes('\0') || seen.has(entry.token)) return [];
    seen.add(entry.token);
  }
  return props.windows;
});
const matches = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  const filtered = admitted.value.filter(entry => {
    if (!showAll.value && isHiddenNoiseWindow(entry)) return false;
    const exe = typeof entry.exe === 'string' ? entry.exe : '';
    const title = typeof entry.title === 'string' ? entry.title : '';
    return !term || [title, exe].some(value => value.toLocaleLowerCase().includes(term));
  });
  const discord = [];
  const rest = [];
  for (const entry of filtered) {
    if (isDiscordWindow(entry)) discord.push(entry);
    else rest.push(entry);
  }
  return [...discord, ...rest];
});
const blocked = computed(() => props.busy || props.status === 'unavailable' || props.status === 'failed');
const totalText = computed(() => typeof props.total === 'number' && Number.isSafeInteger(props.total) && props.total >= 0 ? String(props.total) : 'unconfirmed');
const statusText = computed(() => ({
  idle: 'Press Refresh to scan windows.',
  loading: 'Scanning windows…',
  ready: 'Windows updated.',
  incomplete: 'Partial list. Refresh to scan again.',
  unavailable: 'Café is not connected. Open the app before scanning windows.',
  failed: 'Could not complete the window scan. Refresh to try again.',
  selection_stale: 'The window changed or closed. Select it again.',
}[props.status] || 'Window scan status unconfirmed'));

/**
 * @param {any} entry
 */
function onSelectWindow(entry) {
  if (blocked.value || !entry || anchoredSlotFor(entry) !== '') return;
  emit('select', entry.token);
}
</script>
<template>
  <section id="cafe-window-browser" class="cafe-window-browser" role="dialog" aria-modal="true" aria-labelledby="cafe-window-browser-title">
    <header class="cafe-window-browser-header">
      <div><h2 id="cafe-window-browser-title">Add window</h2><p>Keep each pop-out open and not minimized. Click a window to anchor it.</p></div>
    <p v-if="notice !== ''" class="cafe-error" role="alert">{{ notice }}</p>
      <button type="button" class="cafe-button" @click="emit('close')">Close</button>
    </header>
    <div class="cafe-window-browser-tools">
      <input v-model="query" class="cafe-input" type="search" aria-label="Search windows" placeholder="Search by title or application" />
      <label><input v-model="showAll" type="checkbox" /> Show all windows</label>
      <button type="button" class="cafe-button" :disabled="busy" @click="emit('refresh')">Refresh</button>
    </div>
    <p class="cafe-text" role="status">{{ statusText }}</p>
    <p class="cafe-text">{{ matches.length }} matches · {{ admitted.length }} loaded · total {{ totalText }}</p>
    <ul class="cafe-window-browser-list">
      <li v-for="entry in matches" :key="entry.token">
        <button type="button" class="cafe-window-row" :class="{ selected: entry.token === selectedId }" :aria-pressed="entry.token === selectedId" :disabled="blocked || anchoredSlotFor(entry) !== ''" @click="onSelectWindow(entry)">
          <span>{{ entry.title || 'Untitled window' }}</span>
          <small>{{ entry.exe || 'Unconfirmed app' }}</small>
          <span v-if="anchoredSlotFor(entry) !== ''" class="cafe-window-anchored">Anchored to {{ anchoredSlotFor(entry) }}</span>
        </button>
      </li>
    </ul>
    <p v-if="status === 'ready' && matches.length === 0" class="cafe-text">No matching windows found.</p>
    <slot name="advanced"></slot>
  </section>
</template>
