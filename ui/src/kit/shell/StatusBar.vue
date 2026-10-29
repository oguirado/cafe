<script setup>
import { ref } from 'vue';
import VendorDialog from 'openvue/dialog';
import { createCspDialog } from '../workspace/cspDialog.js';
const Dialog = createCspDialog(VendorDialog);
import UiButton from '../ui/UiButton.vue';

/**
 * Generic S1 status bar (28px telemetry footer). The host supplies breadcrumb,
 * telemetry and shortcut entries via props, or overrides any region through
 * slots. The optional layout slot (layoutSaved prop, default null) renders
 * nothing unless the host opts in. No application names.
 * @module shell/StatusBar
 */

/**
 * @typedef {object} S1Crumb
 * @property {string} label Visible label.
 * @property {('dim'|'strong'|'accent')} [tone] Visual tone; last crumb defaults to accent.
 */

/**
 * @typedef {object} S1TelemetryItem
 * @property {string} key Label key.
 * @property {string} value Display value.
 * @property {boolean} [live] True to prefix the live dot.
 */

/**
 * @typedef {object} S1Shortcut
 * @property {string} id Stable shortcut id.
 * @property {string} label Visible label.
 * @property {string} kbd Key hint chip text.
 * @property {boolean} [opensDialog] True to open the shortcut list instead of emitting.
 */

const props = defineProps({
  crumbs: { type: Array, default: () => [] },
  telemetry: { type: Array, default: () => [] },
  /** Tri-state layout persistence flag: null (default) renders no layout region. */
  layoutSaved: { type: Boolean, default: null },
  shortcuts: {
    type: Array,
    default: () => [
      { id: 'terminal', label: 'Terminal', kbd: '~' },
      { id: 'shortcuts', label: 'Shortcuts', kbd: '?', opensDialog: true },
    ],
  },
});
const emit = defineEmits(['shortcut-activate']);

const dialog = ref(false);
function openShortcuts() { dialog.value = true; }
function closeShortcuts() { dialog.value = false; }
/** @param {S1Shortcut} item */
function activate(item) {
  if (item.opensDialog) { openShortcuts(); return; }
  emit('shortcut-activate', item.id);
}
/**
 * @param {S1Crumb} crumb
 * @param {number} index
 * @param {number} total
 */
function crumbTone(crumb, index, total) {
  if (crumb.tone) return crumb.tone;
  return index === total - 1 ? 'accent' : 'dim';
}
defineExpose({ openShortcuts, closeShortcuts });
</script>
<template>
  <footer id="shell/statusbar" role="contentinfo" aria-label="Workspace status" class="s1-statusbar">
    <slot name="breadcrumb">
      <nav aria-label="Breadcrumb" class="s1-statusbar-crumbs">
        <ol>
          <li
            v-for="(crumb, index) in /** @type {S1Crumb[]} */ (/** @type {unknown} */ (crumbs))"
            :key="`${index}-${crumb.label}`"
            :aria-current="index === crumbs.length - 1 ? 'page' : undefined"
            :class="'s1-statusbar-crumb-' + crumbTone(crumb, index, crumbs.length)"
          >
            <span v-if="index > 0" class="s1-statusbar-sep" aria-hidden="true">/</span>
            <span>{{ crumb.label }}</span>
          </li>
        </ol>
      </nav>
    </slot>
    <slot name="telemetry">
      <div
        v-if="telemetry.length"
        class="s1-statusbar-telemetry"
        aria-label="System telemetry"
      >
        <span
          v-for="(item, index) in /** @type {S1TelemetryItem[]} */ (/** @type {unknown} */ (telemetry))"
          :key="`${index}-${item.key}`"
          class="s1-statusbar-metric"
        >
          <span v-if="index > 0" class="s1-statusbar-bullet" aria-hidden="true">•</span>
          <span v-if="item.live" class="s1-statusbar-live" aria-hidden="true" />
          <span class="s1-statusbar-key">{{ item.key }}:</span>
          <strong class="s1-statusbar-value">{{ item.value }}</strong>
        </span>
      </div>
    </slot>
    <slot name="layout">
      <span
        v-if="layoutSaved !== null && layoutSaved !== undefined"
        id="shell/statusbar/layout"
        class="s1-statusbar-layout"
        role="status"
      >{{ layoutSaved ? 'Layout saved' : 'Layout unsaved' }}</span>
    </slot>
    <slot name="shortcuts">
      <span class="s1-statusbar-shortcuts">
        <template
          v-for="(item, index) in /** @type {S1Shortcut[]} */ (/** @type {unknown} */ (shortcuts))"
          :key="item.id"
        >
          <span v-if="index > 0" class="s1-statusbar-sep" aria-hidden="true">|</span>
          <UiButton
            :id="'shell/statusbar/' + item.id"
            :data-semantic-anchor="'shell/navigation/' + item.id"
            :label="item.label"
            :aria-label="`${item.label} (${item.kbd})`"
            class="s1-statusbar-shortcut"
            @click="activate(item)"
          >
            <span>{{ item.label }}</span>
            <kbd class="s1-statusbar-kbd" aria-hidden="true">{{ item.kbd }}</kbd>
          </UiButton>
        </template>
      </span>
    </slot>
  </footer>
  <Dialog
    id="shell/statusbar/shortcuts-dialog"
    v-model:visible="dialog"
    modal
    header="Keyboard shortcuts"
    class="s1-statusbar-dialog"
    :draggable="false"
  >
    <dl class="s1-statusbar-keys">
      <div
        v-for="item in /** @type {S1Shortcut[]} */ (/** @type {unknown} */ (shortcuts))"
        :key="item.id"
        class="s1-statusbar-key-row"
      >
        <dt>{{ item.label }}</dt>
        <dd><kbd class="s1-statusbar-kbd">{{ item.kbd }}</kbd></dd>
      </div>
      <div class="s1-statusbar-key-row">
        <dt>Command palette</dt>
        <dd><kbd class="s1-statusbar-kbd">Ctrl K</kbd></dd>
      </div>
    </dl>
    <UiButton
      id="shell/statusbar/shortcuts-close"
      data-semantic-anchor="dialog/shortcuts/close"
      label="Close"
      aria-label="Close shortcuts"
      @click="closeShortcuts"
    />
  </Dialog>
