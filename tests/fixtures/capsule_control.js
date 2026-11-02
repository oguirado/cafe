'use strict';
// Diagnostics panel in the actual App.vue: compiled with @vue/compiler-sfc, mounted on an
// in-memory Vue renderer, driven only through its buttons. The real parent helper
// (src/renderer/control.js readDiagnostics) runs behind a counting pass-through.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { parse, compileScript } = require('@vue/compiler-sfc');

// Public Vue v-model checks these DOM types even with a custom renderer.
// This fixture root is neither a Document nor a ShadowRoot and has no focus.
class FixtureDocument {}
class FixtureShadowRoot {}
assert.equal(typeof globalThis.Document, 'undefined');
assert.equal(typeof globalThis.ShadowRoot, 'undefined');
globalThis.Document = FixtureDocument;
globalThis.ShadowRoot = FixtureShadowRoot;
test.after(() => { delete globalThis.Document; delete globalThis.ShadowRoot; });
const ROOT = path.resolve(__dirname, '../..');
const APP = path.join(ROOT, 'ui/src/App.vue');
const CONTROL = path.join(ROOT, 'src/renderer/control.js');
const VUE_MODULES = path.dirname(path.dirname(require.resolve('vue/package.json')));
const CANARY = 'PRIVATE_DIAG_CANARY';
const SHOW = 'Show diagnostics', REFRESH = 'Refresh diagnostics', CLOSE = 'Close diagnostics';

/** @param {string} code @param {string} from @param {string} to */
function swap(code, from, to) {
  const before = code;
  for (const quote of ["'", '"']) code = code.split(quote + from + quote).join(JSON.stringify(to));
  assert.notEqual(code, before, 'import not found: ' + from);
  return code;
}

