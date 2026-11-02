<script>
// @ts-check
import { computed, defineComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue';
/** Largest equal 16:9 tiles fitting the measured area, including gaps.
 * @param {number} count @param {number} width @param {number} height @param {number} [gap]
 */
export function galleryLayout(count, width, height, gap = 8) {
  const empty = { columns: 0, rows: 0, width: 0, height: 0 };
  if (!Number.isInteger(count) || count <= 0 || !Number.isFinite(width) || width <= 0
    || !Number.isFinite(height) || height <= 0 || !Number.isFinite(gap) || gap < 0) return empty;
  let best = empty;
  for (let columns = 1; columns <= count; columns += 1) {
    const rows = Math.ceil(count / columns);
    const tileWidth = Math.min((width - gap * (columns - 1)) / columns,
      (height - gap * (rows - 1)) / rows * 16 / 9);
    if (tileWidth > best.width) best = { columns, rows, width: tileWidth, height: tileWidth * 9 / 16 };
  }
  return best;
}
/** Display-only gallery. Host ids and slot contents remain authoritative. */

/**
 * @typedef {object} CapsuleGridValidation
 * @property {boolean} ok
 * @property {string[]} ids
 */

export default defineComponent({
  props: {
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
    /** Parent disables shortcuts while a dialog or approval needs attention. */
    keyboardEnabled: { type: Boolean, default: true },
    /** Display-only accessible tile labels keyed by capsule id. */
    tabLabels: { type: Object, default: () => ({}) },
  },
  emits: ['add-capsule'],
  setup(props, { emit }) {

    /**
     * @param {unknown} value
     * @returns {value is string}
     */
    function isExactCapsuleId(value) {
      return typeof value === 'string' && value.length > 0 && value === value.trim()
        && !/\s/.test(value) && !/^\d+$/.test(value);
    }

    /** @type {import('vue').ComputedRef<CapsuleGridValidation>} */
    const validation = computed(() => {
      const ids = props.capsuleIDs;
      if (!Array.isArray(ids)) return { ok: false, ids: [] };
      /** @type {string[]} */
      const admitted = [];
      const seen = new Set();
      for (const id of ids) {
        if (!isExactCapsuleId(id) || seen.has(id)) return { ok: false, ids: [] };
        seen.add(id);
        admitted.push(id);
      }
      return { ok: true, ids: admitted };
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

    /** @type {import('vue').Ref<HTMLElement | null>} */
    const region = ref(null);
    const area = ref({ width: 0, height: 0 });
    const focusedId = ref('');
    const gap = 8;
    const layout = computed(() => galleryLayout(capsules.value.length, area.value.width, area.value.height, gap));
    /** @type {ResizeObserver | null} */
    let resizeObserver = null;

    // Focus changes coordinates only: keyed capsule views never leave the wall.
    const positions = computed(() => {
      const { width, height } = area.value;
      const ids = capsules.value;
      const focused = ids.includes(focusedId.value) ? focusedId.value : '';
      let bounds = { x: 0, y: 0, width, height };
      /** @type {Record<string, {x: number, y: number, width: number, height: number}>} */
      const boxes = Object.create(null);
      let remaining = ids;
      if (focused && ids.length > 1) {
        const stripHeight = Math.max(0, (height - gap) * 0.24);
        const mainHeight = Math.max(0, height - gap - stripHeight);
        const main = galleryLayout(1, width, mainHeight, gap);
        boxes[focused] = { x: (width - main.width) / 2, y: (mainHeight - main.height) / 2, ...main };
        bounds = { x: 0, y: mainHeight + gap, width, height: stripHeight };
        remaining = ids.filter(id => id !== focused);
      }
      // Keep the other tiles in one horizontal strip; scroll only this viewport.
      const stripWidth = bounds.height * 16 / 9;
      const grid = focused && ids.length > 1
        ? { columns: remaining.length, rows: 1, width: stripWidth, height: bounds.height }
        : galleryLayout(remaining.length, bounds.width, bounds.height, gap);
      bounds.width = Math.max(bounds.width, grid.columns * grid.width + Math.max(0, grid.columns - 1) * gap);
      remaining.forEach((id, index) => {
        const row = Math.floor(index / (grid.columns || 1));
        const col = index % (grid.columns || 1);
        const rowCount = Math.min(grid.columns, remaining.length - row * grid.columns);
        boxes[id] = {
          x: bounds.x + (bounds.width - rowCount * grid.width - Math.max(0, rowCount - 1) * gap) / 2 + col * (grid.width + gap),
          y: bounds.y + (bounds.height - grid.rows * grid.height - Math.max(0, grid.rows - 1) * gap) / 2 + row * (grid.height + gap),
          width: grid.width, height: grid.height,
        };
      });
      return boxes;
    });
    const trackWidth = computed(() => Math.max(area.value.width,
      ...Object.values(positions.value).map(box => box.x + box.width)));
    /** @param {string} id */
    function tileStyle(id) {
      const box = positions.value[id];
      return { position: focusedId.value === id ? 'sticky' : 'absolute', left: `${box.x}px`, top: `${box.y}px`, width: `${box.width}px`, height: `${box.height}px` };
    }
    /** @param {string} id */
    function toggleFocus(id) {
      focusedId.value = focusedId.value === id ? '' : id;
    }
    /** Display-only shortcuts never dispatch authority commands. @param {KeyboardEvent} event */
    function onKeydown(event) {
      if (!props.keyboardEnabled || !isReady.value || event.defaultPrevented || event.repeat
        || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const target = /** @type {Element|null} */ (event.target);
      if (typeof target?.closest === 'function'
        && target.closest('input, textarea, select, [contenteditable], [role="textbox"], [role="dialog"], [aria-modal="true"], [inert]')) return;
      if (event.key === 'Escape' && focusedId.value !== '') {
        focusedId.value = ''; event.preventDefault(); return;
      }
      if (!/^[1-9]$/.test(event.key)) return;
      const id = capsules.value[Number(event.key) - 1];
      if (id === undefined) return;
      focusedId.value = id; event.preventDefault();
    }
    watch(capsules, ids => { if (!ids.includes(focusedId.value)) focusedId.value = ''; });
    onMounted(() => {
      if (typeof document !== 'undefined') document.addEventListener('keydown', onKeydown);
      if (typeof ResizeObserver === 'undefined' || !region.value) return;
      resizeObserver = new ResizeObserver(entries => {
        const rect = entries[0]?.contentRect;
        if (rect && Number.isFinite(rect.width) && Number.isFinite(rect.height)) {
          area.value = { width: Math.max(0, rect.width), height: Math.max(0, rect.height) };
        }
      });
      resizeObserver.observe(region.value);
    });
    onBeforeUnmount(() => {
      resizeObserver?.disconnect();
      if (typeof document !== 'undefined') document.removeEventListener('keydown', onKeydown);
    });
    return { region, layout, gridState, focusedId, isStarting, isUnavailable, isInvalid, isEmpty,
      capsules, isReady, trackWidth, tileStyle, toggleFocus, emit };
  },
});
</script>

<template>
  <section class="cafe-gallery" aria-label="Capsules" :data-state="gridState" :data-wall-columns="layout.columns" :data-wall-rows="layout.rows" :data-focused-id="focusedId">
    <p v-if="isStarting" class="cafe-starting-line" role="status">Starting Café...</p>
    <p v-else-if="isUnavailable" class="cafe-capsule-grid-note" role="status">Capsules unavailable.</p>
    <p v-else-if="isInvalid" class="cafe-capsule-grid-note" role="status">Capsule list unavailable: expected exact, unique host-issued identifiers.</p>
    <div v-else-if="isEmpty" class="cafe-desktop-empty">
      <button class="cafe-button cafe-empty-add-button" type="button" @click="emit('add-capsule')">Add window</button>
      <p class="cafe-empty-instructions" role="status">No capsules yet — pop out a Discord stream or camera window and click Add window.</p>
    </div>
    <div ref="region" class="cafe-gallery-viewport" :class="{ 'cafe-gallery-focus-view': focusedId !== '' }">
      <div class="cafe-gallery-track" :style="{ width: `${trackWidth}px` }">
        <div v-for="id in capsules" :key="id" class="cafe-gallery-tile" :class="{ 'cafe-gallery-focused': focusedId === id }" :style="tileStyle(id)" :data-capsule-id="id" :aria-label="tabLabels[id] || id">
          <slot name="capsule" :id="id" :visible="isReady" :focused="focusedId === id" :toggle-focus="() => toggleFocus(id)">
            <p>No capsule view supplied. {{ id }}</p>
          </slot>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cafe-gallery { position: relative; flex: 1; min-width: 0; min-height: 0; overflow: hidden; color: var(--fg); }
.cafe-gallery-viewport { position: absolute; inset: 0; overflow: hidden; }
.cafe-gallery-focus-view { overflow-x: auto; }
.cafe-gallery-track { position: relative; height: 100%; }
.cafe-gallery-tile { position: absolute; min-width: 0; min-height: 0; overflow: hidden; border-radius: var(--radius-sm); outline: 1px solid var(--border); }
.cafe-gallery-focused { outline-color: var(--accent); }
.cafe-starting-line, .cafe-capsule-grid-note { text-align: center; color: var(--muted); }
.cafe-desktop-empty { position: relative; z-index: 1; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-3); }
.cafe-empty-instructions { margin: 0; color: var(--muted); text-align: center; font-size: var(--text-sm); }
</style>
