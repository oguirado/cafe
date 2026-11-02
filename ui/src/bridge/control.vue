<script setup>
// @ts-check
/**
 * One presentation-only proposal card. Integration: the parent supplies a
 * session-scoped view, native availability and a Set of pending proposal ids.
 * It consumes the two id-only events after separate native-adapter review.
 * Mount once per session; no native approval or execution is implemented here.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, toRaw, watch } from 'vue';
import { validateProposalView } from './control_schema.js';

defineOptions({ inheritAttrs: false });
const props = defineProps(['proposal', 'availability', 'pending']);
const emit = defineEmits(['confirm', 'reject']);

/** @typedef {Readonly<import('./control_schema.js').ProposalView>} ProposalView */
/**
 * @typedef {object} Presentation
 * @property {ProposalView} view
 * @property {number} acceptedAtMs An actual observed clock, never a sentinel.
 * @property {boolean} live Permanently false after this presentation changes.
 * @property {() => void} confirm
 * @property {() => void} reject
 */

const FIELDS = Object.freeze([
  'proposal_id', 'op', 'capsule_id', 'slot', 'role', 'expires_at_ms',
]);
const LABELS = Object.freeze({
  bind_window: 'Bind window', start_recording: 'Start recording',
  stop_recording: 'Stop recording', send_to_obs: 'Send to OBS',
});
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}(?![\s\S])/;
const MAX_PENDING = 64;
const MAX_DECISIONS = 1024;
const SET_SIZE = Object.getOwnPropertyDescriptor(Set.prototype, 'size')?.get;

/** @type {import('vue').ShallowRef<Presentation|null>} */
const presentation = shallowRef(null);
/** Only suppress repeated UI events; this is not a proposal queue or authority. */
/** @type {Set<string>} */
const decided = new Set();
const decisionCount = ref(0);
let live = true;
/** Largest valid local clock seen; rollback must never revive an expired card. @type {number|null} */
let latestClock = null;
/** @type {ReturnType<typeof setInterval>|null} */
let ticker = null;

/** Unknown local time can only inhibit confirmation. @returns {number|null} */
function readClock() {
  try {
    const value = Date.now();
    if (!Number.isSafeInteger(value) || value < 0
        || (latestClock !== null && value < latestClock)) return null;
    latestClock = value;
    return value;
  } catch {
    return null;
  }
}
const nowMs = ref(readClock());

/**
 * Read only a real session Set; never convert a duplicate list to a Set.
 * Vue's collection iteration also tracks in-place membership changes.
 * @returns {string[]|null}
 */
function pendingIds() {
  try {
    const value = props.pending;
    if (value === null || typeof value !== 'object') return null;
    const raw = toRaw(value);
    if (Object.getPrototypeOf(raw) !== Set.prototype || Reflect.ownKeys(raw).length !== 0) return null;
    // Brand-check and enumerate the underlying native Set with its intrinsics.
    // A proxy cannot supply fake size/iterator values to manufacture membership.
    if (SET_SIZE === undefined) return null;
    const size = SET_SIZE.call(raw);
    if (!Number.isSafeInteger(size) || size < 0 || size > MAX_PENDING) return null;
    // Track Vue's collection dependency, but never use its returned value.
    void value.size;
    /** @type {string[]} */
    const ids = [];
    for (const id of Set.prototype.values.call(raw)) {
      if (typeof id !== 'string' || !ID.test(id) || ids.length >= MAX_PENDING) return null;
      ids.push(id);
    }
    return ids.length === size ? ids : null;
  } catch {
    return null;
  }
}

/** @param {ProposalView|null} a @param {ProposalView|null} b */
function sameView(a, b) {
  return a !== null && b !== null && a.proposal_id === b.proposal_id
    && a.op === b.op && a.capsule_id === b.capsule_id && a.slot === b.slot
    && a.role === b.role && a.expires_at_ms === b.expires_at_ms;
}

/**
 * Track each reactive data field without invoking accessors. The validator
 * itself reads descriptors, which alone do not track Vue value mutations.
 * A synchronous observer kills A's handlers even for an A -> B -> A change
 * before Vue next renders. Unexpected input produces only a closed failure.
 * @returns {unknown[]}
 */
function observeInputs() {
  const observed = [props.proposal, props.availability, props.pending];
  try {
    const value = props.proposal;
    if (value !== null && typeof value === 'object') {
      observed.push(...Reflect.ownKeys(value));
      for (const key of FIELDS) {
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        if (descriptor && Object.prototype.hasOwnProperty.call(descriptor, 'value')) {
          observed.push(value[key]);
        }
      }
    }
    observed.push(pendingIds());
  } catch {
    observed.push(null);
  }
  return observed;
}

/** @param {Presentation} card */
function isKnown(card) {
  if (!live || !card.live || presentation.value !== card
      || decided.has(card.view.proposal_id) || decided.size >= MAX_DECISIONS) return false;
  const ids = pendingIds();
  if (ids === null || !ids.includes(card.view.proposal_id)) return false;
  // This retained real observation time permits Reject after expiry. It never
  // permits Confirm: Confirm separately validates against a fresh local clock.
  const checked = validateProposalView(props.proposal, card.acceptedAtMs);
  return checked.ok && sameView(checked.view, card.view);
}

