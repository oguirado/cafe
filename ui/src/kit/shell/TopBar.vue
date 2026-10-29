<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import VendorDialog from 'openvue/dialog';
import { createCspDialog } from '../workspace/cspDialog.js';
const Dialog = createCspDialog(VendorDialog);
import BarsIcon from '@openvue/icons/bars';
import GridIcon from '@openvue/icons/thlarge';
import SearchIcon from '@openvue/icons/search';
import PlusIcon from '@openvue/icons/plus';
import InfoIcon from '@openvue/icons/infocircle';
import UiButton from '../ui/UiButton.vue';
import UiTextInput from '../ui/UiTextInput.vue';

/**
 * Generic S1 top bar (48px instrument header). No application names: the host
 * supplies product text, palette entries, speeds and notification state via
 * props, or overrides any region through slots. Optional SaaS slots
 * (role-pill, environment, credit, clock) render nothing unless the host
 * supplies slot content or the matching label prop.
 * @module shell/TopBar
 */

/**
 * @typedef {object} S1PaletteItem
 * @property {string} id Stable entry id.
 * @property {string} label Visible label.
 * @property {string} [group] Optional group label.
 */

/**
 * @typedef {object} S1SpeedOption
 * @property {string} id Stable speed id.
 * @property {string} label Short button label.
 * @property {string} name Accessible name.
 * @property {boolean} [pressed] True for the active tier.
 */

const props = defineProps({
  product: { type: String, required: true },
  searchPlaceholder: { type: String, default: 'Search' },
  paletteTitle: { type: String, default: 'Open' },
  paletteItems: { type: Array, default: () => [] },
  unreadCount: { type: Number, default: 0 },
  unreadAvailable: { type: Boolean, default: true },
  newLabel: { type: String, default: 'New' },
  speeds: { type: Array, default: () => [] },
  navExpanded: { type: Boolean, default: true },
  rolePill: { type: String, default: '' },
  environmentLabel: { type: String, default: '' },
  creditLabel: { type: String, default: '' },
  clockLabel: { type: String, default: '' },
});
const emit = defineEmits(['open', 'new-capsule', 'speed-change', 'notifications-open', 'help-open', 'toggle-nav']);

const palette = ref(false);
const query = ref('');
/** @type {import('vue').ComputedRef<S1PaletteItem[]>} */
const results = computed(() => {
  const items = /** @type {S1PaletteItem[]} */ (/** @type {unknown} */ (props.paletteItems));
  const needle = query.value.toLowerCase();
  return items.filter((item) => item.label.toLowerCase().includes(needle));
});
const notificationsLabel = computed(() => (!props.unreadAvailable ? 'Notifications, unread count unavailable' : props.unreadCount > 0
  ? `Notifications, ${props.unreadCount} unread`
  : 'Notifications'));

