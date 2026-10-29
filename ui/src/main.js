// @ts-check
/**
 * Cafe renderer entry (presentation-only).
 *
 * Installs OpenVue unstyled (no preset or dynamic theme-style engine), loads
 * the parent-owned token adapter before the application skin, and mounts the
 * workspace on the host-supplied `#app` element.
 *
 * @module ui/main
 */
import { createApp } from 'vue';
import OpenVue from 'openvue/config';
import './design/tokens.cafe.css';
import './cafe.css';
import App from './App.vue';

const app = createApp(App);
app.use(OpenVue, { unstyled: true });
app.mount('#app');