/** @type {string|null} */ let fixtureRoot = null;
/** @type {Promise<any>|null} */ let loaded = null;
function load() {
  if (loaded) return loaded;
  loaded = (async () => {
    const root = fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-diagnostics-ui-'));
    fs.symlinkSync(VUE_MODULES, path.join(root, 'node_modules'), 'junction');
    const compiledFiles = new Map();
    function compileVue(file) {
      if (compiledFiles.has(file)) return compiledFiles.get(file);
      const target = 'authored-' + compiledFiles.size + '.mjs';
      compiledFiles.set(file, target);
      const { descriptor, errors } = parse(fs.readFileSync(file, 'utf8'), { filename: file });
      assert.equal(errors.length, 0, file);
      let code = compileScript(descriptor, { id: target, inlineTemplate: true,
        templateOptions: { compilerOptions: { hoistStatic: false } } }).content;
      code = code.replace(/import\s*['"][^'"]+\.css['"];?/g, '');
      const imports = [...code.matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g)].map(match => match[1]);
      for (const specifier of new Set(imports)) {
        if (specifier === 'dockview-vue') code = swap(code, specifier, './Dockview.stub.mjs');
        else if (specifier.startsWith('.')) {
          const source = path.resolve(path.dirname(file), specifier);
          let replacement;
          if (source === CONTROL) replacement = './control.mjs';
          else if (specifier.endsWith('/TopBar.vue')) replacement = './TopBar.stub.mjs';
          else if (specifier.endsWith('/StatusBar.vue')) replacement = './StatusBar.stub.mjs';
          else if (specifier.endsWith('.vue')) replacement = './' + compileVue(source);
          else replacement = pathToFileURL(source).href;
          code = swap(code, specifier, replacement);
        }
      }
      fs.writeFileSync(path.join(root, target), code);
      return target;
    }
    const appModule = compileVue(APP);
    const browserModule = compileVue(path.join(ROOT, 'ui/src/windows.vue'));
    // Only the third-party presentation shell is substituted. Authored grid,
    // reconciliation, panel component and parent slot execute unchanged.
    fs.writeFileSync(path.join(root, 'Dockview.stub.mjs'), `
import { defineComponent, shallowRef, h, onMounted } from 'vue';
export const presentations = [];
export const DockviewVue = defineComponent({
  props: ['components', 'tabComponents', 'disableFloatingGroups'], emits: ['ready'],
  setup(props, { emit }) {
    const panels = shallowRef([]); const groups = []; const removed = new Set();
    const api = {
      get panels() { return panels.value; },
      get groups() { return groups; },
      getPanel(id) { return panels.value.find(panel => panel.id === id); },
      addPanel(options) { const group = { header: { hidden: false }, model: { header: { hidden: false } } }; groups.push(group); const panel = { ...options, group, api: { moveTo() {}, setSize() {}, setTitle(t) { panel.title = t; } } }; panels.value = [...panels.value, panel]; return panel; },
      removePanel(panel) { panels.value = panels.value.filter(item => item !== panel); for (const listener of removed) listener({ panel }); },
      onDidRemovePanel(listener) { removed.add(listener); return { dispose() { removed.delete(listener); } }; },
    };
    presentations.push(api);
    onMounted(() => emit('ready', { api }));
    return () => h('div', { 'data-fixture-dockview': true }, panels.value.map(panel =>
      h(props.components[panel.component], { key: panel.id, params: { params: panel.params, api: {}, containerApi: api } })));
  },
});
`);
    fs.writeFileSync(path.join(root, 'TopBar.stub.mjs'), "export default { props: ['product'], render: () => null };\n");
    fs.writeFileSync(path.join(root, 'StatusBar.stub.mjs'), "export default { props: ['shortcuts', 'crumbs', 'telemetry'], render: () => null };\n");
    fs.writeFileSync(path.join(root, 'control.mjs'), [
      `import * as real from ${JSON.stringify(pathToFileURL(CONTROL).href)};`,
      'export const probe = { reads: 0, facades: [], fail: false, actions: [] };',
      'export function createControls(facade) { const controls=real.createControls(facade); return {...controls, startRecording(id){ probe.actions.push(id); return controls.startRecording(id); }}; }',
      'export function readDiagnostics(facade) {',
      '  probe.reads += 1; probe.facades.push(facade);',
      `  if (probe.fail) throw new Error(${JSON.stringify(CANARY + ' synthetic/path/obs.json hwnd 0x1F2E')});`,
      '  return real.readDiagnostics(facade);',
      '}', ''].join('\n'));
    fs.writeFileSync(path.join(root, 'runtime.mjs'), "export { createRenderer, nextTick, h, shallowRef } from 'vue';\n");
    const url = (/** @type {string} */ rel) => pathToFileURL(path.join(root, rel)).href;
    const [app, control, runtime] = await Promise.all([import(url(appModule)), import(url('control.mjs')), import(url('runtime.mjs'))]);
    const browser = await import(url(browserModule));
    const dockview = await import(url('Dockview.stub.mjs'));
    return { App: app.default, WindowBrowser: browser.default, presentations: dockview.presentations, probe: control.probe, ...runtime };
  })();
  return loaded;
}
test.after(() => { if (fixtureRoot) fs.rmSync(fixtureRoot, { recursive: true, force: true }); });

