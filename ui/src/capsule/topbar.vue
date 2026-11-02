<script setup>
// @ts-check
/**
 * Cafe capsule header (presentation-only).
 *
 * Visual grammar copied from MetaViewer CapsulePanel canonical
 * title/status [CITED; METAVIEWER_READONLY/docs/UI_ARCHITECTURE.md:100],
 * adapted to EXISTING Cafe CSS variables only. System fonts only.
 * No actions here; actions live in a separate component.
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
const KNOWN_STATES = Object.freeze(['active', 'standby', 'ceded', 'destroyed', 'lost']);
/** Explicit fallback; never invents a backend value. */
const UNKNOWN_LABEL = 'unknown';

const props = defineProps({
  slotLabel: { type: String, required: true },
  capsuleLabel: { type: String, required: true },
  state: { type: String, required: true }
});

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
  return isKnownState.value ? props.state : UNKNOWN_LABEL;
});

/** Stable key for styling; unknown collapses to one explicit key. @returns {string} */
const stateKey = computed(() => {
  return isKnownState.value ? props.state : 'unknown';
});
</script>

<template>
  <header class="cafe-capsule-header" :data-state-key="stateKey">
    <p class="cafe-capsule-slot">{{ slotText }}</p>
    <h2 class="cafe-capsule-title">{{ capsuleText }}</h2>
    <p class="cafe-capsule-status" role="status">
      <span class="cafe-capsule-dot" aria-hidden="true"></span>
      <span class="cafe-capsule-state">{{ displayState }}</span>
    </p>
  </header>
</template>

<style scoped>
.cafe-capsule-header {
  background: var(--surface);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3) var(--space-4);
  font-family: var(--font-body);
}
.cafe-capsule-slot {
  margin: 0 0 var(--space-1) 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: var(--leading-body);
}
.cafe-capsule-title {
  margin: 0 0 var(--space-2) 0;
  color: var(--fg);
  font-size: var(--text-lg);
  line-height: var(--leading-tight);
  overflow-wrap: anywhere;
}
.cafe-capsule-status {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-1) var(--space-3);
  background: var(--surface-warm);
  color: var(--muted);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-pill);
  font-size: var(--text-sm);
  line-height: var(--leading-body);
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
</style>
