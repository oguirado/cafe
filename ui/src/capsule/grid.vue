<script setup>
// @ts-check
/**
 * @file Cafe desktop wall: dockable, labelled presentation for host-issued
 * capsule ids. The component never creates ids, capsules, counts, previews or
 * authority calls; unknown availability is never rendered as zero.
 */

import { computed, defineComponent, h, markRaw, onBeforeUnmount, onMounted, ref, useSlots, watch } from 'vue';
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
  /** True while waiting for the first authoritative snapshot or while runtime is starting. */
  starting: { type: Boolean, default: false },
  /** Display-only tab labels keyed by capsuleId ("slot-N · person label"). */
  tabLabels: { type: Object, default: () => ({}) },
});

/**
 * @param {string} id
 * @returns {string}
 */
function tabTitleFor(id) {
  const labels = props.tabLabels;
  if (labels && typeof labels === 'object' && typeof labels[id] === 'string' && labels[id].trim().length > 0) {
    return labels[id];
  }
  return id;
}

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

/** @type {import('vue').ComputedRef<'starting' | 'ready' | 'empty' | 'unavailable' | 'invalid'>} */
const gridState = computed(() => {
  if (props.starting === true && props.available !== true) return 'starting';
  if (props.available !== true) return 'unavailable';
  if (validation.value.ok !== true) return 'invalid';
  return validation.value.ids.length === 0 ? 'empty' : 'ready';
});

/** @type {import('vue').ComputedRef<string[]>} */
const capsules = computed(() => (gridState.value === 'ready' ? validation.value.ids : []));

const isStarting = computed(() => gridState.value === 'starting');
const isUnavailable = computed(() => gridState.value === 'unavailable');
const isInvalid = computed(() => gridState.value === 'invalid');
const isEmpty = computed(() => gridState.value === 'empty');
const isReady = computed(() => gridState.value === 'ready');

const emit = defineEmits(['add-capsule']);
const slots = useSlots();
const region = ref(null);
const availableWidth = ref(960);
// 2 tiles side by side (2x1), 4 in 2x2, up to 12 in 4x3.
const columns = computed(() => Math.max(1, Math.min(4, Math.ceil(Math.sqrt(capsules.value.length || 1)))));
const wallRows = computed(() => Math.max(1, Math.ceil(capsules.value.length / columns.value)));
let resizeObserver = null;
/** @type {import('dockview-core').DockviewApi | null} */
let desktopApi = null;
/** @type {{ dispose: () => void } | null} */
let removalSubscription = null;
/** @type {{ dispose: () => void } | null} */
let groupSubscription = null;
let disposed = false;
let reconciling = false;
const panelId = (id) => `capsule:${id}`;

/** @param {any} group */
function hideGroupHeader(group) {
  if (!group) return;
  if (group.header) group.header.hidden = true;
  if (group.model?.header) group.model.header.hidden = true;
  group.relayout?.();
}

function hideAllGroupHeaders() {
  if (!desktopApi) return;
  for (const group of desktopApi.groups || []) {
    hideGroupHeader(group);
  }
  for (const panel of desktopApi.panels || []) {
    hideGroupHeader(panel?.group);
  }
}