/** Minimal in-memory tree for Vue's public custom-renderer API. */
function element(tag) {
  return { kind: 'element', tag, tagName: tag.toUpperCase(), parent: null, children: [], props: {}, value: '', options: [], style: { display: '' },
    getRootNode() { let node = this; while(node.parent) node = node.parent; return node; },
    addEventListener() {}, removeEventListener() {} };
}
const nodeOps = {
  createElement: (/** @type {string} */ tag) => element(tag),
  createText: (/** @type {string} */ text) => ({ kind: 'text', text, parent: null }),
  createComment: (/** @type {string} */ text) => ({ kind: 'comment', text, parent: null }),
  setText(/** @type {any} */ node, /** @type {string} */ text) { node.text = text; },
  setElementText(/** @type {any} */ el, /** @type {string} */ text) {
    for (const child of el.children) child.parent = null;
    el.children = text === '' ? [] : [{ kind: 'text', text, parent: el }];
  },
  insert(/** @type {any} */ child, /** @type {any} */ parent, /** @type {any} */ anchor) {
    if (child.parent) nodeOps.remove(child);
    const at = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(at < 0 ? parent.children.length : at, 0, child);
    child.parent = parent;
  },
  remove(/** @type {any} */ child) {
    if (!child.parent) return;
    const siblings = child.parent.children; const at = siblings.indexOf(child);
    if (at >= 0) siblings.splice(at, 1);
    child.parent = null;
  },
  parentNode: (/** @type {any} */ node) => node.parent,
  nextSibling(/** @type {any} */ node) {
    if (!node.parent) return null;
    const siblings = node.parent.children; return siblings[siblings.indexOf(node) + 1] || null;
  },
  querySelector: () => null,
  patchProp(/** @type {any} */ el, /** @type {string} */ key, /** @type {any} */ _prev, /** @type {any} */ next) { el.props[key] = next; },
};

/** @param {any} node @returns {string} */
function textOf(node) {
  if (node.kind === 'text') return node.text;
  if (node.kind !== 'element') return '';
  return node.children.map(textOf).join(' ');
}
const squash = (/** @type {string} */ s) => s.replace(/\s+/g, ' ').trim();
/** @param {any} node @param {(n:any)=>boolean} match @param {any[]} [out] */
function findAll(node, match, out = []) {
  if (node.kind === 'element') { if (match(node)) out.push(node); for (const c of node.children) findAll(c, match, out); }
  return out;
}
const buttons = (/** @type {any} */ tree) => findAll(tree, n => n.tag === 'button');
function button(/** @type {any} */ tree, /** @type {string} */ label) {
  return buttons(tree).find(b => squash(textOf(b)) === label);
}
const panel = (/** @type {any} */ tree) => findAll(tree, n => n.props.id === 'cafe-diagnostics-panel')[0];

async function settle(/** @type {() => Promise<void>} */ nextTick) {
  for (let i = 0; i < 10; i += 1) await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}

/** Mount the actual App with `facade` as window.cafe; every bridge call is counted. */
async function mount(/** @type {(()=>Promise<any>)} */ getSnapshot, options = {}) {
  const ui = await load();
  ui.presentations.length = 0;
  ui.probe.reads = 0; ui.probe.facades.length = 0; ui.probe.fail = false; ui.probe.actions.length = 0;
  const calls = { snapshots: 0, intents: 0, requests: [], detached: 0 };
  let observer = null;
  const facade = Object.freeze({
    getSnapshot: async () => { calls.snapshots += 1; return getSnapshot(); },
    onSnapshot: (handler) => { observer = handler; return () => { calls.detached += 1; }; },
    submitIntent: (request) => { calls.intents += 1; calls.requests.push(request); if(options.submit) return options.submit(request); throw new Error('MUTATION'); },
  });
  /** @type {any} */ (globalThis).window = { cafe: facade };
  const tree = element('root');
  const app = ui.createRenderer(nodeOps).createApp(ui.App);
  app.mount(tree);
  await settle(ui.nextTick);
  const click = async (/** @type {string} */ label) => {
    const target = button(tree, label);
    assert.ok(target, 'button missing: ' + label);
    assert.notEqual(target.props.disabled, true, 'button disabled: ' + label);
    await target.props.onClick();
    await settle(ui.nextTick);
  };
  const done = () => { app.unmount(); delete /** @type {any} */ (globalThis).window; };
  if(options.page === 'diagnostics') { await click('Help'); await click('Diagnostics'); }
  const push = async (snapshot, wait = true) => { assert.ok(observer); observer(snapshot); if(wait) await settle(ui.nextTick); };
  return { ui, tree, calls, facade, click, done, push };
}


module.exports = { load, nodeOps, element, mount, button, buttons, panel, textOf, squash, findAll, settle, ROOT, APP };
