<template>
  <div class="cafe-actions" :aria-busy="busy ? 'true' : 'false'">
    <div class="cafe-actions-row">
      <button v-if="state === 'recording'" class="cafe-button cafe-rec-primary cafe-stop" type="button" :disabled="stopDisabled" @click="onStop">Stop</button>
      <button v-else-if="state === 'window_closed'" class="cafe-button cafe-rec-primary" type="button" :disabled="busy" @click="emit('remove')">Remove capsule</button>
      <button v-else class="cafe-button cafe-rec-primary" type="button" :disabled="recDisabled || state === 'connecting' || state === 'window_minimized'" @click="onRecord">{{ state === 'recording_failed' ? 'Retry' : 'REC' }}</button>
      <span class="cafe-obs-wrap" :title="obsReasonText || undefined" @click="onObsAttempt">
        <button class="cafe-button cafe-obs-button" type="button" :disabled="obsDisabled" :title="obsReasonText || undefined" @click="onObs">OBS</button>
      </span>
    </div>
    <p v-if="hasProblem" class="cafe-reason" aria-live="polite">
      <template v-if="state === 'recording_failed'">{{ failureReason }}<template v-if="result"> · {{ result }}</template></template>
      <template v-else-if="result">{{ result }}</template>
      <template v-else-if="state === 'window_minimized'">Restore it in Windows</template>
      <template v-else-if="state === 'recording' && stopReasonText !== ''">{{ stopReasonText }}</template>
      <template v-else-if="recReasonText !== '' && state !== 'recording'">{{ recReasonText }} <button v-if="resolveLabel" class="cafe-inline-action" type="button" @click="emit('resolve')">{{ resolveLabel }}</button></template>
      <template v-else-if="obsClicked && obsReasonText !== ''">{{ obsReasonText }}</template>
    </p>
  </div>
</template>

<script setup>
// @ts-check
/**
 * Presentational REC / Stop / OBS triggers.
 * Emits carry no payload; parent resolves target and calls existing helpers.
 * Blocked when busy or when reason text is present; defaults block.
 */
import { computed, ref, watch } from 'vue';

const props = defineProps({
  recordReason: { type: String, default: 'Runtime unavailable' },
  stopReason: { type: String, default: 'Runtime unavailable' },
  obsReason: { type: String, default: 'OBS not ready' },
  busy: { type: Boolean, default: false },
  state: { type: String, default: 'no_preview' },
  resolveLabel: { type: String, default: '' },
  failureReason: { type: String, default: 'Recording reason unavailable. Open diagnostics before retrying.' },
  result: { type: String, default: '' },
});

const emit = defineEmits(['record', 'stop', 'obs', 'resolve', 'remove']);

const obsClicked = ref(false);

const recReasonText = computed(() => (typeof props.recordReason === 'string' ? props.recordReason : 'Runtime unavailable'));
const stopReasonText = computed(() => (typeof props.stopReason === 'string' ? props.stopReason : 'Runtime unavailable'));
const obsReasonText = computed(() => (typeof props.obsReason === 'string' ? props.obsReason : 'OBS not ready'));

const recDisabled = computed(() => props.busy === true || recReasonText.value !== '');
const stopDisabled = computed(() => props.busy === true || stopReasonText.value !== '');
const obsDisabled = computed(() => props.busy === true || obsReasonText.value !== '');

const hasProblem = computed(() => (
  props.state === 'recording_failed'
  || Boolean(props.result)
  || props.state === 'window_minimized'
  || (props.state === 'recording' && stopReasonText.value !== '')
  || (recReasonText.value !== '' && props.state !== 'recording')
  || (obsClicked.value && obsReasonText.value !== '')
));

watch(obsReasonText, (val) => {
  if (val === '') obsClicked.value = false;
});

function onRecord() {
  if (recDisabled.value) return;
  emit('record');
}

function onStop() {
  if (stopDisabled.value) return;
  emit('stop');
}

function onObsAttempt() {
  if (obsDisabled.value && obsReasonText.value !== '') {
    obsClicked.value = true;
  }
}

function onObs() {
  if (obsDisabled.value) {
    if (obsReasonText.value !== '') obsClicked.value = true;
    return;
  }
  obsClicked.value = false;
  emit('obs');
}
</script>

<style scoped>
.cafe-actions {
  flex: 0 0 auto;
  background: var(--surface);
  color: var(--fg);
  border: 0;
  border-radius: 0;
  padding: 0 var(--space-2);
  margin: 0;
  font-family: var(--font-body);
  box-sizing: border-box;
}

.cafe-actions-row {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: var(--space-2);
  height: 40px;
  min-height: 40px;
  max-height: 40px;
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

.cafe-rec-primary {
  flex: 1 1 auto;
  min-width: 6rem;
  height: 32px;
  min-height: 32px;
  font-size: var(--text-sm);
  font-weight: var(--cafe-structure-semantic-typography-font-weight-semibold);
}

.cafe-obs-wrap {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
}

.cafe-obs-button {
  flex: 0 0 auto;
  height: 32px;
  min-height: 32px;
}

.cafe-obs-button:disabled {
  pointer-events: none;
}

.cafe-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  background: var(--surface-warm);
  color: var(--fg);
  border: var(--cafe-structure-semantic-border-width) solid var(--border-soft);
  border-radius: var(--radius-sm);
  padding: 0 var(--space-3);
  font-family: var(--font-body);
  font-size: var(--text-sm);
  line-height: 1;
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
  line-height: 1.2;
  margin: 0;
  padding: 0 0 var(--space-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cafe-inline-action { border: 0; background: transparent; color: var(--accent); padding: 0; font: inherit; text-decoration: underline; cursor: pointer; }
.cafe-inline-action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cafe-stop { border-color: var(--danger); }
</style>
