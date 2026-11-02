<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { planSecondsRemaining } from './control.js';

const props = defineProps({ plan: { type: Object, required: true }, busy: { type: Boolean, default: false }, result: { type: String, default: '' } });
const emit = defineEmits(['confirm', 'reject']);
const now = ref(Date.now());
let timer = null;
const remaining = computed(() => planSecondsRemaining(props.plan.expires_unix_ms, now.value));
const countdown = computed(() => remaining.value === null ? 'Expiry unavailable'
  : remaining.value === 0 ? 'Expired · request a new plan' : `Expires in ${remaining.value}s`);
function confirm() {
  if (props.busy || remaining.value === null || remaining.value <= 0) return;
  emit('confirm', props.plan.plan_id);
}
function reject() { if (!props.busy) emit('reject', props.plan.plan_id); }
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => { if (timer !== null) clearInterval(timer); });
</script>

<template>
  <section class="cafe-organizer-plan" aria-labelledby="cafe-organizer-title" :aria-busy="busy ? 'true' : 'false'">
    <div>
      <h2 id="cafe-organizer-title">AI plan · approval required</h2>
      <p>Review the windows and destination. Confirm submits approval to the authority.</p>
      <ul><li v-for="(entry, index) in plan.windows" :key="index">{{ entry.title }}</li></ul>
      <p><strong>Folder:</strong> {{ plan.folder_name }}</p>
    </div>
    <div class="cafe-organizer-actions">
      <p>{{ countdown }}</p>
      <button class="cafe-button" type="button" :disabled="busy || remaining === null || remaining <= 0" @click="confirm">Confirm</button>
      <button class="cafe-button cafe-button-secondary" type="button" :disabled="busy" @click="reject">Reject</button>
      <p v-if="result" role="status">{{ result }}</p>
    </div>
  </section>
</template>
