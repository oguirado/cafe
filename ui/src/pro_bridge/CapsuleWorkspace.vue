<script setup>
// @ts-check
/**
 * Presentation only. Expected parent hook: bindCapsuleWorkspace() supplies the
 * {props: {capsules, availability, platform, folderConfigured, ffmpegConfigured,
 * obsReady, recordings, pendingIds}, listeners: {onChooseWindow, onRecord,
 * onStop, onObs, onChooseFolder}} with exactly these fields. It projects
 * coherent, immutable, current-session snapshots, supplies real generations
 * and session aliases, and revalidates every intent in the existing controller.
 * It owns Dockview, the single selector, native approval and receipt tracking.
 * An absent recording in a complete snapshot permits REC; only Running permits
 * Stop. Other states are unknown here. Accepted effects stay locally latched
 * until pendingIds includes, then excludes, that capsule. Selector/folder
 * completion and any listener change republish the snapshot. No latch is a
 * native receipt or approval.
 * Session identity reuse and source revision checks remain with the host across
 * remounts; these props have neither a session epoch nor a snapshot revision.
 * No approved preview source is supplied by this interface, so none is shown.
 */
import { computed, getCurrentInstance, onBeforeUnmount, onBeforeUpdate, reactive, shallowRef } from 'vue';
import { validateCapsules } from './capsule_workspace.js';