/** @param {S1PaletteItem} item */
function open(item) {
  emit('open', item);
  palette.value = false;
  query.value = '';
}
async function showPalette() {
  palette.value = true;
  await nextTick();
  document.getElementById('shell-command-input')?.focus();
}
function closePalette() {
  palette.value = false;
  query.value = '';
}
/** @param {KeyboardEvent} event */
function shortcut(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    void showPalette();
  }
}
onMounted(() => document.addEventListener('keydown', shortcut));
onBeforeUnmount(() => document.removeEventListener('keydown', shortcut));
defineExpose({ openPalette: showPalette, closePalette });
</script>
<template>
  <header id="shell/header" role="banner" :aria-label="product" class="s1-topbar">
    <div class="s1-topbar-left">
      <slot name="nav">
        <UiButton
          id="shell/navigation/toggle"
          data-semantic-anchor="shell/navigation/toggle"
          label="Toggle navigation"
          aria-label="Toggle navigation"
          :aria-expanded="String(navExpanded)"
          class="s1-topbar-icon"
          @click="emit('toggle-nav')"
        >
          <BarsIcon aria-hidden="true" />
        </UiButton>
      </slot>
      <slot name="identity">
        <span class="s1-topbar-identity">
          <span class="s1-topbar-tile" aria-hidden="true"><GridIcon /></span>
          <strong class="s1-topbar-wordmark">{{ product }}</strong>
        </span>
      </slot>
      <slot name="context" />
      <slot name="role-pill">
        <span v-if="rolePill" id="shell/topbar/role-pill" class="s1-topbar-pill" role="status">{{ rolePill }}</span>
      </slot>
    </div>
    <div class="s1-topbar-center">
      <slot name="search">
        <UiButton
          id="shell-command-open"
          data-semantic-anchor="shell/navigation/command-open"
          :label="searchPlaceholder"
          :aria-label="`${searchPlaceholder} (Control K)`"
          aria-keyshortcuts="Control+K"
          class="s1-topbar-search"
          @click="showPalette"
        >
          <SearchIcon aria-hidden="true" />
          <span class="s1-topbar-search-text">{{ searchPlaceholder }}</span>
          <kbd class="s1-topbar-kbd" aria-hidden="true"><span>Ctrl</span><span>K</span></kbd>
        </UiButton>
      </slot>
    </div>
    <div class="s1-topbar-right">
      <slot name="environment">
        <span v-if="environmentLabel" id="shell/topbar/environment" class="s1-topbar-meta">{{ environmentLabel }}</span>
      </slot>
      <slot name="credit">
        <span v-if="creditLabel" id="shell/topbar/credit" class="s1-topbar-meta">{{ creditLabel }}</span>
      </slot>
      <slot name="clock">
        <span v-if="clockLabel" id="shell/topbar/clock" class="s1-topbar-meta" role="timer">{{ clockLabel }}</span>
      </slot>
      <slot name="cluster" />
      <slot name="speed">
        <div v-if="speeds.length" class="s1-topbar-speed" role="group" aria-label="Simulation speed">
          <UiButton
            v-for="speed in /** @type {S1SpeedOption[]} */ (/** @type {unknown} */ (speeds))"
            :key="speed.id"
            :id="'shell/topbar/speed-' + speed.id"
            :data-semantic-anchor="'shell/navigation/speed-' + speed.id"
            :label="speed.label"
            :title="speed.name"
            :aria-label="speed.name"
            :aria-pressed="String(!!speed.pressed)"
            class="s1-topbar-speed-btn"
            :class="{ 's1-topbar-speed-active': speed.pressed }"
            @click="emit('speed-change', speed.id)"
          />
        </div>
      </slot>
      <span class="s1-topbar-divider" aria-hidden="true" />
      <slot name="actions">
        <span class="s1-topbar-actions">
          <UiButton
            id="shell/topbar/notifications"
            data-semantic-anchor="shell/navigation/notifications"
            label="Notifications"
            :aria-label="notificationsLabel"
            title="Notifications"
            class="s1-topbar-icon s1-topbar-notif"
            @click="emit('notifications-open')"
          >
            <InfoIcon aria-hidden="true" />
            <span v-if="unreadCount > 0" class="s1-topbar-dot" aria-hidden="true" />
          </UiButton>
          <UiButton
            id="shell/topbar/help"
            data-semantic-anchor="shell/navigation/help"
            label="?"
            aria-label="Documentation and help"
            title="Documentation and help"
            class="s1-topbar-icon"
            @click="emit('help-open')"
          >
            <span aria-hidden="true">?</span>
          </UiButton>
        </span>
      </slot>
      <slot name="actions-extra" />
      <span class="s1-topbar-divider" aria-hidden="true" />
      <slot name="primary">
        <UiButton
          id="shell/topbar/new"
          data-semantic-anchor="shell/navigation/new"
          :label="newLabel"
          :aria-label="newLabel"
          :title="newLabel"
          class="s1-topbar-new"
          @click="emit('new-capsule')"
        >
          <PlusIcon aria-hidden="true" />
          <span class="s1-topbar-new-label">{{ newLabel }}</span>
        </UiButton>
      </slot>
    </div>
  </header>
  <Dialog
    id="shell-command-palette"
    v-model:visible="palette"
    modal
    :header="paletteTitle"
    class="s1-topbar-palette"
    :draggable="false"
  >
    <UiTextInput
      id="shell-command-input"
      data-semantic-anchor="settings/capsule-search"
      v-model="query"
      label="Find entry"
      placeholder="Find an entry…"
      @keydown.enter="results[0] && open(results[0])"
    />
    <div role="list" aria-label="Matching entries" class="s1-topbar-palette-list">
      <UiButton
        v-for="item in results"
        :key="item.id"
        :id="'shell-command-' + item.id"
        :data-semantic-anchor="'shell/navigation/command-' + item.id"
        :label="item.label"
        :aria-label="'Open ' + item.label"
        @click="open(item)"
      />
    </div>
    <p v-if="!results.length" role="status" class="s1-topbar-palette-empty">
      No matching entries. Try another name.
    </p>
  </Dialog>
