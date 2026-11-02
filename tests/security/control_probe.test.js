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

const ROOT = path.resolve(__dirname, '../..');
const APP = path.join(ROOT, 'ui/src/App.vue');
const CONTROL = path.join(ROOT, 'src/renderer/control.js');
const VUE_MODULES = path.dirname(path.dirname(require.resolve('vue/package.json')));
const CANARY = 'PRIVATE_DIAG_CANARY';
const SHOW = 'Mostrar diagnóstico', REFRESH = 'Actualizar diagnóstico', CLOSE = 'Cerrar diagnóstico';

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
    const { descriptor, errors } = parse(fs.readFileSync(APP, 'utf8'), { filename: APP });
    assert.equal(errors.length, 0);
    const compiled = compileScript(descriptor, { id: 'cafe-diagnostics-ui', inlineTemplate: true,
      templateOptions: { compilerOptions: { hoistStatic: false } } });
    const root = fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-diagnostics-ui-'));
    // Same approved pattern as rendererCheck: link the installed Vue packages (junction needs no elevation).
    fs.symlinkSync(VUE_MODULES, path.join(root, 'node_modules'), 'junction');
    let code = compiled.content;
    code = swap(code, './kit/shell/TopBar.vue', './TopBar.stub.mjs');
    code = swap(code, './kit/shell/StatusBar.vue', './StatusBar.stub.mjs');
    code = swap(code, '../../src/renderer/control.js', './control.mjs');
    fs.writeFileSync(path.join(root, 'App.mjs'), code);
    fs.writeFileSync(path.join(root, 'TopBar.stub.mjs'), "export default { props: ['product'], render: () => null };\n");
    fs.writeFileSync(path.join(root, 'StatusBar.stub.mjs'), "export default { props: ['shortcuts', 'crumbs', 'telemetry'], render: () => null };\n");
    fs.writeFileSync(path.join(root, 'control.mjs'), [
      `import * as real from ${JSON.stringify(pathToFileURL(CONTROL).href)};`,
      'export const probe = { reads: 0, facades: [], fail: false };',
      'export const createControls = real.createControls;',
      'export function readDiagnostics(facade) {',
      '  probe.reads += 1; probe.facades.push(facade);',
      `  if (probe.fail) throw new Error(${JSON.stringify(CANARY + ' synthetic/path/obs.json hwnd 0x1F2E')});`,
      '  return real.readDiagnostics(facade);',
      '}', ''].join('\n'));
    fs.writeFileSync(path.join(root, 'runtime.mjs'), "export { createRenderer, nextTick } from 'vue';\n");
    const url = (/** @type {string} */ rel) => pathToFileURL(path.join(root, rel)).href;
    const [app, control, runtime] = await Promise.all([import(url('App.mjs')), import(url('control.mjs')), import(url('runtime.mjs'))]);
    return { App: app.default, probe: control.probe, ...runtime };
  })();
  return loaded;
}
test.after(() => { if (fixtureRoot) fs.rmSync(fixtureRoot, { recursive: true, force: true }); });