</template>
<style>
.s1-statusbar{
  height: var(--cuk-component-app-shell-status-height);
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--cuk-semantic-spacing-md);
  padding: 0 var(--cuk-primitive-space-6);
  background: var(--cuk-semantic-surface-sunken);
  border-top: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-xs);
  color: var(--cuk-semantic-text-muted);
  z-index: var(--cuk-primitive-z-index-panel);
  overflow-x: auto;
}
.s1-statusbar :focus-visible{
  outline: var(--cuk-semantic-focus-ring-width) solid var(--cuk-primitive-color-s1-primary);
  outline-offset: var(--cuk-component-app-shell-space-hairline);
}
.s1-statusbar-crumbs ol{
  display: flex; align-items: center;
  gap: var(--cuk-primitive-space-3);
  list-style: none;
  margin: 0; padding: 0;
  white-space: nowrap;
}
.s1-statusbar-crumbs li{display: flex; align-items: center; gap: var(--cuk-primitive-space-3);}
/* P5: decorative separators keep the muted text tone so they hold >= 3:1
   on the sunken bar in every scheme (border-subtle is 1:1 in light). */
.s1-statusbar-sep{color: var(--cuk-semantic-text-muted);}
.s1-statusbar-crumb-dim{color: var(--cuk-semantic-text-disabled);}
.s1-statusbar-crumb-strong{color: var(--cuk-semantic-text-primary);font-weight:var(--cuk-semantic-typography-font-weight-medium);}
.s1-statusbar-crumb-accent{color: var(--cuk-semantic-text-accent);font-weight:var(--cuk-semantic-typography-font-weight-medium);}
.s1-statusbar-telemetry{display:flex;align-items:center;gap:var(--cuk-primitive-space-8);}
.s1-statusbar-layout{color:var(--cuk-semantic-text-disabled);white-space:nowrap;}
.s1-statusbar-metric{display:flex;align-items:center;gap:var(--cuk-primitive-space-3);white-space:nowrap;}
.s1-statusbar-bullet{color: var(--cuk-semantic-text-muted);}
.s1-statusbar-live{
  width: var(--cuk-primitive-space-3); height: var(--cuk-primitive-space-3);
  border-radius: var(--cuk-semantic-radius-pill);
  background: var(--cuk-primitive-color-emerald-400);
}
.s1-statusbar-key{color: var(--cuk-semantic-text-disabled);}
.s1-statusbar-value{color: var(--cuk-semantic-text-primary);font-weight:var(--cuk-semantic-typography-font-weight-regular);}
.s1-statusbar-shortcuts{display:flex;align-items:center;gap:var(--cuk-primitive-space-4);}
.s1-statusbar .s1-statusbar-shortcut{
  display: inline-flex; align-items: center;
  gap: var(--cuk-primitive-space-2);
  padding: 0;
  background: transparent; border-color: transparent;
  color: var(--cuk-semantic-text-disabled);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-xs);
  min-width: 0;
}
.s1-statusbar .s1-statusbar-shortcut:hover{color: var(--cuk-primitive-color-s1-primary);}
.s1-statusbar-kbd{
  padding: 0 var(--cuk-primitive-space-2);
  background: var(--cuk-semantic-surface-elevated);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-xs);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-s1_10);
  color: var(--cuk-semantic-text-primary);
}
.s1-statusbar-dialog{width: var(--cuk-component-app-shell-palette-width); max-width: 100vw;}
.s1-statusbar-keys{display:flex;flex-direction:column;gap:var(--cuk-semantic-spacing-sm);margin:0 0 var(--cuk-semantic-spacing-md);}
.s1-statusbar-key-row{display:flex;align-items:center;justify-content:space-between;gap:var(--cuk-semantic-spacing-md);}
.s1-statusbar-key-row dt{color: var(--cuk-semantic-text-muted);}
.s1-statusbar-key-row dd{margin: 0;}

.cuk-csp-dialog:not([open]) { display: none !important; }
.cuk-csp-dialog { margin: auto; padding: 0; color: var(--cuk-semantic-text-primary); background: var(--cuk-component-dialog-host-surface); }
</style>