</template>
<style>
.s1-topbar{
  --s1-topbar-search-width: calc(var(--cuk-primitive-space-16) * 10);
  height: var(--cuk-component-app-shell-topbar-height);
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--cuk-semantic-spacing-md);
  padding: 0 var(--cuk-primitive-space-8);
  background: var(--cuk-semantic-surface-sunken);
  border-bottom: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  z-index: var(--cuk-primitive-z-index-panel);
  overflow-x: hidden;
  overflow-x: clip;
}
.s1-topbar-left,.s1-topbar-center,.s1-topbar-right{display:flex;align-items:center;flex:none;}
.s1-topbar-left,.s1-topbar-right{gap: var(--cuk-semantic-spacing-xl);}
.s1-topbar-center{flex: 0 1 auto; justify-content: center; min-width: 0;}
.s1-topbar :focus-visible{
  outline: var(--cuk-semantic-focus-ring-width) solid var(--cuk-primitive-color-s1-primary);
  outline-offset: var(--cuk-component-app-shell-space-hairline);
}
.s1-topbar-identity{display:flex;align-items:center;gap:var(--cuk-semantic-spacing-md);}
.s1-topbar-tile{
  width: var(--cuk-primitive-space-12); height: var(--cuk-primitive-space-12);
  display: grid; place-items: center;
  background: var(--cuk-semantic-surface-elevated);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-sm);
  color: var(--cuk-semantic-text-accent);
}
.s1-topbar-tile svg{width:var(--cuk-semantic-typography-font-size-lg);height:var(--cuk-semantic-typography-font-size-lg);}
.s1-topbar-wordmark{
  font-size: var(--cuk-semantic-typography-roles-headline-sm-font-size);
  line-height: var(--cuk-semantic-typography-roles-headline-sm-line-height);
  font-weight: var(--cuk-semantic-typography-roles-headline-sm-font-weight);
  font-family: var(--cuk-semantic-typography-font-family-sans);
  color: var(--cuk-semantic-text-primary);
}
.s1-topbar .s1-topbar-search{
  width: var(--s1-topbar-search-width); max-width: 100%; min-width: 0;
  height: var(--cuk-component-app-shell-status-height);
  display: inline-flex; align-items: center;
  gap: var(--cuk-semantic-spacing-md);
  padding-left: var(--cuk-primitive-space-5); padding-right: var(--cuk-primitive-space-3);
  background: var(--cuk-semantic-surface-canvas);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-sm);
  font-size: var(--cuk-semantic-typography-roles-body-sm-font-size);
  line-height: var(--cuk-semantic-typography-roles-body-sm-line-height);
  color: var(--cuk-semantic-text-muted);
  text-align: left;
}
.s1-topbar .s1-topbar-search svg{
  width: var(--cuk-semantic-typography-font-size-lg);
  height: var(--cuk-semantic-typography-font-size-lg);
  flex: none;
}
.s1-topbar-search-text{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.s1-topbar .s1-topbar-search:focus{
  border-color: var(--cuk-primitive-color-s1-primary);
  box-shadow: 0 0 0 var(--cuk-semantic-border-width) var(--cuk-primitive-color-s1-primary);
}
.s1-topbar-kbd{
  display: inline-flex; align-items: center;
  gap: var(--cuk-primitive-space-1);
  padding: var(--cuk-primitive-space-1) var(--cuk-primitive-space-2);
  background: var(--cuk-semantic-surface-elevated);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-xs);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-s1_10);
  color: var(--cuk-semantic-text-muted);
}
.s1-topbar-speed{
  display: flex; align-items: center;
  padding: var(--cuk-primitive-space-1);
  background: var(--cuk-semantic-surface-panel);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-sm);
}
.s1-topbar .s1-topbar-speed-btn{
  padding: var(--cuk-primitive-space-1) var(--cuk-primitive-space-3);
  background: transparent; border-color: transparent;
  border-radius: var(--cuk-semantic-radius-xs);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-xs);
  color: var(--cuk-semantic-text-muted);
  min-width: 0;
}
.s1-topbar .s1-topbar-speed-btn:hover{
  background: var(--cuk-semantic-surface-interactive);
  color: var(--cuk-semantic-text-primary);
}
.s1-topbar .s1-topbar-speed-active{
  background: var(--cuk-semantic-surface-elevated);
  border-color: var(--cuk-semantic-border-subtle);
  color: var(--cuk-semantic-text-accent);
  font-weight: var(--cuk-semantic-typography-font-weight-medium);
}
.s1-topbar-actions{display:flex;align-items:center;gap:var(--cuk-primitive-space-1);}
.s1-topbar .s1-topbar-icon{
  position: relative;
  padding: var(--cuk-primitive-space-3);
  background: transparent; border-color: transparent;
  border-radius: var(--cuk-semantic-radius-sm);
  color: var(--cuk-semantic-text-muted);
  min-width: 0;
}
.s1-topbar .s1-topbar-icon:hover{
  background: var(--cuk-semantic-surface-interactive);
  color: var(--cuk-semantic-text-primary);
}
.s1-topbar .s1-topbar-icon svg{
  width: var(--cuk-semantic-typography-font-size-lg);
  height: var(--cuk-semantic-typography-font-size-lg);
}
.s1-topbar-dot{
  position: absolute;
  top: var(--cuk-primitive-space-3); right: var(--cuk-primitive-space-3);
  width: var(--cuk-primitive-space-3); height: var(--cuk-primitive-space-3);
  border-radius: var(--cuk-semantic-radius-pill);
  background: var(--cuk-primitive-color-s1-primary);
  box-shadow: 0 0 0 var(--cuk-primitive-space-1) var(--cuk-semantic-surface-sunken);
}
/* P5: decorative divider keeps the muted text tone so it holds >= 3:1
   on the sunken bar in every scheme (border-subtle is 1:1 in light). */