defineOptions({ inheritAttrs: false });
const props = defineProps(['capsules', 'availability', 'platform', 'folderConfigured',
  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds']);
const emit = defineEmits(['choose-window', 'record', 'stop', 'obs', 'choose-folder']);
const instance = getCurrentInstance();
const UNAVAILABLE = 'Runtime unavailable';
const listenerNames = ['onChooseWindow', 'onRecord', 'onStop', 'onObs', 'onChooseFolder'];
const propNames = ['capsules', 'availability', 'platform', 'folderConfigured',
  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds'];
/** @typedef {import('./capsule_workspace.js').CapsuleRow} CapsuleRow */
/** @typedef {'record'|'stop'|'obs'} Action */
/** @typedef {{feed_id:string,state:string|null,written_frames:string|null,backpressure:string|null}} RecordingFeed */
/** @typedef {{capsule_id:string,state:string|null,feeds:RecordingFeed[]}} RecordingRow */
/** @typedef {{valid:boolean,bindings:boolean,rows:CapsuleRow[],recordings:RecordingRow[],pendingIds:string[],
 * availability:unknown,platform:unknown,folderConfigured:unknown,ffmpegConfigured:unknown,obsReady:unknown}} Data */
/** @typedef {{sources:unknown[],listeners:unknown[],data:Data}} Snapshot */
/** @typedef {{capsule_id:string,slot:string|null,role:'game'|'camera'|null,pending:boolean,
 * canRecord:boolean,canStop:boolean,canSendToObs:boolean,canChooseWindow:boolean}} CardView */
/** @typedef {{snapshot:Snapshot,row:CapsuleRow,action:Action,confirm:()=>void,reject:()=>void}} Proposal */

/** @param {unknown} value @returns {value is string} */
function identifier(value) { return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value); }
/** @param {unknown} value @returns {value is string} */
function clock(value) {
  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) &&
    (value.length < 20 || value <= '18446744073709551615');
}
/** @param {unknown} value */
function nullableText(value) { return value === null || typeof value === 'string'; }
/** @param {unknown} value */
function nullableBoolean(value) { return value === null || typeof value === 'boolean'; }
/** Own data fields only; never invoke a caller getter.
 * @param {unknown} value @param {string[]} keys @returns {Record<string,unknown>|null} */
function fields(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null || Reflect.ownKeys(value).length !== keys.length) return null;
  const out = Object.create(null);
  for (const key of keys) {
    const field = Object.getOwnPropertyDescriptor(value, key);
    if (!field?.enumerable || !Object.hasOwn(field, 'value')) return null;
    out[key] = field.value;
  }
  return out;
}
/** @param {unknown} value @param {number} max @returns {unknown[]|null} */
function list(value, max) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
  const length = Object.getOwnPropertyDescriptor(value, 'length')?.value;
  if (!Number.isInteger(length) || length < 0 || length > max || Reflect.ownKeys(value).length !== length + 1) return null;
  const out = [];
  for (let i = 0; i < length; i += 1) {
    const item = Object.getOwnPropertyDescriptor(value, String(i));
    if (!item?.enumerable || !Object.hasOwn(item, 'value')) return null;
    out.push(item.value);
  }
  return out;
}
/** @param {string} key @returns {unknown} */
function incoming(key) {
  const source = instance?.vnode.props;
  if (!source) return undefined;
  const field = Object.getOwnPropertyDescriptor(source, key) ??
    Object.getOwnPropertyDescriptor(source, key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()));
  return field && Object.hasOwn(field, 'value') ? field.value : undefined;
}
/** @returns {Snapshot} A fresh, detached projection; failures contain no exception text. */
function readSnapshot() {
  const sources = [props.capsules, props.availability, props.platform, props.folderConfigured,
    props.ffmpegConfigured, props.obsReady, props.recordings, props.pendingIds];
  const listeners = listenerNames.map(incoming);
  const capsules = validateCapsules(props.capsules);
  const validAvailability = props.availability === null || ['available', 'unavailable', 'unknown'].includes(props.availability);
  const validPlatform = ['windows', 'unsupported', 'unknown'].includes(props.platform);
  const validFlags = [props.folderConfigured, props.ffmpegConfigured, props.obsReady].every(nullableBoolean);
  /** @type {Data} */
  const data = { valid: false, bindings: listeners.every(value => typeof value === 'function') &&
    Object.keys(instance?.attrs || {}).length === 0 && propNames.every((key, i) => Object.is(incoming(key), sources[i])),
    rows: capsules.rows, recordings: [], pendingIds: [],
    availability: validAvailability && props.availability !== null ? props.availability : 'unknown',
    platform: validPlatform ? props.platform : 'unknown', folderConfigured: validFlags ? props.folderConfigured : null,
    ffmpegConfigured: validFlags ? props.ffmpegConfigured : null, obsReady: validFlags ? props.obsReady : null };
  const result = { sources, listeners, data };
  try {
    if (!capsules.ok || !validAvailability || !validPlatform || !validFlags) return result;
    const pending = list(props.pendingIds, 2), recordings = list(props.recordings, 2);
    if (!pending || !recordings) return result;
    const ids = new Set(data.rows.map(row => row.capsule_id));
    const pendingSeen = new Set(), recordingSeen = new Set(), feedSeen = new Set();
    for (const id of pending) {
      if (!identifier(id) || !ids.has(id) || pendingSeen.has(id)) return result;
      pendingSeen.add(id); data.pendingIds.push(id);
    }
    for (const item of recordings) {
      const row = fields(item, ['capsule_id', 'state', 'feeds']);
      if (!row || !identifier(row.capsule_id) || !ids.has(row.capsule_id) || recordingSeen.has(row.capsule_id) ||
          !nullableText(row.state)) return result;
      recordingSeen.add(row.capsule_id);
      const capsule = data.rows.find(value => value.capsule_id === row.capsule_id);
      const feeds = list(row.feeds, 2);
      if (!capsule || !feeds) return result;
      /** @type {RecordingFeed[]} */ const normalized = [];
      for (const itemFeed of feeds) {
        const feed = fields(itemFeed, ['feed_id', 'state', 'written_frames', 'backpressure']);
        if (!feed || !identifier(feed.feed_id) || feedSeen.has(feed.feed_id) ||
            !capsule.feeds.some(value => value.feed_id === feed.feed_id) || !nullableText(feed.state) ||
            !(feed.written_frames === null || clock(feed.written_frames)) ||
            !(feed.backpressure === null || clock(feed.backpressure))) return result;
        feedSeen.add(feed.feed_id);
        normalized.push(/** @type {RecordingFeed} */ ({ feed_id: feed.feed_id, state: feed.state,
          written_frames: feed.written_frames, backpressure: feed.backpressure }));
      }
      data.recordings.push(/** @type {RecordingRow} */ ({ capsule_id: row.capsule_id, state: row.state, feeds: normalized }));
    }
    data.valid = true;
  } catch { /* Closed refusal; never expose a caller exception. */ }
  if (!data.valid) { data.recordings = []; data.pendingIds = []; }
  return result;
}
/** Compare only our normalized value objects, never caller objects.
 * @param {unknown} a @param {unknown} b @returns {boolean} */
function same(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) &&
    same(/** @type {Record<string,unknown>} */ (a)[key], /** @type {Record<string,unknown>} */ (b)[key]));
}

let alive = true;
/** @type {import('vue').ShallowRef<Snapshot|null>} */ const snapshot = shallowRef(null);
/** @type {import('vue').ShallowRef<Proposal|null>} */ const proposal = shallowRef(null);
/** These transient click guards do not own or manage any host session. */
const dispatched = reactive(new Map());
const choosing = reactive(new Set());
const folderPending = shallowRef(false);
const retired = new Set();
/** @type {Map<string,{owner:string,role:string,generation:string}>} */ const seen = new Map();
/** @type {Set<string>} */ let previousIds = new Set();

