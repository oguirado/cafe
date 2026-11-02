<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createPreviewSession, previewGeometry, previewReasonText, observePreviewFrames } from './capture.js';

const props = defineProps({
  capsuleId: { type: String, required: true },
  feed: { type: Object, required: true },
  enabled: { type: Boolean, default: true },
  visible: { type: Boolean, default: true },
});
const emit = defineEmits(['state']);
const surface = ref(null);
const video = ref(null);
const dimensions = ref({ width: 0, height: 0, videoWidth: 0, videoHeight: 0 });
function measure() {
  dimensions.value = { width: surface.value?.clientWidth || 0, height: surface.value?.clientHeight || 0,
    videoWidth: video.value?.videoWidth || 0, videoHeight: video.value?.videoHeight || 0 };
}
const geometry = computed(() => { const d = dimensions.value; return previewGeometry(d.width, d.height, d.videoWidth, d.videoHeight, props.feed.top_trim ?? 0); });
const frameStyle = computed(() => ({ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', overflow: 'hidden',
  width: geometry.value ? `${geometry.value.width}px` : '100%', height: geometry.value ? `${geometry.value.height}px` : '100%' }));
const videoStyle = computed(() => geometry.value ? { inset: 'auto', left: '0', top: `${geometry.value.top}px`, width: '100%', height: `${geometry.value.videoHeight}px` } : {});
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
    : previewReasonText(reason.value, state.value));

function publish(value, why) { state.value = value; reason.value = why; emit('state', value); }
function visibility() {
  session?.setVisibility(props.visible && intersecting.value && documentVisible.value && externalState.value === '', props.enabled);
}
function pageVisibility() {
  documentVisible.value = typeof document === 'undefined' || document.visibilityState !== 'hidden'; visibility();
}
function binding() {
  const changed = session?.setBinding({ capsule_id: props.capsuleId, feed_id: props.feed.feed_id, generation: props.feed.generation, preview_available: props.feed.preview_available, preview_revision: props.feed.preview_revision });
  if (changed) visibility();
}
watch(() => [props.capsuleId, props.feed.feed_id, props.feed.generation, props.feed.preview_available, props.feed.preview_revision], binding);
watch(() => [props.enabled, props.visible, externalState.value], visibility);
onMounted(() => {
  const scope = typeof window === 'undefined' ? {} : /** @type {{cafe?:unknown}} */ (/** @type {unknown} */ (window));
  session = createPreviewSession({
    bridge: scope.cafe,
    getDisplayMedia: constraints => {
      if (typeof navigator === 'undefined' || typeof navigator.mediaDevices?.getDisplayMedia !== 'function') return Promise.reject(new Error('preview_unavailable'));
      return navigator.mediaDevices.getDisplayMedia(constraints);
    },
    observeFrames: (stream, onFrame) => observePreviewFrames(video.value, stream, onFrame),
    onState: publish,
    onStream: stream => {
      if (!video.value) return;
      video.value.srcObject = stream;
      if (!stream) dimensions.value = { ...dimensions.value, videoWidth: 0, videoHeight: 0 };
      if (stream && typeof video.value.play === 'function') {
        const result = video.value.play();
        result?.catch(() => { session?.setVisibility(false, props.enabled); publish('no_preview', 'preview_denied'); });
      }
    },
    size: () => ({ width: surface.value?.clientWidth || 1, height: surface.value?.clientHeight || 1 }),
  });
  binding(); pageVisibility(); measure();
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', pageVisibility);
  if (typeof IntersectionObserver !== 'undefined' && surface.value) {
    intersection = new IntersectionObserver(entries => {
      intersecting.value = entries.some(entry => entry.isIntersecting && entry.intersectionRatio > 0); visibility();
    }, { threshold: 0.01 });
    intersection.observe(surface.value);
  }
  if (typeof ResizeObserver !== 'undefined' && surface.value) {
    resizeObserver = new ResizeObserver(() => {
      measure();
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
    <div :style="frameStyle">
    <video ref="video" :style="videoStyle" @loadedmetadata="measure" @resize="measure" :hidden="externalState !== ''" muted autoplay playsinline :aria-label="feed.role === 'camera' ? 'Camera window preview' : 'Anchored window preview'"></video>
    </div>
    <div v-if="state !== 'live' || externalState !== ''" class="cafe-preview-placeholder" role="status">
      <strong>{{ placeholder }}</strong><span>{{ note }}</span>
    </div>
  </div>
</template>
