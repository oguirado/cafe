<template>
  <div class="cafe-actions" :aria-busy="busy ? 'true' : 'false'">
    <div class="cafe-row">
      <button class="cafe-button" type="button" :disabled="recDisabled" @click="onRecord">REC</button>
      <button class="cafe-button" type="button" :disabled="stopDisabled" @click="onStop">Stop</button>
      <button class="cafe-button" type="button" :disabled="obsDisabled" @click="onObs">OBS</button>
    </div>
    <p v-if="recReasonText !== ''" class="cafe-reason" aria-live="polite">{{ recReasonText }}</p>
    <p v-if="stopReasonText !== ''" class="cafe-reason" aria-live="polite">{{ stopReasonText }}</p>
    <p v-if="obsReasonText !== ''" class="cafe-reason" aria-live="polite">{{ obsReasonText }}</p>
  </div>
</template>

<script setup>
// @ts-check
/**
 * Presentational REC / Stop / OBS triggers.
 * Emits carry no payload; parent resolves target and calls existing helpers.
 * Blocked when busy or when reason text is present; defaults block.
 */
import { computed } from 'vue';

const props = defineProps({
  recordReason: { type: String, default: 'Runtime unavailable' },
  stopReason: { type: String, default: 'Runtime unavailable' },
  obsReason: { type: String, default: 'OBS not ready' },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(['record', 'stop', 'obs']);

const recReasonText = computed(() => (typeof props.recordReason === 'string' ? props.recordReason : 'Runtime unavailable'));
const stopReasonText = computed(() => (typeof props.stopReason === 'string' ? props.stopReason : 'Runtime unavailable'));
const obsReasonText = computed(() => (typeof props.obsReason === 'string' ? props.obsReason : 'OBS not ready'));

const recDisabled = computed(() => props.busy === true || recReasonText.value !== '');
const stopDisabled = computed(() => props.busy === true || stopReasonText.value !== '');
const obsDisabled = computed(() => props.busy === true || obsReasonText.value !== '');

function onRecord() {
  if (recDisabled.value) return;
  emit('record');
}

function onStop() {
  if (stopDisabled.value) return;
  emit('stop');
}

function onObs() {
  if (obsDisabled.value) return;
  emit('obs');
}
</script>

<style scoped>
.cafe-actions {
  background: var(--surface);
  color: var(--fg);
  border: var(--cafe-structure-semantic-border-width) solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  font-family: var(--font-body);
}

.cafe-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.cafe-button {
  background: var(--surface-warm);
  color: var(--fg);
  border: var(--cafe-structure-semantic-border-width) solid var(--border-soft);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  font-family: var(--font-body);
  font-size: var(--text-sm);
}

.cafe-button:disabled {
  background: var(--surface);
  color: var(--muted);
  border-color: var(--border);
}

.cafe-button:focus-visible {
  outline: 2px solid var(--muted);
  outline-offset: 2px;
}

.cafe-reason {
  color: var(--muted);
  font-family: var(--font-body);
  font-size: var(--text-xs);
  margin: var(--space-2) 0 0;
}
</style>
