# PRO-03 — Capsule Workspace P1

```diff
diff --git a/ui/src/pro_bridge/CapsuleWorkspace.vue b/ui/src/pro_bridge/CapsuleWorkspace.vue
new file mode 100644
index 0000000..18bff78
--- /dev/null
+++ b/ui/src/pro_bridge/CapsuleWorkspace.vue
@@ -0,0 +1,323 @@
+<script setup>
+// @ts-check
+/**
+ * Presentation only. Expected parent hook: bindCapsuleWorkspace() supplies the
+ * {props: {capsules, availability, platform, folderConfigured, ffmpegConfigured,
+ * obsReady, recordings, pendingIds}, listeners: {onChooseWindow, onRecord,
+ * onStop, onObs, onChooseFolder}} with exactly these fields. It projects
+ * coherent, immutable, current-session snapshots, supplies real generations
+ * and session aliases, and revalidates every intent in the existing controller.
+ * It owns Dockview, the single selector, native approval and receipt tracking.
+ * An absent recording in a complete snapshot permits REC; only Running permits
+ * Stop. Other states are unknown here. Accepted effects stay locally latched
+ * until pendingIds includes, then excludes, that capsule. Selector/folder
+ * completion and any listener change republish the snapshot. No latch is a
+ * native receipt or approval.
+ * Session identity reuse and source revision checks remain with the host across
+ * remounts; these props have neither a session epoch nor a snapshot revision.
+ * No approved preview source is supplied by this interface, so none is shown.
+ */
+import { computed, getCurrentInstance, onBeforeUnmount, onBeforeUpdate, reactive, shallowRef } from 'vue';
+import { validateCapsules } from './capsule_workspace.js';
+
+defineOptions({ inheritAttrs: false });
+const props = defineProps(['capsules', 'availability', 'platform', 'folderConfigured',
+  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds']);
+const emit = defineEmits(['choose-window', 'record', 'stop', 'obs', 'choose-folder']);
+const instance = getCurrentInstance();
+const UNAVAILABLE = 'Runtime unavailable';
+const listenerNames = ['onChooseWindow', 'onRecord', 'onStop', 'onObs', 'onChooseFolder'];
+const propNames = ['capsules', 'availability', 'platform', 'folderConfigured',
+  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds'];
+/** @typedef {import('./capsule_workspace.js').CapsuleRow} CapsuleRow */
+/** @typedef {'record'|'stop'|'obs'} Action */
+/** @typedef {{feed_id:string,state:string|null,written_frames:string|null,backpressure:string|null}} RecordingFeed */
+/** @typedef {{capsule_id:string,state:string|null,feeds:RecordingFeed[]}} RecordingRow */
+/** @typedef {{valid:boolean,bindings:boolean,rows:CapsuleRow[],recordings:RecordingRow[],pendingIds:string[],
+ * availability:unknown,platform:unknown,folderConfigured:unknown,ffmpegConfigured:unknown,obsReady:unknown}} Data */
+/** @typedef {{sources:unknown[],listeners:unknown[],data:Data}} Snapshot */
+/** @typedef {{capsule_id:string,slot:string|null,role:'game'|'camera'|null,pending:boolean,
+ * canRecord:boolean,canStop:boolean,canSendToObs:boolean,canChooseWindow:boolean}} CardView */
+/** @typedef {{snapshot:Snapshot,row:CapsuleRow,action:Action,confirm:()=>void,reject:()=>void}} Proposal */
+
+/** @param {unknown} value @returns {value is string} */
+function identifier(value) { return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value); }
+/** @param {unknown} value @returns {value is string} */
+function clock(value) {
+  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) &&
+    (value.length < 20 || value <= '18446744073709551615');
+}
+/** @param {unknown} value */
+function nullableText(value) { return value === null || typeof value === 'string'; }
+/** @param {unknown} value */
+function nullableBoolean(value) { return value === null || typeof value === 'boolean'; }
+/** Own data fields only; never invoke a caller getter.
+ * @param {unknown} value @param {string[]} keys @returns {Record<string,unknown>|null} */
+function fields(value, keys) {
+  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
+  const prototype = Object.getPrototypeOf(value);
+  if (prototype !== Object.prototype && prototype !== null || Reflect.ownKeys(value).length !== keys.length) return null;
+  const out = Object.create(null);
+  for (const key of keys) {
+    const field = Object.getOwnPropertyDescriptor(value, key);
+    if (!field?.enumerable || !Object.hasOwn(field, 'value')) return null;
+    out[key] = field.value;
+  }
+  return out;
+}
+/** @param {unknown} value @param {number} max @returns {unknown[]|null} */
+function list(value, max) {
+  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
+  const length = Object.getOwnPropertyDescriptor(value, 'length')?.value;
+  if (!Number.isInteger(length) || length < 0 || length > max || Reflect.ownKeys(value).length !== length + 1) return null;
+  const out = [];
+  for (let i = 0; i < length; i += 1) {
+    const item = Object.getOwnPropertyDescriptor(value, String(i));
+    if (!item?.enumerable || !Object.hasOwn(item, 'value')) return null;
+    out.push(item.value);
+  }
+  return out;
+}
+/** @param {string} key @returns {unknown} */
+function incoming(key) {
+  const source = instance?.vnode.props;
+  if (!source) return undefined;
+  const field = Object.getOwnPropertyDescriptor(source, key) ??
+    Object.getOwnPropertyDescriptor(source, key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()));
+  return field && Object.hasOwn(field, 'value') ? field.value : undefined;
+}
+/** @returns {Snapshot} A fresh, detached projection; failures contain no exception text. */
+function readSnapshot() {
+  const sources = [props.capsules, props.availability, props.platform, props.folderConfigured,
+    props.ffmpegConfigured, props.obsReady, props.recordings, props.pendingIds];
+  const listeners = listenerNames.map(incoming);
+  const capsules = validateCapsules(props.capsules);
+  const validAvailability = props.availability === null || ['available', 'unavailable', 'unknown'].includes(props.availability);
+  const validPlatform = ['windows', 'unsupported', 'unknown'].includes(props.platform);
+  const validFlags = [props.folderConfigured, props.ffmpegConfigured, props.obsReady].every(nullableBoolean);
+  /** @type {Data} */
+  const data = { valid: false, bindings: listeners.every(value => typeof value === 'function') &&
+    Object.keys(instance?.attrs || {}).length === 0 && propNames.every((key, i) => Object.is(incoming(key), sources[i])),
+    rows: capsules.rows, recordings: [], pendingIds: [],
+    availability: validAvailability && props.availability !== null ? props.availability : 'unknown',
+    platform: validPlatform ? props.platform : 'unknown', folderConfigured: validFlags ? props.folderConfigured : null,
+    ffmpegConfigured: validFlags ? props.ffmpegConfigured : null, obsReady: validFlags ? props.obsReady : null };
+  const result = { sources, listeners, data };
+  try {
+    if (!capsules.ok || !validAvailability || !validPlatform || !validFlags) return result;
+    const pending = list(props.pendingIds, 2), recordings = list(props.recordings, 2);
+    if (!pending || !recordings) return result;
+    const ids = new Set(data.rows.map(row => row.capsule_id));
+    const pendingSeen = new Set(), recordingSeen = new Set(), feedSeen = new Set();
+    for (const id of pending) {
+      if (!identifier(id) || !ids.has(id) || pendingSeen.has(id)) return result;
+      pendingSeen.add(id); data.pendingIds.push(id);
+    }
+    for (const item of recordings) {
+      const row = fields(item, ['capsule_id', 'state', 'feeds']);
+      if (!row || !identifier(row.capsule_id) || !ids.has(row.capsule_id) || recordingSeen.has(row.capsule_id) ||
+          !nullableText(row.state)) return result;
+      recordingSeen.add(row.capsule_id);
+      const capsule = data.rows.find(value => value.capsule_id === row.capsule_id);
+      const feeds = list(row.feeds, 2);
+      if (!capsule || !feeds) return result;
+      /** @type {RecordingFeed[]} */ const normalized = [];
+      for (const itemFeed of feeds) {
+        const feed = fields(itemFeed, ['feed_id', 'state', 'written_frames', 'backpressure']);
+        if (!feed || !identifier(feed.feed_id) || feedSeen.has(feed.feed_id) ||
+            !capsule.feeds.some(value => value.feed_id === feed.feed_id) || !nullableText(feed.state) ||
+            !(feed.written_frames === null || clock(feed.written_frames)) ||
+            !(feed.backpressure === null || clock(feed.backpressure))) return result;
+        feedSeen.add(feed.feed_id);
+        normalized.push(/** @type {RecordingFeed} */ ({ feed_id: feed.feed_id, state: feed.state,
+          written_frames: feed.written_frames, backpressure: feed.backpressure }));
+      }
+      data.recordings.push(/** @type {RecordingRow} */ ({ capsule_id: row.capsule_id, state: row.state, feeds: normalized }));
+    }
+    data.valid = true;
+  } catch { /* Closed refusal; never expose a caller exception. */ }
+  if (!data.valid) { data.recordings = []; data.pendingIds = []; }
+  return result;
+}
+/** Compare only our normalized value objects, never caller objects.
+ * @param {unknown} a @param {unknown} b @returns {boolean} */
+function same(a, b) {
+  if (Object.is(a, b)) return true;
+  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
+  const keys = Object.keys(a);
+  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) &&
+    same(/** @type {Record<string,unknown>} */ (a)[key], /** @type {Record<string,unknown>} */ (b)[key]));
+}
+
+let alive = true;
+/** @type {import('vue').ShallowRef<Snapshot|null>} */ const snapshot = shallowRef(null);
+/** @type {import('vue').ShallowRef<Proposal|null>} */ const proposal = shallowRef(null);
+/** These transient click guards do not own or manage any host session. */
+const dispatched = reactive(new Map());
+const choosing = reactive(new Set());
+const folderPending = shallowRef(false);
+const retired = new Set();
+/** @type {Map<string,{owner:string,role:string,generation:string}>} */ const seen = new Map();
+/** @type {Set<string>} */ let previousIds = new Set();
+
+/** @param {Data} data */
+function observeIdentities(data) {
+  if (!data.valid || data.availability !== 'available') return;
+  const current = new Set();
+  for (const row of data.rows) {
+    const identities = [{ id: row.capsule_id, owner: row.capsule_id, role: 'capsule', generation: row.generation },
+      ...row.feeds.map(feed => ({ id: feed.feed_id, owner: row.capsule_id, role: feed.role, generation: feed.generation }))];
+    for (const identity of identities) {
+      current.add(identity.id);
+      const old = seen.get(identity.id);
+      if (old && (old.owner !== identity.owner || old.role !== identity.role ||
+          identity.generation.length < old.generation.length ||
+          identity.generation.length === old.generation.length && identity.generation < old.generation)) retired.add(identity.id);
+      if (!retired.has(identity.id)) seen.set(identity.id, identity);
+    }
+  }
+  for (const id of previousIds) if (!current.has(id)) retired.add(id);
+  previousIds = current;
+}
+/** Recheck even in the interval between a prop mutation and the next render.
+ * @returns {Snapshot} */
+function refresh() {
+  const next = readSnapshot(), old = snapshot.value;
+  if (old && old.sources.every((value, i) => Object.is(value, next.sources[i])) &&
+      old.listeners.every((value, i) => value === next.listeners[i]) && same(old.data, next.data)) return old;
+  proposal.value = null;
+  choosing.clear(); folderPending.value = false;
+  observeIdentities(next.data);
+  if (next.data.valid && next.data.availability === 'available') {
+    for (const [id, acknowledged] of dispatched) {
+      if (next.data.pendingIds.includes(id)) dispatched.set(id, true);
+      else if (acknowledged) dispatched.delete(id);
+    }
+  }
+  snapshot.value = next;
+  return next;
+}
+refresh();
+onBeforeUpdate(refresh);
+onBeforeUnmount(() => { alive = false; proposal.value = null; });
+
+/** @param {Snapshot} value @returns {string} */
+function unavailableReason(value) {
+  const data = value.data;
+  if (!alive || !data.valid || !data.bindings || data.availability !== 'available') return UNAVAILABLE;
+  return data.platform === 'windows' ? '' : 'Capture needs Windows';
+}
+/** @param {CapsuleRow} row @param {Snapshot} value @returns {CardView} */
+function cardView(row, value) {
+  const data = value.data;
+  const pending = data.pendingIds.includes(row.capsule_id) || dispatched.has(row.capsule_id) || choosing.has(row.capsule_id);
+  const exact = data.rows.some(current => current === row) && !retired.has(row.capsule_id) &&
+    row.feeds.every(feed => !retired.has(feed.feed_id));
+  const enabled = unavailableReason(value) === '' && exact && !pending;
+  const recording = data.recordings.find(entry => entry.capsule_id === row.capsule_id);
+  const role = row.feeds.find(feed => feed.role === 'game')?.role ?? row.feeds[0]?.role ?? null;
+  return { capsule_id: row.capsule_id, slot: row.slot, role, pending,
+    canRecord: enabled && role !== null && data.folderConfigured === true && data.ffmpegConfigured === true && !recording,
+    canStop: enabled && recording?.state === 'Running',
+    canSendToObs: enabled && role !== null && data.obsReady === true,
+    canChooseWindow: enabled };
+}
+/** @param {CapsuleRow} row @param {Snapshot} origin @param {Action} action */
+function allowed(row, origin, action) {
+  if (!alive || refresh() !== origin) return false;
+  const view = cardView(row, origin);
+  return action === 'record' ? view.canRecord : action === 'stop' ? view.canStop : view.canSendToObs;
+}
+/** @param {CapsuleRow} row @param {Snapshot} origin @param {Action} action */
+function propose(row, origin, action) {
+  if (!allowed(row, origin, action)) return;
+  /** @type {Proposal} */
+  const next = { snapshot: origin, row, action,
+    confirm: () => {
+      if (proposal.value !== next || !allowed(row, origin, action) || proposal.value !== next) return;
+      proposal.value = null;
+      dispatched.set(row.capsule_id, false); // Set before emit, including synchronous reentry.
+      emit(action, { capsule_id: row.capsule_id });
+    },
+    reject: () => { if (proposal.value === next) proposal.value = null; } };
+  proposal.value = next;
+}
+/** @param {CapsuleRow} row @param {Snapshot} origin @param {'game'|'camera'} role */
+function chooseWindow(row, origin, role) {
+  if (!alive || refresh() !== origin || !cardView(row, origin).canChooseWindow ||
+      (role !== 'game' && role !== 'camera')) return;
+  proposal.value = null; choosing.add(row.capsule_id);
+  emit('choose-window', { capsule_id: row.capsule_id, role });
+}
+const cards = computed(() => {
+  const origin = /** @type {Snapshot} */ (snapshot.value);
+  return origin.data.rows.map(row => ({ row, view: cardView(row, origin),
+    recording: origin.data.valid ? origin.data.recordings.find(entry => entry.capsule_id === row.capsule_id) : undefined,
+    feeds: (/** @type {Array<'game'|'camera'>} */ (['game', 'camera'])).map(role => ({ role,
+      feed: row.feeds.find(feed => feed.role === role), choose: () => chooseWindow(row, origin, role) })),
+    record: () => propose(row, origin, 'record'), stop: () => propose(row, origin, 'stop'), obs: () => propose(row, origin, 'obs') }));
+});
+const reason = computed(() => unavailableReason(/** @type {Snapshot} */ (snapshot.value)));
+const folder = computed(() => {
+  const origin = /** @type {Snapshot} */ (snapshot.value);
+  return { disabled: reason.value !== '' || folderPending.value,
+    choose: () => {
+      if (!alive || refresh() !== origin || unavailableReason(origin) !== '' || folderPending.value) return;
+      proposal.value = null; folderPending.value = true;
+      emit('choose-folder');
+    } };
+});
+</script>
+
+<template>
+  <section class="capsule-workspace" aria-label="Capsule workspace">
+    <header class="capsule-workspace__header">
+      <h2>Capsules</h2>
+      <button type="button" :disabled="folder.disabled" v-on="{ click: folder.choose }">Choose folder</button>
+    </header>
+    <p v-if="reason" class="capsule-workspace__notice" role="status">{{ reason }}</p>
+    <p v-else-if="cards.length === 0" role="status">No capsules in this session.</p>
+    <article v-for="card in cards" :key="card.row.capsule_id" :data-capsule-id="card.row.capsule_id"
+      class="capsule-workspace__card" aria-label="Capsule">
+      <header class="capsule-workspace__header">
+        <h3>Capsule · {{ card.row.slot ?? 'Slot unknown' }}</h3>
+        <span class="capsule-workspace__identity">{{ card.row.capsule_id }}</span>
+      </header>
+      <p v-if="card.row.person_label !== null">{{ card.row.person_label }}</p>
+      <div class="capsule-workspace__feeds">
+        <section v-for="item in card.feeds" :key="item.role" class="capsule-workspace__feed">
+          <h4>{{ item.role === 'game' ? 'Game feed' : 'Camera feed' }}</h4>
+          <p v-if="item.feed" class="capsule-workspace__identity">{{ item.feed.feed_id }} · generation {{ item.feed.generation }}</p>
+          <p v-else>No {{ item.role }} feed paired.</p>
+          <p class="capsule-workspace__absence">No approved preview source.</p>
+          <button type="button" :disabled="!card.view.canChooseWindow" v-on="{ click: item.choose }">Choose {{ item.role }} window</button>
+        </section>
+      </div>
+      <p v-if="card.recording">Recording state: {{ card.recording.state ?? 'Unknown' }}</p>
+      <p v-else>{{ reason ? 'Recording state unknown.' : 'No current native recording.' }}</p>
+      <ul v-if="card.recording">
+        <li v-for="feed in card.recording.feeds" :key="feed.feed_id">
+          {{ feed.feed_id }} · {{ feed.state ?? 'Unknown' }} · frames {{ feed.written_frames ?? 'Unknown' }} · backpressure {{ feed.backpressure ?? 'Unknown' }}
+        </li>
+      </ul>
+      <p v-if="card.view.pending" role="status">Request pending.</p>
+      <div class="capsule-workspace__actions">
+        <button type="button" :disabled="!card.view.canRecord" v-on="{ click: card.record }">REC</button>
+        <button type="button" :disabled="!card.view.canStop" v-on="{ click: card.stop }">Stop</button>
+        <button type="button" :disabled="!card.view.canSendToObs" v-on="{ click: card.obs }">OBS</button>
+      </div>
+    </article>
+    <section v-if="proposal" class="capsule-workspace__confirmation" role="dialog" aria-label="Confirm capsule action" aria-modal="false">
+      <h3>{{ proposal.action === 'record' ? 'Confirm recording' : proposal.action === 'stop' ? 'Confirm stop' : 'Confirm send to OBS' }}</h3>
+      <p>Capsule {{ proposal.row.capsule_id }}</p>
+      <p>{{ proposal.action === 'record' ? 'Request recording of the paired feeds?' : proposal.action === 'stop' ? 'Request stopping the current recording?' : 'Request sending this capsule to OBS?' }}</p>
+      <div class="capsule-workspace__actions">
+        <!-- Pass the captured functions themselves; no cached late-bound wrapper. -->
+        <button type="button" v-on="{ click: proposal.confirm }">Confirm</button>
+        <button type="button" v-on="{ click: proposal.reject }">Reject</button>
+      </div>
+    </section>
+  </section>
+</template>
+
+<style src="./capsule_workspace.css"></style>
diff --git a/ui/src/pro_bridge/capsule_workspace.js b/ui/src/pro_bridge/capsule_workspace.js
new file mode 100644
index 0000000..4b62969
--- /dev/null
+++ b/ui/src/pro_bridge/capsule_workspace.js
@@ -0,0 +1,109 @@
+// @ts-check
+/**
+ * Closed presentation rows. Identities are opaque and clocks stay decimal
+ * strings. Slots are caller-owned session aliases, never authorization keys.
+ * The parent must supply the current session and authoritative generations;
+ * this validator neither constructs identities nor reads a host bridge.
+ * @typedef {{feed_id:string, role:'game'|'camera', generation:string}} CapsuleFeed
+ * @typedef {{capsule_id:string, slot:string|null, person_label:string|null,
+ *   generation:string, feeds:CapsuleFeed[]}} CapsuleRow
+ */
+
+const MAX_CLOCK = '18446744073709551615';
+/** @param {unknown} value @returns {value is string} */
+function identifier(value) {
+  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value);
+}
+/** @param {unknown} value @returns {value is string} */
+function clock(value) {
+  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) &&
+    (value.length < MAX_CLOCK.length || value <= MAX_CLOCK);
+}
+/** Read only own, enumerable data fields; accessors never get invoked.
+ * @param {unknown} value @param {string[]} keys
+ * @returns {Record<string, unknown>|null} */
+function fields(value, keys) {
+  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
+  const prototype = Object.getPrototypeOf(value);
+  if (prototype !== Object.prototype && prototype !== null) return null;
+  if (Reflect.ownKeys(value).length !== keys.length) return null;
+  /** @type {Record<string, unknown>} */
+  const result = Object.create(null);
+  for (const key of keys) {
+    const field = Object.getOwnPropertyDescriptor(value, key);
+    if (!field || !field.enumerable || !Object.hasOwn(field, 'value')) return null;
+    result[key] = field.value;
+  }
+  return result;
+}
+/** Reject sparse arrays, extra properties, subclasses and index accessors.
+ * @param {unknown} value @param {number} maximum @returns {unknown[]|null} */
+function rows(value, maximum) {
+  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
+  const length = Object.getOwnPropertyDescriptor(value, 'length')?.value;
+  if (!Number.isInteger(length) || length < 0 || length > maximum ||
+      Reflect.ownKeys(value).length !== length + 1) return null;
+  /** @type {unknown[]} */
+  const result = [];
+  for (let index = 0; index < length; index += 1) {
+    const entry = Object.getOwnPropertyDescriptor(value, String(index));
+    if (!entry || !entry.enumerable || !Object.hasOwn(entry, 'value')) return null;
+    result.push(entry.value);
+  }
+  return result;
+}
+
+/**
+ * Validate the entire capsules input, preserving the existing controller's
+ * two-capsule/two-feed bounds. Invalid input refuses the whole snapshot, never
+ * a partial success. The returned rows are detached, closed value objects.
+ * No caller getter, exception text, identifier prefix or array position is
+ * used to infer identity. Session tombstones belong to the parent session.
+ * @param {unknown} value
+ * @returns {{ok:boolean, rows:CapsuleRow[]}}
+ */
+export function validateCapsules(value) {
+  /** @returns {{ok:false, rows:CapsuleRow[]}} */
+  const refuse = () => ({ ok: false, rows: [] });
+  try {
+    const input = rows(value, 2);
+    if (input === null) return refuse();
+    const identities = new Set();
+    const slots = new Set();
+    /** @type {CapsuleRow[]} */
+    const normalized = [];
+    for (const candidate of input) {
+      const row = fields(candidate, ['capsule_id', 'slot', 'person_label', 'generation', 'feeds']);
+      if (!row || !identifier(row.capsule_id) || identities.has(row.capsule_id) ||
+          !clock(row.generation) ||
+          !(row.person_label === null || typeof row.person_label === 'string') ||
+          !(row.slot === null || typeof row.slot === 'string' && /^slot-[0-9]+$/.test(row.slot))) {
+        return refuse();
+      }
+      identities.add(row.capsule_id);
+      if (row.slot !== null) {
+        if (slots.has(row.slot)) return refuse();
+        slots.add(row.slot);
+      }
+      const inputFeeds = rows(row.feeds, 2);
+      if (inputFeeds === null) return refuse();
+      const roles = new Set();
+      /** @type {CapsuleFeed[]} */
+      const feeds = [];
+      for (const candidateFeed of inputFeeds) {
+        const feed = fields(candidateFeed, ['feed_id', 'role', 'generation']);
+        if (!feed || !identifier(feed.feed_id) || identities.has(feed.feed_id) ||
+            (feed.role !== 'game' && feed.role !== 'camera') || roles.has(feed.role) ||
+            !clock(feed.generation)) return refuse();
+        identities.add(feed.feed_id);
+        roles.add(feed.role);
+        feeds.push({ feed_id: feed.feed_id, role: feed.role, generation: feed.generation });
+      }
+      normalized.push({ capsule_id: row.capsule_id, slot: row.slot,
+        person_label: row.person_label, generation: row.generation, feeds });
+    }
+    return { ok: true, rows: normalized };
+  } catch {
+    return refuse();
+  }
+}
diff --git a/ui/src/pro_bridge/capsule_workspace.css b/ui/src/pro_bridge/capsule_workspace.css
new file mode 100644
index 0000000..600bc9b
--- /dev/null
+++ b/ui/src/pro_bridge/capsule_workspace.css
@@ -0,0 +1,68 @@
+/* Uses only the existing parent token adapter; both themes flow through it. */
+.capsule-workspace {
+  display: grid;
+  gap: var(--space-4);
+  padding: var(--space-4);
+  min-width: 0;
+  color: var(--fg);
+  background: var(--bg);
+  font-family: var(--font-body);
+  font-size: var(--text-sm);
+  line-height: var(--leading-body);
+}
+.capsule-workspace h2,
+.capsule-workspace h3,
+.capsule-workspace h4,
+.capsule-workspace p { margin: 0; overflow-wrap: anywhere; }
+.capsule-workspace h2 { font-size: var(--text-xl); }
+.capsule-workspace h3 { font-size: var(--text-lg); }
+.capsule-workspace h4 { font-size: var(--text-sm); }
+.capsule-workspace__header,
+.capsule-workspace__actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
+.capsule-workspace__header { justify-content: space-between; }
+.capsule-workspace__card,
+.capsule-workspace__confirmation {
+  display: grid;
+  gap: var(--space-3);
+  padding: var(--space-4);
+  border: var(--cafe-structure-semantic-border-width) solid var(--border);
+  border-radius: var(--radius-lg);
+  background: var(--surface);
+}
+.capsule-workspace__feeds { display: flex; flex-wrap: wrap; gap: var(--space-3); }
+.capsule-workspace__feed {
+  display: grid;
+  align-content: start;
+  flex: 1 1 var(--cafe-structure-component-app-shell-palette-width);
+  min-width: 0;
+  gap: var(--space-2);
+  padding: var(--space-3);
+  border: var(--cafe-structure-semantic-border-width) solid var(--border-soft);
+  border-radius: var(--radius-md);
+}
+.capsule-workspace__notice,
+.capsule-workspace__identity,
+.capsule-workspace__absence { color: var(--muted); }
+.capsule-workspace__identity { font-family: var(--font-mono); font-size: var(--text-xs); }
+.capsule-workspace__absence { padding: var(--space-4) 0; }
+.capsule-workspace button {
+  font: inherit;
+  color: var(--accent-on);
+  background: var(--accent);
+  border: var(--cafe-structure-semantic-border-width) solid var(--accent);
+  border-radius: var(--radius-md);
+  padding: var(--space-2) var(--space-4);
+  cursor: pointer;
+}
+.capsule-workspace button:hover:enabled { background: var(--accent-hover); border-color: var(--accent-hover); }
+.capsule-workspace button:focus-visible {
+  outline: var(--cafe-structure-semantic-focus-ring-width) solid var(--accent);
+  outline-offset: var(--space-1);
+}
+.capsule-workspace button:disabled {
+  background: var(--surface-warm);
+  border-color: var(--border);
+  color: var(--meta);
+  cursor: not-allowed;
+}
+.capsule-workspace__confirmation { border-color: var(--accent); }
diff --git a/tests/security/pro_capsule_workspace.test.js b/tests/security/pro_capsule_workspace.test.js
new file mode 100644
index 0000000..fca4406
--- /dev/null
+++ b/tests/security/pro_capsule_workspace.test.js
@@ -0,0 +1,423 @@
+'use strict';
+// Presentation tests only: synthetic native rows prove rendered text and emitted
+// intents, never native capture, recording, OBS, approval or Windows execution.
+const test = require('node:test');
+const assert = require('node:assert/strict');
+const fs = require('node:fs');
+const os = require('node:os');
+const path = require('node:path');
+const { pathToFileURL } = require('node:url');
+
+const ROOT = path.resolve(__dirname, '../..');
+const COMPONENT = path.join(ROOT, 'ui/src/pro_bridge/CapsuleWorkspace.vue');
+const MODEL = path.join(ROOT, 'ui/src/pro_bridge/capsule_workspace.js');
+const EVENTS = ['choose-window', 'record', 'stop', 'obs', 'choose-folder'];
+const LISTENERS = ['onChooseWindow', 'onRecord', 'onStop', 'onObs', 'onChooseFolder'];
+const PROPS = ['capsules', 'availability', 'platform', 'folderConfigured',
+  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds'];
+const clone = value => structuredClone(value);
+const capsule = (suffix = '1') => ({ capsule_id: 'cap-demo-' + suffix,
+  slot: 'slot-' + suffix, person_label: 'Synthetic user ' + suffix, generation: '1',
+  feeds: [{ feed_id: 'game-demo-' + suffix, role: 'game', generation: '1' }] });
+const snapshot = () => ({ capsules: [capsule()], availability: 'available', platform: 'windows',
+  folderConfigured: true, ffmpegConfigured: true, obsReady: true, recordings: [], pendingIds: [] });
+const running = () => [{ capsule_id: 'cap-demo-1', state: 'Running', feeds: [
+  { feed_id: 'game-demo-1', state: 'Running', written_frames: null, backpressure: '4' },
+] }];
+
+let modelPromise;
+function model() {
+  // No package installation or module-manifest change is needed for pure tests.
+  modelPromise ??= import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(MODEL, 'utf8')).toString('base64'));
+  return modelPromise;
+}
+
+test('validator normalizes the complete closed input without aliasing caller rows', async () => {
+  const api = await model();
+  assert.deepEqual(Object.keys(api), ['validateCapsules']);
+  const input = [capsule(), capsule('2')];
+  input[0].slot = 'slot-0'; input[1].slot = 'slot-01'; input[1].person_label = null;
+  const result = api.validateCapsules(input);
+  assert.equal(result.ok, true); assert.deepEqual(result.rows, input);
+  assert.notEqual(result.rows, input); assert.notEqual(result.rows[0], input[0]);
+  assert.notEqual(result.rows[0].feeds[0], input[0].feeds[0]);
+  assert.deepEqual(api.validateCapsules([]), { ok: true, rows: [] });
+});
+
+test('validator keeps canonical u64 clocks exact and rejects coercion', async () => {
+  const { validateCapsules } = await model();
+  for (const value of ['0', '1', '9007199254740993', '18446744073709551615']) {
+    const rows = [capsule()]; rows[0].generation = value; rows[0].feeds[0].generation = value;
+    const result = validateCapsules(rows);
+    assert.equal(result.ok, true); assert.equal(result.rows[0].generation, value);
+    assert.equal(result.rows[0].feeds[0].generation, value);
+  }
+  for (const value of ['', '01', '1.0', '1e1', '-1', ' 1', '1 ', '18446744073709551616',
+    '99999999999999999999', '100000000000000000000', null, 1, true, 1n]) {
+    for (const target of ['capsule', 'feed']) {
+      const rows = [capsule()];
+      (target === 'capsule' ? rows[0] : rows[0].feeds[0]).generation = value;
+      assert.deepEqual(validateCapsules(rows), { ok: false, rows: [] });
+    }
+  }
+});
+
+test('validator rejects missing fields, extras, wrong types and identifier ambiguity', async () => {
+  const { validateCapsules } = await model();
+  const invalid = [null, undefined, {}, true, [capsule(), capsule(), capsule('3')]];
+  for (const key of Object.keys(capsule())) {
+    const row = capsule(); delete row[key]; invalid.push([row]);
+  }
+  for (const key of Object.keys(capsule().feeds[0])) {
+    const row = capsule(); delete row.feeds[0][key]; invalid.push([row]);
+  }
+  const mutate = edit => { const rows = [capsule(), capsule('2')]; edit(rows); invalid.push(rows); };
+  for (const id of ['', '-bad', 'bad id', 'a'.repeat(97), true, null]) mutate(r => { r[0].capsule_id = id; });
+  mutate(r => { r[0].extra = true; }); mutate(r => { r[0].feeds[0].extra = true; });
+  mutate(r => { r[0].person_label = 1; }); mutate(r => { r[0].slot = 'other-session'; });
+  mutate(r => { r[0].feeds = null; }); mutate(r => { r[0].feeds[0].role = 'screen'; });
+  mutate(r => { r[1].capsule_id = r[0].capsule_id; });
+  mutate(r => { r[1].feeds[0].feed_id = r[0].feeds[0].feed_id; });
+  mutate(r => { r[0].feeds[0].feed_id = r[1].capsule_id; });
+  mutate(r => { r[1].slot = r[0].slot; });
+  mutate(r => { r[0].feeds.push({ feed_id: 'game-demo-extra', role: 'game', generation: '1' }); });
+  for (const input of invalid) assert.deepEqual(validateCapsules(input), { ok: false, rows: [] });
+  const exact = capsule(); exact.capsule_id = 'a'.repeat(96);
+  assert.equal(validateCapsules([exact]).ok, true);
+});
+
+test('validator refuses accessors, sparse arrays and hidden or symbol fields', async () => {
+  const { validateCapsules } = await model();
+  let reads = 0;
+  const getter = capsule(); Object.defineProperty(getter, 'capsule_id', { enumerable: true,
+    get() { reads += 1; throw new Error('synthetic-refusal'); } });
+  const symbol = capsule(); symbol[Symbol('synthetic')] = true;
+  const hidden = capsule(); Object.defineProperty(hidden, 'extra', { value: true });
+  const array = [capsule()]; array.extra = true;
+  for (const input of [[getter], [symbol], [hidden], array, Array(1)]) {
+    assert.deepEqual(validateCapsules(input), { ok: false, rows: [] });
+  }
+  assert.equal(reads, 0, 'validation must not execute property getters');
+});
+
+let fixtureRoot, uiPromise;
+async function loadUi() {
+  if (uiPromise) return uiPromise;
+  uiPromise = (async () => {
+    // Lazy loading deliberately leaves missing approved dependencies as a real
+    // test failure; selecting validator tests never silently skips component tests.
+    const { parse, compileScript } = require('vue/compiler-sfc');
+    const source = fs.readFileSync(COMPONENT, 'utf8');
+    const { descriptor, errors } = parse(source, { filename: COMPONENT });
+    assert.deepEqual(errors, []);
+    assert.equal(descriptor.scriptSetup?.lang, undefined);
+    const compiled = compileScript(descriptor, { id: 'pro-capsule-workspace', inlineTemplate: true,
+      templateOptions: { compilerOptions: { hoistStatic: false } } });
+    const root = fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-pro-capsule-'));
+    const modules = path.dirname(path.dirname(require.resolve('vue/package.json')));
+    fs.symlinkSync(modules, path.join(root, 'node_modules'), 'junction');
+    const code = compiled.content.replace(/(['"])\.\/capsule_workspace\.js\1/g, "'./capsule_workspace.mjs'");
+    assert.notEqual(code, compiled.content, 'real validator import must be preserved');
+    fs.writeFileSync(path.join(root, 'CapsuleWorkspace.mjs'), code);
+    fs.writeFileSync(path.join(root, 'capsule_workspace.mjs'), fs.readFileSync(MODEL, 'utf8'));
+    fs.writeFileSync(path.join(root, 'runtime.mjs'), "export { createRenderer, h, nextTick } from 'vue';\n");
+    const [component, runtime] = await Promise.all(['CapsuleWorkspace.mjs', 'runtime.mjs']
+      .map(file => import(pathToFileURL(path.join(root, file)).href)));
+    return { Component: component.default, descriptor, ...runtime };
+  })();
+  return uiPromise;
+}
+test.after(() => { if (fixtureRoot) fs.rmSync(fixtureRoot, { recursive: true, force: true }); });
+
+function element(tag) { return { kind: 'element', tag, parent: null, children: [], props: {} }; }
+const nodeOps = {
+  createElement: element,
+  createText: text => ({ kind: 'text', text, parent: null }),
+  createComment: text => ({ kind: 'comment', text, parent: null }),
+  setText(node, text) { node.text = text; },
+  setElementText(node, text) {
+    for (const child of node.children) child.parent = null;
+    node.children = text === '' ? [] : [{ kind: 'text', text, parent: node }];
+  },
+  insert(child, parent, anchor) {
+    if (child.parent) nodeOps.remove(child);
+    const index = anchor ? parent.children.indexOf(anchor) : -1;
+    parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent;
+  },
+  remove(child) {
+    if (!child.parent) return;
+    const siblings = child.parent.children, index = siblings.indexOf(child);
+    if (index >= 0) siblings.splice(index, 1); child.parent = null;
+  },
+  parentNode: node => node.parent,
+  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null; },
+  patchProp(node, key, _old, value) { node.props[key] = value; },
+  querySelector: () => null,
+};
+function textOf(node) {
+  return node.kind === 'text' ? node.text : node.kind === 'element' ? node.children.map(textOf).join(' ') : '';
+}
+function findAll(node, predicate, out = []) {
+  if (node.kind === 'element') {
+    if (predicate(node)) out.push(node);
+    for (const child of node.children) findAll(child, predicate, out);
+  }
+  return out;
+}
+function button(tree, label, id) {
+  const scope = id ? findAll(tree, n => n.tag === 'article' && n.props['data-capsule-id'] === id)[0] : tree;
+  assert.ok(scope, 'synthetic card must exist');
+  return findAll(scope, n => n.tag === 'button' && textOf(n).replace(/\s+/g, ' ').trim() === label)[0];
+}
+async function mount(initial = snapshot(), listenerOverrides = {}) {
+  const ui = await loadUi(), tree = element('root'), events = [];
+  const renderer = ui.createRenderer(nodeOps);
+  const listeners = Object.fromEntries(LISTENERS.map((key, index) => [key, (...args) => {
+    events.push([EVENTS[index], args]);
+  }]));
+  Object.assign(listeners, listenerOverrides);
+  let vnode;
+  const render = props => { vnode = ui.h(ui.Component, { ...props, ...listeners }); renderer.render(vnode, tree); };
+  render(initial); await ui.nextTick();
+  const click = async (label, id) => {
+    const target = button(tree, label, id); assert.ok(target, 'expected control: ' + label);
+    assert.notEqual(target.props.disabled, true, 'control should be enabled: ' + label);
+    target.props.onClick(); await ui.nextTick();
+  };
+  return { ui, tree, events, click, listeners, tamper: change => change(vnode.props),
+    update: async props => { render(props); await ui.nextTick(); },
+    done: () => renderer.render(null, tree) };
+}
+
+test('component exposes only the frozen props and events and renders truthful cards', async () => {
+  const props = snapshot(); props.capsules.push(capsule('2')); props.recordings = running();
+  const m = await mount(props);
+  try {
+    const keys = value => Array.isArray(value) ? [...value] : Object.keys(value);
+    assert.deepEqual(keys(m.ui.Component.props).sort(), [...PROPS].sort());
+    assert.deepEqual(keys(m.ui.Component.emits).sort(), [...EVENTS].sort());
+    assert.equal(findAll(m.tree, n => n.tag === 'article').length, 2);
+    const text = textOf(m.tree);
+    for (const value of ['slot-1', 'slot-2', 'Synthetic user 1', 'Synthetic user 2', 'game-demo-1', 'Running', '4']) {
+      assert.ok(text.includes(value), 'native presentation value missing');
+    }
+    assert.match(text, /unknown/i); assert.match(text, /preview.*unavailable|no.*preview/i);
+    assert.equal(findAll(m.tree, n => ['video', 'img', 'canvas', 'iframe'].includes(n.tag)).length, 0);
+    assert.equal(m.events.length, 0);
+  } finally { m.done(); }
+});
+
+for (const role of ['game', 'camera']) test('component emits exact choose-window role ' + role, async () => {
+  const m = await mount();
+  try {
+    await m.click('Choose ' + role + ' window');
+    assert.deepEqual(m.events, [['choose-window', [{ capsule_id: 'cap-demo-1', role }]]]);
+  } finally { m.done(); }
+});
+
+test('component emits choose-folder with no payload', async () => {
+  const m = await mount();
+  try { await m.click('Choose folder'); assert.deepEqual(m.events, [['choose-folder', []]]); }
+  finally { m.done(); }
+});
+
+for (const label of ['Choose folder', 'Choose camera window']) {
+  test('component immediate double click emits one intent: ' + label, async () => {
+    const m = await mount();
+    try {
+      const click = button(m.tree, label).props.onClick; click(); click(); await m.ui.nextTick();
+      assert.equal(m.events.length, 1);
+      const next = snapshot(); await m.update(next); await m.click(label); assert.equal(m.events.length, 2);
+    } finally { m.done(); }
+  });
+}
+
+for (const [label, name] of [['REC', 'record'], ['Stop', 'stop'], ['OBS', 'obs']]) {
+  test('component ' + name + ' requires explicit Confirm and emits exactly once', async () => {
+    const props = snapshot(); if (name === 'stop') props.recordings = running();
+    const m = await mount(props);
+    try {
+      await m.click(label); assert.equal(m.events.length, 0);
+      assert.ok(button(m.tree, 'Reject'));
+      const confirm = button(m.tree, 'Confirm').props.onClick;
+      confirm(); confirm(); await m.ui.nextTick();
+      assert.deepEqual(m.events, [[name, [{ capsule_id: 'cap-demo-1' }]]]);
+    } finally { m.done(); }
+  });
+}
+
+test('component Reject and an obsolete replaced dialog handler emit nothing', async () => {
+  const m = await mount();
+  try {
+    await m.click('REC'); const obsolete = button(m.tree, 'Confirm').props.onClick;
+    await m.click('Reject'); await m.click('OBS');
+    obsolete(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    await m.click('Confirm'); assert.deepEqual(m.events, [['obs', [{ capsule_id: 'cap-demo-1' }]]]);
+  } finally { m.done(); }
+});
+
+test('component replacing a live dialog invalidates its earlier Confirm callback', async () => {
+  const m = await mount();
+  try {
+    await m.click('REC'); const obsolete = button(m.tree, 'Confirm').props.onClick;
+    await m.click('OBS'); obsolete(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    await m.click('Confirm'); assert.deepEqual(m.events, [['obs', [{ capsule_id: 'cap-demo-1' }]]]);
+  } finally { m.done(); }
+});
+
+test('component waits for the native pending receipt cycle before another effect', async () => {
+  const m = await mount();
+  try {
+    await m.click('REC'); await m.click('Confirm');
+    const next = snapshot(); next.recordings = running(); await m.update(next);
+    assert.equal(button(m.tree, 'Stop').props.disabled, true, 'publication alone cannot acknowledge an effect');
+    const pending = clone(next); pending.pendingIds = ['cap-demo-1']; await m.update(pending);
+    assert.equal(button(m.tree, 'Stop').props.disabled, true);
+    await m.update(clone(next)); await m.click('Stop'); await m.click('Confirm');
+    assert.deepEqual(m.events.map(row => row[0]), ['record', 'stop']);
+  } finally { m.done(); }
+});
+
+test('component Confirm rereads raw config and native counters after in-place tampering', async () => {
+  for (const [label, change] of [['REC', p => { p.folderConfigured = false; }],
+    ['Stop', p => { p.recordings[0].feeds[0].written_frames = '01'; }]]) {
+    const props = snapshot(); if (label === 'Stop') props.recordings = running();
+    const m = await mount(props);
+    try {
+      await m.click(label); const confirm = button(m.tree, 'Confirm').props.onClick;
+      m.tamper(change); confirm(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    } finally { m.done(); }
+  }
+});
+
+test('component pending receipt disables every capsule effect and forced handlers refuse', async () => {
+  const props = snapshot(); props.pendingIds = ['cap-demo-1'];
+  const m = await mount(props);
+  try {
+    for (const label of ['REC', 'Stop', 'OBS', 'Choose game window', 'Choose camera window']) {
+      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
+    }
+    await m.ui.nextTick(); assert.equal(m.events.length, 0); assert.equal(button(m.tree, 'Confirm'), undefined);
+  } finally { m.done(); }
+});
+
+test('component unavailable and unknown runtime show one shared reason, not empty capsules', async () => {
+  for (const availability of ['unavailable', 'unknown', null]) {
+    const props = snapshot(); props.availability = availability;
+    const m = await mount(props);
+    try {
+      assert.match(textOf(m.tree), /Runtime unavailable/); assert.doesNotMatch(textOf(m.tree), /No capsules/);
+      for (const target of findAll(m.tree, n => n.tag === 'button')) {
+        assert.equal(target.props.disabled, true); target.props.onClick?.();
+      }
+      await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    } finally { m.done(); }
+  }
+});
+
+test('component missing store or listener bindings is unavailable without throwing', async () => {
+  for (const [props, overrides] of [[{}, {}], [snapshot(), { onRecord: undefined }]]) {
+    const m = await mount(props, overrides);
+    try {
+      assert.match(textOf(m.tree), /Runtime unavailable/);
+      for (const target of findAll(m.tree, n => n.tag === 'button')) target.props.onClick?.();
+      await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    } finally { m.done(); }
+  }
+});
+
+test('component refuses stale confirmation after runtime, identity, generation or state changes', async () => {
+  const changes = [p => { p.availability = 'unavailable'; }, p => { p.capsules = []; },
+    p => { p.capsules.push(clone(p.capsules[0])); }, p => { delete p.capsules[0].capsule_id; },
+    p => { p.capsules[0].generation = '2'; }, p => { p.capsules[0].feeds[0].generation = '2'; },
+    p => { p.capsules[0].feeds[0].role = 'camera'; },
+    p => { p.recordings = running(); }, p => { p.pendingIds = ['cap-demo-1']; },
+    p => { p.folderConfigured = null; }, p => { p.ffmpegConfigured = false; }];
+  for (const change of changes) {
+    const m = await mount();
+    try {
+      await m.click('REC'); const stale = button(m.tree, 'Confirm').props.onClick;
+      const next = snapshot(); change(next); await m.update(next);
+      stale(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    } finally { m.done(); }
+  }
+});
+
+test('component rejects saved render handlers and invalidates them on unmount', async () => {
+  const m = await mount();
+  const staleChoose = button(m.tree, 'Choose camera window').props.onClick;
+  try {
+    const next = snapshot(); next.capsules[0].generation = '2'; await m.update(next);
+    staleChoose(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    await m.click('REC'); const staleConfirm = button(m.tree, 'Confirm').props.onClick;
+    m.done(); staleConfirm(); staleChoose(); assert.equal(m.events.length, 0);
+  } finally { m.done(); }
+});
+
+test('component keeps exact prefixed identities and never reuses a removed identity', async () => {
+  const props = snapshot(); props.capsules[0].capsule_id = 'capsule-cap-demo-1';
+  const m = await mount(props);
+  try {
+    await m.click('Choose camera window');
+    assert.deepEqual(m.events, [['choose-window', [{ capsule_id: 'capsule-cap-demo-1', role: 'camera' }]]]);
+    const removed = snapshot(); removed.capsules = []; await m.update(removed);
+    await m.update(props);
+    const retired = findAll(m.tree, n => n.tag === 'article' && n.props['data-capsule-id'] === 'capsule-cap-demo-1')[0];
+    if (retired) for (const target of findAll(retired, n => n.tag === 'button')) target.props.onClick?.();
+    await m.ui.nextTick(); assert.equal(m.events.length, 1);
+  } finally { m.done(); }
+});
+
+test('component refuses a rolled-back generation even through current handlers', async () => {
+  const m = await mount();
+  try {
+    const newer = snapshot(); newer.capsules[0].generation = '2'; await m.update(newer);
+    await m.update(snapshot());
+    for (const label of ['REC', 'OBS', 'Choose camera window']) {
+      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
+    }
+    await m.ui.nextTick(); assert.equal(m.events.length, 0);
+  } finally { m.done(); }
+});
+
+test('component tombstones a removed feed and refuses its reappearance', async () => {
+  const m = await mount();
+  try {
+    const removed = snapshot(); removed.capsules[0].feeds = []; await m.update(removed);
+    await m.update(snapshot());
+    for (const label of ['REC', 'OBS', 'Choose game window']) {
+      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
+    }
+    await m.ui.nextTick(); assert.equal(m.events.length, 0);
+  } finally { m.done(); }
+});
+
+test('component start, stop and OBS gates keep null and unknown states closed', async () => {
+  for (const change of [p => { p.folderConfigured = null; }, p => { p.ffmpegConfigured = null; },
+    p => { p.platform = 'unknown'; }, p => { p.capsules[0].feeds = []; },
+    p => { p.recordings = [{ capsule_id: 'cap-demo-1', state: null, feeds: [] }]; }]) {
+    const props = snapshot(); change(props); const m = await mount(props);
+    try { assert.equal(button(m.tree, 'REC').props.disabled, true); assert.equal(m.events.length, 0); }
+    finally { m.done(); }
+  }
+  const props = snapshot(); props.obsReady = null; const m = await mount(props);
+  try {
+    assert.equal(button(m.tree, 'OBS').props.disabled, true); assert.equal(button(m.tree, 'Stop').props.disabled, true);
+  } finally { m.done(); }
+});
+
+test('component rejects malformed ancillary snapshots and noncanonical native counters', async () => {
+  for (const change of [p => { p.pendingIds = ['cap-demo-1', 'cap-demo-1']; },
+    p => { p.folderConfigured = 1; }, p => { p.availability = 'starting'; },
+    p => { p.recordings = [...running(), ...running()]; },
+    p => { p.recordings = running(); p.recordings[0].extra = true; },
+    p => { p.recordings = running(); p.recordings[0].feeds[0].written_frames = '01'; },
+    p => { p.recordings = running(); p.recordings[0].feeds[0].backpressure = true; }]) {
+    const props = snapshot(); change(props); const m = await mount(props);
+    try {
+      assert.match(textOf(m.tree), /Runtime unavailable/);
+      for (const target of findAll(m.tree, n => n.tag === 'button')) target.props.onClick?.();
+      await m.ui.nextTick(); assert.equal(m.events.length, 0);
+    } finally { m.done(); }
+  }
+});
```

## Assumptions

1. bindCapsuleWorkspace() supplies the eight frozen props and five named listeners, and republishes coherent immutable snapshots after binding changes; the host owns Dockview and the single selector.
2. The host supplies real generations and session-only slot aliases, rejects stale source revisions and identity reuse across remounts, and revalidates every presentation intent before existing native approval.
3. A complete native recordings list with no row permits REC; only the exact Running state permits Stop. Other native states remain disabled.
4. Accepted effects are rearmed only after pendingIds includes then excludes the capsule; selector/folder completion republishes the snapshot.
5. No approved preview source is provided by the closed interface. Tests prove presentation events only; native recording, OBS execution and live preview remain unverified.