/** @param {Presentation} card */
function canConfirm(card) {
  if (!isKnown(card) || props.availability !== 'available') return false;
  const checked = validateProposalView(props.proposal, readClock());
  return checked.ok && sameView(checked.view, card.view);
}

/** @param {Presentation} card @param {'confirm'|'reject'} kind */
function decide(card, kind) {
  if (kind === 'confirm' ? !canConfirm(card) : !isKnown(card)) return;
  // Reflective input may trigger a synchronous prop update during validation.
  // Recheck liveness after all such reads, immediately before taking the latch.
  if (!live || !card.live || presentation.value !== card
      || decided.has(card.view.proposal_id) || decided.size >= MAX_DECISIONS
      || (kind === 'confirm' && props.availability !== 'available')) return;
  // Latch before emit, including synchronous parent listeners and double clicks.
  decided.add(card.view.proposal_id);
  decisionCount.value = decided.size;
  emit(kind, Object.freeze({ proposal_id: card.view.proposal_id }));
}

function replacePresentation() {
  const previous = presentation.value;
  if (previous !== null) previous.live = false;
  const now = readClock();
  nowMs.value = now;
  const checked = validateProposalView(props.proposal, now);
  let view = checked.view;
  let acceptedAtMs = now;
  if (!checked.ok && previous !== null) {
    // Preserve only the same previously validated view for explicit rejection.
    // A different malformed or already expired incoming proposal stays unknown.
    const known = validateProposalView(props.proposal, previous.acceptedAtMs);
    if (known.ok && sameView(known.view, previous.view)) {
      view = known.view;
      acceptedAtMs = previous.acceptedAtMs;
    }
  }
  if (view === null || acceptedAtMs === null) {
    presentation.value = null;
    return;
  }
  /** @type {Presentation} */
  const card = {
    view, acceptedAtMs, live: true,
    confirm: () => decide(card, 'confirm'),
    reject: () => decide(card, 'reject'),
  };
  presentation.value = card;
}

watch(observeInputs, replacePresentation, { immediate: true, flush: 'sync' });

const confirmEnabled = computed(() => {
  // The ticker only refreshes presentation; the click samples time again.
  void nowMs.value;
  void decisionCount.value;
  const card = presentation.value;
  return card !== null && canConfirm(card);
});
const rejectEnabled = computed(() => {
  void decisionCount.value;
  const card = presentation.value;
  return card !== null && isKnown(card);
});
const status = computed(() => {
  void decisionCount.value;
  const card = presentation.value;
  if (card !== null && decided.has(card.view.proposal_id)) return 'Decision submitted.';
  if (props.availability !== 'available') return 'Native state unavailable.';
  if (card === null || !isKnown(card)) return 'Proposal unavailable.';
  return confirmEnabled.value ? 'Ready for your confirmation.' : 'Confirmation unavailable.';
});
const validity = computed(() => {
  const card = presentation.value;
  const now = nowMs.value;
  if (card === null || now === null) return 'Validity unavailable.';
  const remaining = card.view.expires_at_ms - now;
  return remaining <= 0 ? 'Proposal expired.' : 'Valid for ' + Math.ceil(remaining / 1000) + ' s.';
});

onMounted(() => {
  ticker = setInterval(() => { nowMs.value = readClock(); }, 250);
});
onBeforeUnmount(() => {
  live = false;
  if (presentation.value !== null) presentation.value.live = false;
  if (ticker !== null) clearInterval(ticker);
  ticker = null;
  decided.clear();
});
</script>

<template>
  <section class="cafe-panel control-card" aria-labelledby="control-proposal-title">
    <h2 id="control-proposal-title" class="cafe-heading">Review proposal</h2>
    <dl v-if="presentation !== null" class="cafe-list">
      <div class="cafe-row">
        <dt class="cafe-row-main">Operation</dt><dd>{{ LABELS[presentation.view.op] }}</dd>
      </div>
      <div class="cafe-row">
        <dt class="cafe-row-main">{{ presentation.view.slot === null ? 'Capsule' : 'Session slot' }}</dt>
        <dd>{{ presentation.view.slot ?? presentation.view.capsule_id }}</dd>
      </div>
      <div v-if="presentation.view.role !== null" class="cafe-row">
        <dt class="cafe-row-main">Role</dt><dd>{{ presentation.view.role === 'game' ? 'Game' : 'Camera' }}</dd>
      </div>
    </dl>
    <p class="cafe-status" role="status">{{ status }}</p>
    <p class="cafe-text">{{ validity }}</p>
    <p class="cafe-text">Preview unavailable.</p>
    <div class="cafe-controls control-actions">
      <button type="button" class="cafe-button" :disabled="!rejectEnabled" :onClick="presentation?.reject">Reject</button>
      <button type="button" class="cafe-button" :disabled="!confirmEnabled" :onClick="presentation?.confirm">Confirm</button>
    </div>
  </section>
</template>

<style scoped>
/* The host already loads the existing token-backed cafe skin. Only layout is local. */
.control-card { box-sizing: border-box; width: 100%; }
dd { margin: 0; overflow-wrap: anywhere; }
.control-actions { justify-content: flex-end; }
</style>