/** Minimal in-memory tree for Vue's public custom-renderer API. */
function element(tag) {
  return { kind: 'element', tag, parent: null, children: [], props: {}, value: '', options: [],
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
async function mount(/** @type {(()=>Promise<any>)} */ getSnapshot) {
  const ui = await load();
  ui.probe.reads = 0; ui.probe.facades.length = 0; ui.probe.fail = false;
  const calls = { snapshots: 0, intents: 0 };
  const facade = Object.freeze({
    getSnapshot: async () => { calls.snapshots += 1; return getSnapshot(); },
    onSnapshot: () => () => {},
    submitIntent: () => { calls.intents += 1; throw new Error('MUTATION'); },
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
  return { ui, tree, calls, facade, click, done };
}

const secretSnapshot = () => ({ availability: 'starting', state: {
  last_result: { state: 'rejected', code: 'pair_window_ambiguous' },
  capsules: { cap: { person_label: CANARY + '_ACCOUNT', feeds: { f: { role: 'game' } } } },
  windows: [
    { visible: true, minimized: false, title: CANARY + '_TITLE', handle: CANARY + '_HANDLE', pid: 4242, executable: 'C:\\' + CANARY + '\\game.exe' },
    { visible: false, minimized: true, title: CANARY + '_TITLE_2', password: CANARY + '_PASSWORD' },
  ] } });

test('App.vue template and script stay plain-text and timer/log free', () => {
  const { descriptor } = parse(fs.readFileSync(APP, 'utf8'), { filename: APP });
  const template = descriptor.template?.content ?? '', script = descriptor.scriptSetup?.content ?? '';
  for (const label of [SHOW, REFRESH, CLOSE]) assert.ok(template.includes(label), label);
  assert.doesNotMatch(template, /v-html|innerHTML|v-text/);
  assert.doesNotMatch(script, /\b(setTimeout|setInterval|requestAnimationFrame|console\.|clipboard|localStorage|sessionStorage|indexedDB|JSON\.stringify|openDevTools|ipcRenderer|require\s*\()/);
  assert.match(script, /import \{ createControls, readDiagnostics \} from '\.\.\/\.\.\/src\/renderer\/control\.js';/);
});

test('no diagnostics read at mount; explicit show and refresh render the fixed report', async () => {
  const m = await mount(async () => secretSnapshot());
  try {
    assert.equal(m.ui.probe.reads, 0);
    assert.equal(panel(m.tree), undefined);
    assert.ok(button(m.tree, SHOW));
    const atMount = m.calls.snapshots; // existing connect() read only
    await settle(m.ui.nextTick); await settle(m.ui.nextTick);
    assert.equal(m.ui.probe.reads, 0, 'no timer-driven reads');

    await m.click(SHOW);
    assert.equal(m.ui.probe.reads, 1);
    assert.equal(m.ui.probe.facades[0], m.facade, 'helper receives the closed facade itself');
    assert.equal(m.calls.snapshots, atMount + 1);
    const view = panel(m.tree);
    assert.ok(view, 'panel visible');
    const text = squash(textOf(view));
    for (const line of ['schema: cafe.diagnostic.v1', 'bridge: starting', 'result_state: rejected',
      'result_code: pair_window_ambiguous', 'capsule_count: 1', 'paired_feed_count: 1',
      'listed_window_count: 2', 'minimized_window_count: 1', 'visible_window_count: 1']) {
      assert.ok(text.includes(line), line);
    }
    assert.ok(button(m.tree, REFRESH) && button(m.tree, CLOSE) && !button(m.tree, SHOW));

    await m.click(REFRESH);
    assert.equal(m.ui.probe.reads, 2);
    assert.equal(m.calls.snapshots, atMount + 2);

    await m.click(CLOSE);
    assert.equal(panel(m.tree), undefined);
    assert.ok(button(m.tree, SHOW));
    assert.equal(m.ui.probe.reads, 2, 'close never reads');

    assert.equal(m.calls.intents, 0, 'submitIntent untouched');
    assert.equal(textOf(m.tree).includes(CANARY), false);
    assert.doesNotMatch(textOf(m.tree), /game\.exe|4242/);
  } finally { m.done(); }
});

test('secret-bearing bridge error and unknown codes never reach the panel', async () => {
  const m = await mount(async () => { throw new Error(CANARY + ' synthetic/path/obs.json hwnd 0x1F2E'); });
  try {
    await m.click(SHOW);
    let text = squash(textOf(panel(m.tree)));
    assert.ok(text.includes('bridge: unknown') && text.includes('result_code: unknown_result'), text);
    assert.ok(text.includes('listed_window_count: sin dato'), 'missing evidence stays no-data');
    assert.equal(textOf(m.tree).includes(CANARY), false);
    assert.doesNotMatch(textOf(m.tree), /obs\.json|hwnd|0x1F2E|Users/);
    assert.equal(m.calls.intents, 0);
  } finally { m.done(); }

  const n = await mount(async () => {
    const s = secretSnapshot(); s.state.last_result = { state: CANARY + '_STATE', code: CANARY + '_ERROR' }; return s;
  });
  try {
    await n.click(SHOW);
    const text = squash(textOf(panel(n.tree)));
    assert.ok(text.includes('result_state: unknown') && text.includes('result_code: unknown_result'), text);
    assert.equal(textOf(n.tree).includes(CANARY), false);
    assert.equal(n.calls.intents, 0);
  } finally { n.done(); }
});

test('a throwing diagnostics read shows only the fixed note', async () => {
  const m = await mount(async () => secretSnapshot());
  try {
    m.ui.probe.fail = true;
    await m.click(SHOW);
    const text = squash(textOf(panel(m.tree)));
    assert.ok(text.includes('Diagnóstico no disponible'), text);
    assert.doesNotMatch(text, /result_code|obs\.json|hwnd/);
    assert.equal(textOf(m.tree).includes(CANARY), false);
    assert.equal(m.calls.intents, 0);
    m.ui.probe.fail = false;
    await m.click(REFRESH);
    assert.ok(squash(textOf(panel(m.tree))).includes('result_code: pair_window_ambiguous'));
  } finally { m.done(); }
});
