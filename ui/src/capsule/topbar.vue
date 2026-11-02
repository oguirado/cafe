<script setup>
// @ts-check
/**
 * Cafe capsule header (presentation-only).
 *
 * Visual grammar copied from MetaViewer CapsulePanel canonical
 * title/status [CITED; METAVIEWER_READONLY/docs/UI_ARCHITECTURE.md:100],
 * adapted to EXISTING Cafe CSS variables only. System fonts only.
 * Slot identity comes only from explicit `slotLabel` string prop;
 * never assigned by array index. Never accepts or renders window token.
 * State text renders only known backend values; else explicit unknown.
 *
 * @module ui/capsule/header
 */
import { computed } from 'vue';

/**
 * Known backend lifecycle truth, never extended locally.
 * [CITED; METAVIEWER_READONLY/ui/src/wall/SurfacePanel.vue:24]
 */
const KNOWN_STATES = Object.freeze(['active', 'standby', 'ceded', 'destroyed', 'lost', 'live', 'connecting', 'window_minimized', 'window_closed', 'no_preview', 'recording', 'recording_failed']);
/** Explicit fallback; never invents a backend value. */
const UNKNOWN_LABEL = 'unknown';

const props = defineProps({
  slotLabel: { type: String, required: true },
  capsuleLabel: { type: String, required: true },
  state: { type: String, required: true },
  elapsed: { type: String, default: '—' },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(['remove']);

/** @returns {string} */
const slotText = computed(() => {
  return (typeof props.slotLabel === 'string' && props.slotLabel.trim().length > 0)
    ? props.slotLabel
    : UNKNOWN_LABEL;
});

/** @returns {string} */
const capsuleText = computed(() => {
  return (typeof props.capsuleLabel === 'string' && props.capsuleLabel.trim().length > 0)
    ? props.capsuleLabel
    : UNKNOWN_LABEL;
});

/** @returns {boolean} */
const isKnownState = computed(() => {
  return typeof props.state === 'string' && KNOWN_STATES.includes(props.state);
});

/**
 * Display only exact known backend value, else explicit unknown.
 * @returns {string}
 */
const displayState = computed(() => {
  return isKnownState.value ? ({live:'Live preview',connecting:'Connecting',window_minimized:'Window minimized',window_closed:'Window closed',no_preview:'No preview',recording:'Recording',recording_failed:'Recording failed'}[props.state] || props.state) : UNKNOWN_LABEL;
});

/** Stable key for styling; unknown collapses to one explicit key. @returns {string} */
const stateKey = computed(() => {
  return isKnownState.value ? props.state : 'unknown';
});

const removeDisabled = computed(() => props.busy === true || stateKey.value === 'recording');

function onRemove() {
  if (removeDisabled.value) return;
  emit('remove');
}
</script>

<template>
  <header class="cafe-capsule-header" :data-state-key="stateKey">
    <span class="cafe-capsule-slot">{{ slotText }}</span>
    <h2 class="cafe-capsule-title">{{ capsuleText }}</h2>
    <p class="cafe-capsule-status" role="status">
      <span class="cafe-capsule-dot" aria-hidden="true"></span>
      <span class="cafe-capsule-state">{{ displayState }}</span>
    </p>
    <span class="cafe-capsule-elapsed" :aria-label="elapsed === '—' ? 'Recording timer inactive' : 'Recording display timer'">{{ elapsed }}</span>
    <button
      class="cafe-button cafe-capsule-remove"
      type="button"
      :disabled="removeDisabled"
      :title="stateKey === 'recording' ? 'Stop recording before removing' : 'Remove capsule'"
      @click="onRemove"
    >Remove</button>
  </header>
</template>

<style scoped>
.cafe-capsule-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 28px;
  min-height: 28px;
  max-height: 28px;
  flex: 0 0 28px;
  box-sizing: border-box;
  background: var(--surface);
  color: var(--fg);
  border: 0;
  border-radius: 0;
  padding: 0 var(--space-2);
  margin: 0;
  font-family: var(--font-body);
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
}
.cafe-capsule-slot {
  flex: 0 0 auto;
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1;
}
.cafe-capsule-title {
  flex: 1 1 0;
  min-width: 0;
  margin: 0;
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cafe-capsule-status {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  margin: 0;
  padding: 0 var(--space-2);
  height: 20px;
  box-sizing: border-box;
  background: var(--surface-warm);
  color: var(--muted);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  line-height: 1;
}
.cafe-capsule-dot {
  width: var(--space-2);
  height: var(--space-2);
  border-radius: var(--radius-pill);
  background: var(--muted);
}
.cafe-capsule-header[data-state-key="destroyed"] .cafe-capsule-status,
.cafe-capsule-header[data-state-key="lost"] .cafe-capsule-status {
  border-style: dashed;
}
.cafe-capsule-header[data-state-key="unknown"] .cafe-capsule-status {
  border-style: dotted;
}
.cafe-capsule-state {
  font-family: var(--font-mono);
}
.cafe-capsule-elapsed {
  flex: 0 0 auto;
  margin: 0;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-size: var(--text-xs);
  line-height: 1;
}
.cafe-capsule-remove {
  flex: 0 0 auto;
  height: 20px;
  padding: 0 var(--space-2);
  background: var(--surface-warm);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-body);
  font-size: var(--text-xs);
  line-height: 1;
  cursor: pointer;
}
.cafe-capsule-remove:hover:not(:disabled) {
  color: var(--fg);
  border-color: var(--danger);
}
.cafe-capsule-remove:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.cafe-capsule-header[data-state-key="recording"] .cafe-capsule-dot {
  background: var(--danger);
}
.cafe-capsule-header[data-state-key="recording"] .cafe-capsule-status {
  color: var(--fg);
  border-color: var(--danger);
}
</style>
