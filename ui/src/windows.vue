<script setup>
// @ts-check
import { computed, ref } from 'vue';
const props = defineProps({
  windows: { type: Array, required: true },
  currentPage: { type: Object, default: () => ({ offset: 0, nextOffset: null }) },
  total: { type: Number, default: null },
  status: { type: String, default: 'idle' },
  busy: { type: Boolean, default: false },
  selectedId: { type: String, default: '' },
});
const emit = defineEmits(['select', 'refresh', 'prev', 'next', 'close', 'pair']);
const query = ref('');
const discordOnly = ref(false);
const copyNote = ref('');
const copying = ref(false);
const tokenField = ref(null);
const admitted = computed(() => {
  if (!Array.isArray(props.windows)) return [];
  const seen = new Set();
  for (const entry of props.windows) {
    if (entry === null || typeof entry !== 'object' || typeof entry.token !== 'string'
      || entry.token.length === 0 || new TextEncoder().encode(entry.token).length > 128
      || entry.token.includes('\0') || seen.has(entry.token)) return [];
    seen.add(entry.token);
  }
  return props.windows;
});
const matches = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return admitted.value.filter(entry => {
    const exe = typeof entry.exe === 'string' ? entry.exe : '';
    const title = typeof entry.title === 'string' ? entry.title : '';
    if (discordOnly.value && !exe.toLocaleLowerCase().includes('discord')) return false;
    return !term || [title, exe, entry.token].some(value => value.toLocaleLowerCase().includes(term));
  });
});
const selected = computed(() => admitted.value.find(entry => entry.token === props.selectedId));
const blocked = computed(() => props.busy || props.status === 'unavailable' || props.status === 'failed');
const totalText = computed(() => typeof props.total === 'number' && Number.isSafeInteger(props.total) && props.total >= 0 ? String(props.total) : 'sin confirmar');
const statusText = computed(() => ({
  idle: 'Pulsa Actualizar para buscar ventanas.', loading: 'Buscando ventanas…',
  ready: 'Ventanas actualizadas.', incomplete: 'Listado parcial. Actualiza para volver a buscar.',
  unavailable: 'Café no está conectado. Abre la aplicación antes de buscar ventanas.',
  failed: 'No se pudo completar la búsqueda. Actualiza para intentarlo de nuevo.',
  selection_stale: 'La ventana cambió o se cerró. Selecciónala de nuevo.',
}[props.status] || 'Estado de búsqueda sin confirmar'));
async function copyIdentifier() {
  if (!selected.value || copying.value || blocked.value) return;
  const token = selected.value.token;
  copying.value = true; copyNote.value = '';
  try {
    if (!globalThis.navigator?.clipboard?.writeText) throw new Error('unavailable');
    await globalThis.navigator.clipboard.writeText(token);
    if (selected.value?.token === token) copyNote.value = 'Identificador copiado';
  } catch {
    copyNote.value = 'Selecciona el identificador y pulsa Ctrl+C.';
    const field = tokenField.value;
    if (field && typeof field.focus === 'function') field.focus();
    if (field && typeof field.select === 'function') field.select();
  } finally { copying.value = false; }
}
</script>
<template>
  <section id="cafe-window-browser" class="cafe-window-browser" role="dialog" aria-modal="true" aria-labelledby="cafe-window-browser-title">
    <header class="cafe-window-browser-header">
      <div><h2 id="cafe-window-browser-title">Elegir ventana</h2><p>Deja el pop-out de Discord abierto y sin minimizar.</p></div>
      <button type="button" class="cafe-button" @click="emit('close')">Cerrar</button>
    </header>
    <div class="cafe-window-browser-tools">
      <input v-model="query" class="cafe-input" type="search" aria-label="Buscar ventanas" placeholder="Buscar por título, aplicación o identificador" />
      <label><input v-model="discordOnly" type="checkbox" /> Solo Discord</label>
      <button type="button" class="cafe-button" :disabled="busy" @click="emit('refresh')">Actualizar</button>
    </div>
    <p class="cafe-text" role="status">{{ statusText }}</p>
    <p class="cafe-text">{{ matches.length }} coincidencias · {{ admitted.length }} cargadas · total {{ totalText }}</p>
    <ul class="cafe-window-browser-list">
      <li v-for="entry in matches" :key="entry.token">
        <button type="button" class="cafe-window-row" :class="{ selected: entry.token === selectedId }" :aria-pressed="entry.token === selectedId" :disabled="blocked" @click="emit('select', entry.token)">
          <span>{{ entry.title || 'Ventana sin título' }}</span><small>{{ entry.exe || 'Aplicación sin confirmar' }}</small><code>{{ entry.token }}</code>
        </button>
      </li>
    </ul>
    <p v-if="status === 'ready' && matches.length === 0" class="cafe-text">No hay ventanas que coincidan con la búsqueda.</p>
    <div class="cafe-window-browser-pages">
      <button type="button" class="cafe-button" :disabled="busy || !currentPage || currentPage.offset === 0" @click="emit('prev')">Anterior</button>
      <button type="button" class="cafe-button" :disabled="busy || !currentPage || currentPage.nextOffset === null || currentPage.nextOffset === undefined" @click="emit('next')">Siguiente</button>
    </div>
    <div v-if="selected" class="cafe-window-browser-identity">
      <label for="cafe-window-token">Identificador de ventana</label>
      <input id="cafe-window-token" ref="tokenField" class="cafe-input" readonly :value="selected.token" />
      <button type="button" class="cafe-button" :disabled="copying || blocked" @click="copyIdentifier">Copiar identificador</button>
      <p role="status">{{ copyNote }}</p>
    </div>
    <slot name="advanced"></slot>
    <footer><button type="button" class="cafe-button cafe-primary" :disabled="!selected || blocked" @click="emit('pair')">Emparejar ventana</button></footer>
  </section>
</template>
