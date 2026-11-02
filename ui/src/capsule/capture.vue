<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createPreviewSession } from './capture.js';

const props = defineProps({
  capsuleId: { type: String, required: true },
  feed: { type: Object, required: true },
  enabled: { type: Boolean, default: true },
  visible: { type: Boolean, default: true },
});
const emit = defineEmits(['state']);
const surface = ref(null);
const video = ref(null);
const state = ref('no_preview');
const reason = ref('preview_disabled');
const intersecting = ref(false);
const documentVisible = ref(true);
let session = null, intersection = null, resizeObserver = null, resizeTimer = null;
const externalState = computed(() => ['window_minimized', 'window_closed'].includes(props.feed.window_state) ? props.feed.window_state : '');
const placeholder = computed(() => externalState.value === 'window_closed' ? 'Window closed'
  : externalState.value === 'window_minimized' ? 'Window minimized'
    : state.value === 'connecting' ? 'Connecting preview' : 'No preview');
const note = computed(() => externalState.value === 'window_minimized' ? 'Restore it in Windows'
  : externalState.value === 'window_closed' ? 'Remove the capsule, then anchor a new pop-out.'
    : reason.value === 'preview_denied' ? 'Preview permission unavailable; recording setup is separate.'
      : state.value === 'connecting' ? 'Waiting for an approved window source.'
        : 'Recording can still run while preview is unavailable.');

function publish(value, why) { state.value = value; reason.value = why; emit('state', value); }
function visibility() {
  session?.setVisibility(props.visible && intersecting.value && documentVisible.value && externalState.value === '', props.enabled);
}
function pageVisibility() {
  documentVisible.value = typeof document === 'undefined' || document.visibilityState !== 'hidden'; visibility();
}
function binding() {
  session?.setBinding({ capsule_id: props.capsuleId, feed_id: props.feed.feed_id, generation: props.feed.generation });
}
watch(() => [props.capsuleId, props.feed.feed_id, props.feed.generation], binding);
watch(() => [props.enabled, props.visible, externalState.value], visibility);
onMounted(() => {
  const scope = typeof window === 'undefined' ? {} : /** @type {{cafe?:unknown}} */ (/** @type {unknown} */ (window));
  session = createPreviewSession({
    bridge: scope.cafe,
    getDisplayMedia: constraints => {
      if (typeof navigator === 'undefined' || typeof navigator.mediaDevices?.getDisplayMedia !== 'function') return Promise.reject(new Error('preview_unavailable'));
      return navigator.mediaDevices.getDisplayMedia(constraints);
    },
    onState: publish,
    onStream: stream => {
      if (!video.value) return;
      video.value.srcObject = stream;
      if (stream && typeof video.value.play === 'function') {
        const result = video.value.play();
        result?.catch(() => { session?.setVisibility(false, props.enabled); publish('no_preview', 'preview_denied'); });
      }
    },
    size: () => ({ width: surface.value?.clientWidth || 1, height: surface.value?.clientHeight || 1 }),
  });
  binding(); pageVisibility();
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', pageVisibility);
  if (typeof IntersectionObserver !== 'undefined' && surface.value) {
    intersection = new IntersectionObserver(entries => {
      intersecting.value = entries.some(entry => entry.isIntersecting && entry.intersectionRatio > 0); visibility();
    }, { threshold: 0.01 });
    intersection.observe(surface.value);
  }
  if (typeof ResizeObserver !== 'undefined' && surface.value) {
    resizeObserver = new ResizeObserver(() => {
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { resizeTimer = null; void session?.resize(); }, 120);
    });
    resizeObserver.observe(surface.value);
  }
});
onBeforeUnmount(() => {
  intersection?.disconnect(); resizeObserver?.disconnect();
  if (resizeTimer !== null) clearTimeout(resizeTimer);
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', pageVisibility);
  session?.dispose(); session = null;
  if (video.value) video.value.srcObject = null;
});
</script>

<template>
  <div ref="surface" class="cafe-feed-preview" :data-preview-state="externalState || state">
    <video ref="video" :hidden="state !== 'live' || externalState !== ''" muted autoplay playsinline :aria-label="feed.role === 'camera' ? 'Camera window preview' : 'Anchored window preview'"></video>
    <div v-if="state !== 'live' || externalState !== ''" class="cafe-preview-placeholder" role="status">
      <strong>{{ placeholder }}</strong><span>{{ note }}</span>
    </div>
  </div>
</template>