.s1-topbar-divider{
  width: var(--cuk-semantic-border-width); height: var(--cuk-primitive-space-8);
  background: var(--cuk-semantic-text-muted); flex: none;
}
.s1-topbar-pill{
  display: inline-flex; align-items: center;
  padding: var(--cuk-primitive-space-1) var(--cuk-primitive-space-3);
  background: var(--cuk-semantic-surface-elevated);
  border: var(--cuk-semantic-border-width) solid var(--cuk-semantic-border-subtle);
  border-radius: var(--cuk-semantic-radius-pill);
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-s1_10);
  color: var(--cuk-semantic-text-muted);
  white-space: nowrap;
}
.s1-topbar-meta{
  font-family: var(--cuk-semantic-typography-font-family-mono);
  font-size: var(--cuk-semantic-typography-font-size-xs);
  color: var(--cuk-semantic-text-muted);
  white-space: nowrap;
}
.s1-topbar .s1-topbar-new{
  height: var(--cuk-component-app-shell-status-height);
  display: inline-flex; align-items: center;
  gap: var(--cuk-primitive-space-2);
  padding: 0 var(--cuk-primitive-space-5);
  background: var(--cuk-primitive-color-s1-primary);
  border-color: transparent;
  border-radius: var(--cuk-semantic-radius-sm);
  font-size: var(--cuk-semantic-typography-font-size-xs);
  font-weight: var(--cuk-semantic-typography-font-weight-semibold);
  color: var(--cuk-primitive-color-s1-on-primary);
}
.s1-topbar .s1-topbar-new:hover{opacity: 0.9;}
.s1-topbar .s1-topbar-new svg{
  width: var(--cuk-semantic-typography-font-size-s1_14);
  height: var(--cuk-semantic-typography-font-size-s1_14);
}
.s1-topbar-palette{width: var(--cuk-component-app-shell-palette-width); max-width: 100vw;}
.s1-topbar-palette input{width: 100%;}
.s1-topbar-palette-list{
  display: flex; flex-direction: column;
  gap: var(--cuk-semantic-spacing-xs);
  padding-top: var(--cuk-semantic-spacing-sm);
}
.s1-topbar-palette-empty{color: var(--cuk-semantic-text-muted);}
/* Narrow viewports (pl1-008): collapse labels to icons instead of
   overflowing. 80em tracks the 1280-wide bench viewport at the default root
   size; em units keep the breakpoint root-relative (var() is invalid in
   media queries). Accessible names stay on the controls. */
@media (max-width: 80em){
  .s1-topbar-left,.s1-topbar-right{gap: var(--cuk-semantic-spacing-md);}
  .s1-topbar-left{flex: 0 1 auto; min-width: 0;}
  .s1-topbar-identity{min-width: 0;}
  .s1-topbar-wordmark{overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
  .s1-topbar-kbd{display: none;}
  .s1-topbar-new-label{display: none;}
}

.cuk-csp-dialog:not([open]) { display: none !important; }
.cuk-csp-dialog { margin: auto; padding: 0; color: var(--cuk-semantic-text-primary); background: var(--cuk-component-dialog-host-surface); }
</style>
