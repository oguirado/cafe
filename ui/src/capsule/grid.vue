<script setup>
// @ts-check
/**
 * @file Cafe desktop wall: dockable, labelled presentation for host-issued
 * capsule ids. The component never creates ids, capsules, counts, previews or
 * authority calls; unknown availability is never rendered as zero.
 */

import { computed, defineComponent, h, markRaw, onBeforeUnmount, useSlots, watch } from 'vue';
import { DockviewVue } from 'dockview-vue';
import 'dockview-vue/dist/styles/dockview.css';
import '../workspace.css';

/**
 * @typedef {object} CapsuleGridValidation
 * @property {boolean} ok
 * @property {string[]} ids
 */

const props = defineProps({
  /** Exact host-issued capsule ids, unique and already admitted by the parent. */
  capsuleIDs: {
    type: Array,
    required: true,
    validator: (value) => Array.isArray(value)
      && value.every((id) => typeof id === 'string'
        && id.length > 0
        && id === id.trim()
        && !/\s/.test(id)
        && !/^\d+$/.test(id))
      && new Set(value).size === value.length,
  },
  /** False means the authoritative capsule view is unavailable, not empty. */
  available: { type: Boolean, required: true },
});

/**
 * @param {unknown} value
 * @returns {boolean}
 */
function isExactCapsuleId(value) {
  return typeof value === 'string' && value.length > 0 && value === value.trim()
    && !/\s/.test(value) && !/^\d+$/.test(value);
}

/** @type {import('vue').ComputedRef<CapsuleGridValidation>} */
const validation = computed(() => {
  const ids = props.capsuleIDs;
  if (!Array.isArray(ids)) return { ok: false, ids: [] };
  const seen = new Set();
  for (const id of ids) {
    if (!isExactCapsuleId(id) || seen.has(id)) return { ok: false, ids: [] };
    seen.add(id);
  }
  return { ok: true, ids: [...ids] };
});

/** @type {import('vue').ComputedRef<'ready' | 'empty' | 'unavailable' | 'invalid'>} */
const gridState = computed(() => {
  if (props.available !== true) return 'unavailable';
  if (validation.value.ok !== true) return 'invalid';
  return validation.value.ids.length === 0 ? 'empty' : 'ready';
});

/** @type {import('vue').ComputedRef<string[]>} */
const capsules = computed(() => (gridState.value === 'ready' ? validation.value.ids : []));

const isUnavailable = computed(() => gridState.value === 'unavailable');
const isInvalid = computed(() => gridState.value === 'invalid');
const isEmpty = computed(() => gridState.value === 'empty');
const isReady = computed(() => gridState.value === 'ready');

const emit = defineEmits(['add-capsule']);
const slots = useSlots();
/** @type {import('dockview-core').DockviewApi | null} */
let desktopApi = null;
/** @type {{ dispose: () => void } | null} */
let removalSubscription = null;
let disposed = false;
let reconciling = false;
const panelId = (id) => `capsule:${id}`;

// Dockview's Vue renderer supplies { params: { params, api, containerApi } }.
// Content remains the parent's genuine capsule view; this wall owns no verbs.
const CapsulePanel = markRaw(defineComponent({
  props: { params: { type: Object, required: true } },
  setup(router) {
    return () => {
      const id = router.params.params?.capsuleId;
      if (!isReady.value || !capsules.value.includes(id)) return null;
      return h('div', { class: 'cafe-desktop-panel', 'data-capsule-id': id },
        slots.capsule ? slots.capsule({ id }) : [
          h('p', { class: 'cafe-capsule-grid-fallback' }, `No capsule view supplied. ${id}`),
        ]);
    };
  },
}));
const CapsuleTab = markRaw(defineComponent({
  props: { params: { type: Object, required: true } },
  setup(tab) {
    return () => h('span', {
      class: 'cafe-desktop-tab', title: tab.params.params?.capsuleId,
    }, tab.params.params?.capsuleId);
  },
}));
const panelComponents = markRaw({ CapsulePanel });
const tabComponents = markRaw({ CapsuleTab });