/** @param {Data} data */
function observeIdentities(data) {
  if (!data.valid || data.availability !== 'available') return;
  const current = new Set();
  for (const row of data.rows) {
    const identities = [{ id: row.capsule_id, owner: row.capsule_id, role: 'capsule', generation: row.generation },
      ...row.feeds.map(feed => ({ id: feed.feed_id, owner: row.capsule_id, role: feed.role, generation: feed.generation }))];
    for (const identity of identities) {
      current.add(identity.id);
      const old = seen.get(identity.id);
      if (old && (old.owner !== identity.owner || old.role !== identity.role ||
          identity.generation.length < old.generation.length ||
          identity.generation.length === old.generation.length && identity.generation < old.generation)) retired.add(identity.id);
      if (!retired.has(identity.id)) seen.set(identity.id, identity);
    }
  }
  for (const id of previousIds) if (!current.has(id)) retired.add(id);
  previousIds = current;
}
/** Recheck even in the interval between a prop mutation and the next render.
 * @returns {Snapshot} */
function refresh() {
  const next = readSnapshot(), old = snapshot.value;
  if (old && old.sources.every((value, i) => Object.is(value, next.sources[i])) &&
      old.listeners.every((value, i) => value === next.listeners[i]) && same(old.data, next.data)) return old;
  proposal.value = null;
  choosing.clear(); folderPending.value = false;
  observeIdentities(next.data);
  if (next.data.valid && next.data.availability === 'available') {
    for (const [id, acknowledged] of dispatched) {
      if (next.data.pendingIds.includes(id)) dispatched.set(id, true);
      else if (acknowledged) dispatched.delete(id);
    }
  }
  snapshot.value = next;
  return next;
}
refresh();
onBeforeUpdate(refresh);
onBeforeUnmount(() => { alive = false; proposal.value = null; });

/** @param {Snapshot} value @returns {string} */
function unavailableReason(value) {
  const data = value.data;
  if (!alive || !data.valid || !data.bindings || data.availability !== 'available') return UNAVAILABLE;
  return data.platform === 'windows' ? '' : 'Capture needs Windows';
}
/** @param {CapsuleRow} row @param {Snapshot} value @returns {CardView} */
function cardView(row, value) {
  const data = value.data;
  const pending = data.pendingIds.includes(row.capsule_id) || dispatched.has(row.capsule_id) || choosing.has(row.capsule_id);
  const exact = data.rows.some(current => current === row) && !retired.has(row.capsule_id) &&
    row.feeds.every(feed => !retired.has(feed.feed_id));
  const enabled = unavailableReason(value) === '' && exact && !pending;
  const recording = data.recordings.find(entry => entry.capsule_id === row.capsule_id);
  const role = row.feeds.find(feed => feed.role === 'game')?.role ?? row.feeds[0]?.role ?? null;
  return { capsule_id: row.capsule_id, slot: row.slot, role, pending,
    canRecord: enabled && role !== null && data.folderConfigured === true && data.ffmpegConfigured === true && !recording,
    canStop: enabled && recording?.state === 'Running',
    canSendToObs: enabled && role !== null && data.obsReady === true,
    canChooseWindow: enabled };
}
/** @param {CapsuleRow} row @param {Snapshot} origin @param {Action} action */
function allowed(row, origin, action) {
  if (!alive || refresh() !== origin) return false;
  const view = cardView(row, origin);
  return action === 'record' ? view.canRecord : action === 'stop' ? view.canStop : view.canSendToObs;
}
/** @param {CapsuleRow} row @param {Snapshot} origin @param {Action} action */
function propose(row, origin, action) {
  if (!allowed(row, origin, action)) return;
  /** @type {Proposal} */
  const next = { snapshot: origin, row, action,
    confirm: () => {
      if (proposal.value !== next || !allowed(row, origin, action) || proposal.value !== next) return;
      proposal.value = null;
      dispatched.set(row.capsule_id, false); // Set before emit, including synchronous reentry.
      emit(action, { capsule_id: row.capsule_id });
    },
    reject: () => { if (proposal.value === next) proposal.value = null; } };
  proposal.value = next;
}
/** @param {CapsuleRow} row @param {Snapshot} origin @param {'game'|'camera'} role */
function chooseWindow(row, origin, role) {
  if (!alive || refresh() !== origin || !cardView(row, origin).canChooseWindow ||
      (role !== 'game' && role !== 'camera')) return;
  proposal.value = null; choosing.add(row.capsule_id);
  emit('choose-window', { capsule_id: row.capsule_id, role });
}
const cards = computed(() => {
  const origin = /** @type {Snapshot} */ (snapshot.value);
  return origin.data.rows.map(row => ({ row, view: cardView(row, origin),
    recording: origin.data.valid ? origin.data.recordings.find(entry => entry.capsule_id === row.capsule_id) : undefined,
    feeds: (/** @type {Array<'game'|'camera'>} */ (['game', 'camera'])).map(role => ({ role,
      feed: row.feeds.find(feed => feed.role === role), choose: () => chooseWindow(row, origin, role) })),
    record: () => propose(row, origin, 'record'), stop: () => propose(row, origin, 'stop'), obs: () => propose(row, origin, 'obs') }));
});
const reason = computed(() => unavailableReason(/** @type {Snapshot} */ (snapshot.value)));
const folder = computed(() => {
  const origin = /** @type {Snapshot} */ (snapshot.value);
  return { disabled: reason.value !== '' || folderPending.value,
    choose: () => {
      if (!alive || refresh() !== origin || unavailableReason(origin) !== '' || folderPending.value) return;
      proposal.value = null; folderPending.value = true;
      emit('choose-folder');
    } };
});
</script>