// Dockview's Vue renderer supplies { params: { params, api, containerApi } }.
// Content remains the parent's genuine capsule view; this wall owns no verbs.
const CapsulePanel = markRaw(defineComponent({
  props: { params: { type: Object, required: true } },
  setup(router) {
    const visible = ref(router.params.api?.isVisible !== false);
    const subscription = router.params.api?.onDidVisibilityChange?.(event => { visible.value = event.isVisible === true; });
    onBeforeUnmount(() => subscription?.dispose());
    return () => {
      const id = router.params.params?.capsuleId;
      if (!isReady.value || !capsules.value.includes(id)) return null;
      return h('div', { class: 'cafe-desktop-panel', 'data-capsule-id': id },
        slots.capsule ? slots.capsule({ id, visible: visible.value }) : [
          h('p', { class: 'cafe-capsule-grid-fallback' }, `No capsule view supplied. ${id}`),
        ]);
    };
  },
}));
const CapsuleTab = markRaw(defineComponent({
  props: { params: { type: Object, required: true } },
  setup(tab) {
    return () => {
      const id = tab.params.params?.capsuleId || '';
      const label = tabTitleFor(id);
      return h('span', {
        class: 'cafe-desktop-tab', title: label,
      }, label);
    };
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
    for (const [index, id] of capsules.value.entries()) {
      const exactPanelId = panelId(id);
      const label = tabTitleFor(id);
      const existing = desktopApi.getPanel(exactPanelId);
      if (!existing) {
        const panel = desktopApi.addPanel({
          id: exactPanelId, component: 'CapsulePanel', tabComponent: 'CapsuleTab',
          title: label, params: { capsuleId: id }, renderer: 'always',
          minimumWidth: 160, minimumHeight: 120,
          ...(index >= columns.value
            ? { position: { referencePanel: panelId(capsules.value[index - columns.value]), direction: 'below' } }
            : index > 0 ? { position: { referencePanel: panelId(capsules.value[index - 1]), direction: 'right' } } : {}),
        });
        hideGroupHeader(panel?.group);
      } else {
        existing.api?.setTitle?.(label);
        hideGroupHeader(existing?.group);
      }
    }
    hideAllGroupHeaders();
  } finally { reconciling = false; }
}

/** Arrange existing panels into a grid and hide group tab headers. */
function arrange() {
  if (!desktopApi || disposed || reconciling) return;
  const panels = capsules.value.map(id => desktopApi.getPanel(panelId(id))).filter(Boolean);
  hideAllGroupHeaders();
  if (!panels.length || typeof panels[0].api?.moveTo !== 'function') return;
  reconciling = true;
  try {
    for (const panel of panels.slice(1)) panel.api.moveTo({ group: panels[0].group, position: 'center', skipSetActive: true });
    for (let index = 1; index < panels.length; index += 1) {
      const panel = panels[index];
      if (index < columns.value) panel.api.moveTo({ group: panels[index - 1].group, position: 'right', skipSetActive: true });
      else panel.api.moveTo({ group: panels[index - columns.value].group, position: 'bottom', skipSetActive: true });
    }
    hideAllGroupHeaders();
    const width = Math.max(160, Math.floor((desktopApi.width || availableWidth.value) / columns.value));
    const height = Math.max(120, Math.floor((desktopApi.height || 540) / wallRows.value));
    for (const panel of panels) panel.api.setSize?.({ width, height });
  } finally { reconciling = false; }
}

function onReady(event) {
  desktopApi = event.api;
  groupSubscription = desktopApi.onDidAddGroup?.(group => {
    hideGroupHeader(group);
  }) || null;
  removalSubscription = desktopApi.onDidRemovePanel(() => {
    // A local close never removes a capsule. Restore from the authoritative ids.
    if (!reconciling) queueMicrotask(() => { reconcile(); arrange(); });
  });
  reconcile();
  arrange();
}
watch(() => capsules.value.join('\0'), () => { reconcile(); arrange(); });
watch(() => props.tabLabels, () => { reconcile(); }, { deep: true });
watch(columns, arrange);
onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !region.value) return;
  resizeObserver = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width;
    if (typeof width === 'number' && width > 0) availableWidth.value = width;
  });
  resizeObserver.observe(region.value);
});
onBeforeUnmount(() => {
  disposed = true;
  resizeObserver?.disconnect();
  removalSubscription?.dispose();
  groupSubscription?.dispose();
  desktopApi = null;
});
</script>

<template>
  <section ref='region' class='cafe-capsule-grid-region cafe-desktop' aria-label='Capsules' :data-state='gridState' :data-wall-columns='columns' :data-wall-rows='wallRows'>
    <div v-if='isReady' class='cafe-wall-layout-tools'><span>{{ capsules.length }} / 12 capsules</span><button class='cafe-button' type='button' @click='arrange'>Arrange wall</button></div>
    <p v-if='isStarting' class='cafe-starting-line' role='status'>Starting Café...</p>
    <p v-else-if='isUnavailable' class='cafe-capsule-grid-note' role='status'>
      Capsules unavailable.
    </p>
    <p v-else-if='isInvalid' class='cafe-capsule-grid-note' role='status'>
      Capsule list unavailable: expected exact, unique host-issued identifiers.
    </p>
    <div v-else-if='isEmpty' class='cafe-desktop-empty'>
      <button class='cafe-button cafe-empty-add-button' type='button' @click="emit('add-capsule')">Add window</button>
      <p class='cafe-empty-instructions' role='status'>No capsules yet — pop out a Discord stream or camera window and click Add window.</p>
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

.cafe-starting-line {
  margin: auto;
  padding: 0;
  color: var(--muted);
  font-size: var(--text-sm, 0.875rem);
  line-height: var(--leading-body, 1.5);
  text-align: center;
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

.cafe-empty-instructions {
  margin: 0;
  padding: 0;
  color: var(--muted);
  font-size: var(--text-sm, 0.875rem);
  line-height: var(--leading-body, 1.5);
  text-align: center;
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