function reconcile() {
  if (!desktopApi || disposed || reconciling) return;
  reconciling = true;
  try {
    const wanted = new Set(capsules.value.map(panelId));
    for (const panel of [...desktopApi.panels]) {
      if (!wanted.has(panel.id)) desktopApi.removePanel(panel);
    }
    let previous = null;
    for (const id of capsules.value) {
      const exactPanelId = panelId(id);
      if (!desktopApi.getPanel(exactPanelId)) {
        desktopApi.addPanel({
          id: exactPanelId, component: 'CapsulePanel', tabComponent: 'CapsuleTab',
          title: id, params: { capsuleId: id }, renderer: 'always',
          minimumWidth: 240, minimumHeight: 200,
          ...(previous ? { position: { referencePanel: previous, direction: 'right' } } : {}),
        });
      }
      previous = exactPanelId;
    }
  } finally { reconciling = false; }
}

function onReady(event) {
  desktopApi = event.api;
  removalSubscription = desktopApi.onDidRemovePanel(() => {
    // A local close never removes a capsule. Restore from the authoritative ids.
    if (!reconciling) queueMicrotask(reconcile);
  });
  reconcile();
}
watch(capsules, reconcile);
onBeforeUnmount(() => {
  disposed = true;
  removalSubscription?.dispose();
  desktopApi = null;
});
</script>

<template>
  <section class='cafe-capsule-grid-region cafe-desktop' aria-label='Capsules' :data-state='gridState'>
    <p v-if='isUnavailable' class='cafe-capsule-grid-note' role='status'>
      Capsules unavailable.
    </p>
    <p v-else-if='isInvalid' class='cafe-capsule-grid-note' role='status'>
      Capsule list unavailable: expected exact, unique host-issued identifiers.
    </p>
    <div v-else-if='isEmpty' class='cafe-desktop-empty'>
      <p class='cafe-capsule-grid-note' role='status'>No capsules reported. Add a capsule to begin.</p>
      <button type='button' @click="emit('add-capsule')">Add capsule</button>
    </div>
    <DockviewVue
      v-show='isReady'
      class='cafe-desktop-wall'
      :components='panelComponents'
      :tab-components='tabComponents'
      :disable-floating-groups='true'
      @ready='onReady'
    />
  </section>
</template>

<style scoped>
.cafe-capsule-grid-region {
  min-width: 0;
  color: var(--fg);
  font-family: var(--font-body);
}

.cafe-capsule-grid-note {
  margin: 0;
  padding: var(--space-3, 12px) var(--space-4, 16px);
  border: var(--cafe-structure-semantic-border-width, 1px) dashed var(--border-soft, var(--border, currentColor));
  border-radius: var(--radius-sm, 4px);
  background: var(--surface);
  color: var(--muted);
  font-size: var(--text-sm, 0.875rem);
  line-height: var(--leading-body, 1.5);
}

.cafe-capsule-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 16rem), 1fr));
  gap: var(--space-3, 12px);
  margin: 0;
  padding: 0;
  list-style: none;
}

.cafe-capsule-grid-cell {
  display: grid;
  min-width: 0;
  align-content: start;
}

.cafe-capsule-grid-fallback {
  display: grid;
  gap: var(--space-2, 8px);
  align-content: start;
  min-width: 0;
  padding: var(--space-4, 16px);
  border: var(--cafe-structure-semantic-border-width, 1px) solid var(--border-soft, var(--border, currentColor));
  border-radius: var(--radius-sm, 4px);
  background: var(--surface);
}

.cafe-capsule-grid-fallback-title {
  color: var(--muted);
  font-size: var(--text-xs, 0.75rem);
}

.cafe-capsule-grid-fallback-id {
  min-width: 0;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm, 0.875rem);
  overflow-wrap: anywhere;
}

.cafe-capsule-grid-fallback-note {
  color: var(--meta);
  font-size: var(--text-xs, 0.75rem);
  line-height: var(--leading-body, 1.5);
}

@media (max-width: 30rem) {
  .cafe-capsule-grid {
    gap: var(--space-2, 8px);
  }
}
</style>