<template>
  <section class="capsule-workspace" aria-label="Capsule workspace">
    <header class="capsule-workspace__header">
      <h2>Capsules</h2>
      <button type="button" :disabled="folder.disabled" v-on="{ click: folder.choose }">Choose folder</button>
    </header>
    <p v-if="reason" class="capsule-workspace__notice" role="status">{{ reason }}</p>
    <p v-else-if="cards.length === 0" role="status">No capsules in this session.</p>
    <article v-for="card in cards" :key="card.row.capsule_id" :data-capsule-id="card.row.capsule_id"
      class="capsule-workspace__card" aria-label="Capsule">
      <header class="capsule-workspace__header">
        <h3>Capsule · {{ card.row.slot ?? 'Slot unknown' }}</h3>
        <span class="capsule-workspace__identity">{{ card.row.capsule_id }}</span>
      </header>
      <p v-if="card.row.person_label !== null">{{ card.row.person_label }}</p>
      <div class="capsule-workspace__feeds">
        <section v-for="item in card.feeds" :key="item.role" class="capsule-workspace__feed">
          <h4>{{ item.role === 'game' ? 'Game feed' : 'Camera feed' }}</h4>
          <p v-if="item.feed" class="capsule-workspace__identity">{{ item.feed.feed_id }} · generation {{ item.feed.generation }}</p>
          <p v-else>No {{ item.role }} feed paired.</p>
          <p class="capsule-workspace__absence">No approved preview source.</p>
          <button type="button" :disabled="!card.view.canChooseWindow" v-on="{ click: item.choose }">Choose {{ item.role }} window</button>
        </section>
      </div>
      <p v-if="card.recording">Recording state: {{ card.recording.state ?? 'Unknown' }}</p>
      <p v-else>{{ reason ? 'Recording state unknown.' : 'No current native recording.' }}</p>
      <ul v-if="card.recording">
        <li v-for="feed in card.recording.feeds" :key="feed.feed_id">
          {{ feed.feed_id }} · {{ feed.state ?? 'Unknown' }} · frames {{ feed.written_frames ?? 'Unknown' }} · backpressure {{ feed.backpressure ?? 'Unknown' }}
        </li>
      </ul>
      <p v-if="card.view.pending" role="status">Request pending.</p>
      <div class="capsule-workspace__actions">
        <button type="button" :disabled="!card.view.canRecord" v-on="{ click: card.record }">REC</button>
        <button type="button" :disabled="!card.view.canStop" v-on="{ click: card.stop }">Stop</button>
        <button type="button" :disabled="!card.view.canSendToObs" v-on="{ click: card.obs }">OBS</button>
      </div>
    </article>
    <section v-if="proposal" class="capsule-workspace__confirmation" role="dialog" aria-label="Confirm capsule action" aria-modal="false">
      <h3>{{ proposal.action === 'record' ? 'Confirm recording' : proposal.action === 'stop' ? 'Confirm stop' : 'Confirm send to OBS' }}</h3>
      <p>Capsule {{ proposal.row.capsule_id }}</p>
      <p>{{ proposal.action === 'record' ? 'Request recording of the paired feeds?' : proposal.action === 'stop' ? 'Request stopping the current recording?' : 'Request sending this capsule to OBS?' }}</p>
      <div class="capsule-workspace__actions">
        <!-- Pass the captured functions themselves; no cached late-bound wrapper. -->
        <button type="button" v-on="{ click: proposal.confirm }">Confirm</button>
        <button type="button" v-on="{ click: proposal.reject }">Reject</button>
      </div>
    </section>
  </section>
</template>

<style src="./capsule_workspace.css"></style>
