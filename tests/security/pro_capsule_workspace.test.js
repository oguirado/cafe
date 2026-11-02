'use strict';
// Presentation tests only: synthetic native rows prove rendered text and emitted
// intents, never native capture, recording, OBS, approval or Windows execution.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '../..');
const COMPONENT = path.join(ROOT, 'ui/src/pro_bridge/CapsuleWorkspace.vue');
const MODEL = path.join(ROOT, 'ui/src/pro_bridge/capsule_workspace.js');
const EVENTS = ['choose-window', 'record', 'stop', 'obs', 'choose-folder'];
const LISTENERS = ['onChooseWindow', 'onRecord', 'onStop', 'onObs', 'onChooseFolder'];
const PROPS = ['capsules', 'availability', 'platform', 'folderConfigured',
  'ffmpegConfigured', 'obsReady', 'recordings', 'pendingIds'];
const clone = value => structuredClone(value);
const capsule = (suffix = '1') => ({ capsule_id: 'cap-demo-' + suffix,
  slot: 'slot-' + suffix, person_label: 'Synthetic user ' + suffix, generation: '1',
  feeds: [{ feed_id: 'game-demo-' + suffix, role: 'game', generation: '1' }] });
const snapshot = () => ({ capsules: [capsule()], availability: 'available', platform: 'windows',
  folderConfigured: true, ffmpegConfigured: true, obsReady: true, recordings: [], pendingIds: [] });
const running = () => [{ capsule_id: 'cap-demo-1', state: 'Running', feeds: [
  { feed_id: 'game-demo-1', state: 'Running', written_frames: null, backpressure: '4' },
] }];

let modelPromise;
function model() {
  // No package installation or module-manifest change is needed for pure tests.
  modelPromise ??= import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(MODEL, 'utf8')).toString('base64'));
  return modelPromise;
}

test('validator normalizes the complete closed input without aliasing caller rows', async () => {
  const api = await model();
  assert.deepEqual(Object.keys(api), ['validateCapsules']);
  const input = [capsule(), capsule('2')];
  input[0].slot = 'slot-0'; input[1].slot = 'slot-01'; input[1].person_label = null;
  const result = api.validateCapsules(input);
  assert.equal(result.ok, true); assert.deepEqual(result.rows, input);
  assert.notEqual(result.rows, input); assert.notEqual(result.rows[0], input[0]);
  assert.notEqual(result.rows[0].feeds[0], input[0].feeds[0]);
  assert.deepEqual(api.validateCapsules([]), { ok: true, rows: [] });
});

test('validator keeps canonical u64 clocks exact and rejects coercion', async () => {
  const { validateCapsules } = await model();
  for (const value of ['0', '1', '9007199254740993', '18446744073709551615']) {
    const rows = [capsule()]; rows[0].generation = value; rows[0].feeds[0].generation = value;
    const result = validateCapsules(rows);
    assert.equal(result.ok, true); assert.equal(result.rows[0].generation, value);
    assert.equal(result.rows[0].feeds[0].generation, value);
  }
  for (const value of ['', '01', '1.0', '1e1', '-1', ' 1', '1 ', '18446744073709551616',
    '99999999999999999999', '100000000000000000000', null, 1, true, 1n]) {
    for (const target of ['capsule', 'feed']) {
      const rows = [capsule()];
      (target === 'capsule' ? rows[0] : rows[0].feeds[0]).generation = value;
      assert.deepEqual(validateCapsules(rows), { ok: false, rows: [] });
    }
  }
});

test('validator rejects missing fields, extras, wrong types and identifier ambiguity', async () => {
  const { validateCapsules } = await model();
  const invalid = [null, undefined, {}, true, [capsule(), capsule(), capsule('3')]];
  for (const key of Object.keys(capsule())) {
    const row = capsule(); delete row[key]; invalid.push([row]);
  }
  for (const key of Object.keys(capsule().feeds[0])) {
    const row = capsule(); delete row.feeds[0][key]; invalid.push([row]);
  }
  const mutate = edit => { const rows = [capsule(), capsule('2')]; edit(rows); invalid.push(rows); };
  for (const id of ['', '-bad', 'bad id', 'a'.repeat(97), true, null]) mutate(r => { r[0].capsule_id = id; });
  mutate(r => { r[0].extra = true; }); mutate(r => { r[0].feeds[0].extra = true; });
  mutate(r => { r[0].person_label = 1; }); mutate(r => { r[0].slot = 'other-session'; });
  mutate(r => { r[0].feeds = null; }); mutate(r => { r[0].feeds[0].role = 'screen'; });
  mutate(r => { r[1].capsule_id = r[0].capsule_id; });
  mutate(r => { r[1].feeds[0].feed_id = r[0].feeds[0].feed_id; });
  mutate(r => { r[0].feeds[0].feed_id = r[1].capsule_id; });
  mutate(r => { r[1].slot = r[0].slot; });
  mutate(r => { r[0].feeds.push({ feed_id: 'game-demo-extra', role: 'game', generation: '1' }); });
  for (const input of invalid) assert.deepEqual(validateCapsules(input), { ok: false, rows: [] });
  const exact = capsule(); exact.capsule_id = 'a'.repeat(96);
  assert.equal(validateCapsules([exact]).ok, true);
});

test('validator refuses accessors, sparse arrays and hidden or symbol fields', async () => {
  const { validateCapsules } = await model();
  let reads = 0;
  const getter = capsule(); Object.defineProperty(getter, 'capsule_id', { enumerable: true,
    get() { reads += 1; throw new Error('synthetic-refusal'); } });
  const symbol = capsule(); symbol[Symbol('synthetic')] = true;
  const hidden = capsule(); Object.defineProperty(hidden, 'extra', { value: true });
  const array = [capsule()]; array.extra = true;
  for (const input of [[getter], [symbol], [hidden], array, Array(1)]) {
    assert.deepEqual(validateCapsules(input), { ok: false, rows: [] });
  }
  assert.equal(reads, 0, 'validation must not execute property getters');
});

let fixtureRoot, uiPromise;
async function loadUi() {
  if (uiPromise) return uiPromise;
  uiPromise = (async () => {
    // Lazy loading deliberately leaves missing approved dependencies as a real
    // test failure; selecting validator tests never silently skips component tests.
    const { parse, compileScript } = require('vue/compiler-sfc');
    const source = fs.readFileSync(COMPONENT, 'utf8');
    const { descriptor, errors } = parse(source, { filename: COMPONENT });
    assert.deepEqual(errors, []);
    assert.equal(descriptor.scriptSetup?.lang, undefined);
    const compiled = compileScript(descriptor, { id: 'pro-capsule-workspace', inlineTemplate: true,
      templateOptions: { compilerOptions: { hoistStatic: false } } });
    const root = fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-pro-capsule-'));
    const modules = path.dirname(path.dirname(require.resolve('vue/package.json')));
    fs.symlinkSync(modules, path.join(root, 'node_modules'), 'junction');
    const code = compiled.content.replace(/(['"])\.\/capsule_workspace\.js\1/g, "'./capsule_workspace.mjs'");
    assert.notEqual(code, compiled.content, 'real validator import must be preserved');
    fs.writeFileSync(path.join(root, 'CapsuleWorkspace.mjs'), code);
    fs.writeFileSync(path.join(root, 'capsule_workspace.mjs'), fs.readFileSync(MODEL, 'utf8'));
    fs.writeFileSync(path.join(root, 'runtime.mjs'), "export { createRenderer, h, nextTick } from 'vue';\n");
    const [component, runtime] = await Promise.all(['CapsuleWorkspace.mjs', 'runtime.mjs']
      .map(file => import(pathToFileURL(path.join(root, file)).href)));
    return { Component: component.default, descriptor, ...runtime };
  })();
  return uiPromise;
}
test.after(() => { if (fixtureRoot) fs.rmSync(fixtureRoot, { recursive: true, force: true }); });

function element(tag) { return { kind: 'element', tag, parent: null, children: [], props: {} }; }
const nodeOps = {
  createElement: element,
  createText: text => ({ kind: 'text', text, parent: null }),
  createComment: text => ({ kind: 'comment', text, parent: null }),
  setText(node, text) { node.text = text; },
  setElementText(node, text) {
    for (const child of node.children) child.parent = null;
    node.children = text === '' ? [] : [{ kind: 'text', text, parent: node }];
  },
  insert(child, parent, anchor) {
    if (child.parent) nodeOps.remove(child);
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent;
  },
  remove(child) {
    if (!child.parent) return;
    const siblings = child.parent.children, index = siblings.indexOf(child);
    if (index >= 0) siblings.splice(index, 1); child.parent = null;
  },
  parentNode: node => node.parent,
  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null; },
  patchProp(node, key, _old, value) { node.props[key] = value; },
  querySelector: () => null,
};
function textOf(node) {
  return node.kind === 'text' ? node.text : node.kind === 'element' ? node.children.map(textOf).join(' ') : '';
}
function findAll(node, predicate, out = []) {
  if (node.kind === 'element') {
    if (predicate(node)) out.push(node);
    for (const child of node.children) findAll(child, predicate, out);
  }
  return out;
}
function button(tree, label, id) {
  const scope = id ? findAll(tree, n => n.tag === 'article' && n.props['data-capsule-id'] === id)[0] : tree;
  assert.ok(scope, 'synthetic card must exist');
  return findAll(scope, n => n.tag === 'button' && textOf(n).replace(/\s+/g, ' ').trim() === label)[0];
}
async function mount(initial = snapshot(), listenerOverrides = {}) {
  const ui = await loadUi(), tree = element('root'), events = [];
  const renderer = ui.createRenderer(nodeOps);
  const listeners = Object.fromEntries(LISTENERS.map((key, index) => [key, (...args) => {
    events.push([EVENTS[index], args]);
  }]));
  Object.assign(listeners, listenerOverrides);
  let vnode;
  const render = props => { vnode = ui.h(ui.Component, { ...props, ...listeners }); renderer.render(vnode, tree); };
  render(initial); await ui.nextTick();
  const click = async (label, id) => {
    const target = button(tree, label, id); assert.ok(target, 'expected control: ' + label);
    assert.notEqual(target.props.disabled, true, 'control should be enabled: ' + label);
    target.props.onClick(); await ui.nextTick();
  };
  return { ui, tree, events, click, listeners, tamper: change => change(vnode.props),
    update: async props => { render(props); await ui.nextTick(); },
    done: () => renderer.render(null, tree) };
}

test('component exposes only the frozen props and events and renders truthful cards', async () => {
  const props = snapshot(); props.capsules.push(capsule('2')); props.recordings = running();
  const m = await mount(props);
  try {
    const keys = value => Array.isArray(value) ? [...value] : Object.keys(value);
    assert.deepEqual(keys(m.ui.Component.props).sort(), [...PROPS].sort());
    assert.deepEqual(keys(m.ui.Component.emits).sort(), [...EVENTS].sort());
    assert.equal(findAll(m.tree, n => n.tag === 'article').length, 2);
    const text = textOf(m.tree);
    for (const value of ['slot-1', 'slot-2', 'Synthetic user 1', 'Synthetic user 2', 'game-demo-1', 'Running', '4']) {
      assert.ok(text.includes(value), 'native presentation value missing');
    }
    assert.match(text, /unknown/i); assert.match(text, /preview.*unavailable|no.*preview/i);
    assert.equal(findAll(m.tree, n => ['video', 'img', 'canvas', 'iframe'].includes(n.tag)).length, 0);
    assert.equal(m.events.length, 0);
  } finally { m.done(); }
});

for (const role of ['game', 'camera']) test('component emits exact choose-window role ' + role, async () => {
  const m = await mount();
  try {
    await m.click('Choose ' + role + ' window');
    assert.deepEqual(m.events, [['choose-window', [{ capsule_id: 'cap-demo-1', role }]]]);
  } finally { m.done(); }
});

test('component emits choose-folder with no payload', async () => {
  const m = await mount();
  try { await m.click('Choose folder'); assert.deepEqual(m.events, [['choose-folder', []]]); }
  finally { m.done(); }
});

for (const label of ['Choose folder', 'Choose camera window']) {
  test('component immediate double click emits one intent: ' + label, async () => {
    const m = await mount();
    try {
      const click = button(m.tree, label).props.onClick; click(); click(); await m.ui.nextTick();
      assert.equal(m.events.length, 1);
      const next = snapshot(); await m.update(next); await m.click(label); assert.equal(m.events.length, 2);
    } finally { m.done(); }
  });
}

for (const [label, name] of [['REC', 'record'], ['Stop', 'stop'], ['OBS', 'obs']]) {
  test('component ' + name + ' requires explicit Confirm and emits exactly once', async () => {
    const props = snapshot(); if (name === 'stop') props.recordings = running();
    const m = await mount(props);
    try {
      await m.click(label); assert.equal(m.events.length, 0);
      assert.ok(button(m.tree, 'Reject'));
      const confirm = button(m.tree, 'Confirm').props.onClick;
      confirm(); confirm(); await m.ui.nextTick();
      assert.deepEqual(m.events, [[name, [{ capsule_id: 'cap-demo-1' }]]]);
    } finally { m.done(); }
  });
}

test('component Reject and an obsolete replaced dialog handler emit nothing', async () => {
  const m = await mount();
  try {
    await m.click('REC'); const obsolete = button(m.tree, 'Confirm').props.onClick;
    await m.click('Reject'); await m.click('OBS');
    obsolete(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
    await m.click('Confirm'); assert.deepEqual(m.events, [['obs', [{ capsule_id: 'cap-demo-1' }]]]);
  } finally { m.done(); }
});

test('component replacing a live dialog invalidates its earlier Confirm callback', async () => {
  const m = await mount();
  try {
    await m.click('REC'); const obsolete = button(m.tree, 'Confirm').props.onClick;
    await m.click('OBS'); obsolete(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
    await m.click('Confirm'); assert.deepEqual(m.events, [['obs', [{ capsule_id: 'cap-demo-1' }]]]);
  } finally { m.done(); }
});

test('component waits for the native pending receipt cycle before another effect', async () => {
  const m = await mount();
  try {
    await m.click('REC'); await m.click('Confirm');
    const next = snapshot(); next.recordings = running(); await m.update(next);
    assert.equal(button(m.tree, 'Stop').props.disabled, true, 'publication alone cannot acknowledge an effect');
    const pending = clone(next); pending.pendingIds = ['cap-demo-1']; await m.update(pending);
    assert.equal(button(m.tree, 'Stop').props.disabled, true);
    await m.update(clone(next)); await m.click('Stop'); await m.click('Confirm');
    assert.deepEqual(m.events.map(row => row[0]), ['record', 'stop']);
  } finally { m.done(); }
});

test('component Confirm rereads raw config and native counters after in-place tampering', async () => {
  for (const [label, change] of [['REC', p => { p.folderConfigured = false; }],
    ['Stop', p => { p.recordings[0].feeds[0].written_frames = '01'; }]]) {
    const props = snapshot(); if (label === 'Stop') props.recordings = running();
    const m = await mount(props);
    try {
      await m.click(label); const confirm = button(m.tree, 'Confirm').props.onClick;
      m.tamper(change); confirm(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
    } finally { m.done(); }
  }
});

test('component pending receipt disables every capsule effect and forced handlers refuse', async () => {
  const props = snapshot(); props.pendingIds = ['cap-demo-1'];
  const m = await mount(props);
  try {
    for (const label of ['REC', 'Stop', 'OBS', 'Choose game window', 'Choose camera window']) {
      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
    }
    await m.ui.nextTick(); assert.equal(m.events.length, 0); assert.equal(button(m.tree, 'Confirm'), undefined);
  } finally { m.done(); }
});

test('component unavailable and unknown runtime show one shared reason, not empty capsules', async () => {
  for (const availability of ['unavailable', 'unknown', null]) {
    const props = snapshot(); props.availability = availability;
    const m = await mount(props);
    try {
      assert.match(textOf(m.tree), /Runtime unavailable/); assert.doesNotMatch(textOf(m.tree), /No capsules/);
      for (const target of findAll(m.tree, n => n.tag === 'button')) {
        assert.equal(target.props.disabled, true); target.props.onClick?.();
      }
      await m.ui.nextTick(); assert.equal(m.events.length, 0);
    } finally { m.done(); }
  }
});

test('component missing store or listener bindings is unavailable without throwing', async () => {
  for (const [props, overrides] of [[{}, {}], [snapshot(), { onRecord: undefined }]]) {
    const m = await mount(props, overrides);
    try {
      assert.match(textOf(m.tree), /Runtime unavailable/);
      for (const target of findAll(m.tree, n => n.tag === 'button')) target.props.onClick?.();
      await m.ui.nextTick(); assert.equal(m.events.length, 0);
    } finally { m.done(); }
  }
});

test('component refuses stale confirmation after runtime, identity, generation or state changes', async () => {
  const changes = [p => { p.availability = 'unavailable'; }, p => { p.capsules = []; },
    p => { p.capsules.push(clone(p.capsules[0])); }, p => { delete p.capsules[0].capsule_id; },
    p => { p.capsules[0].generation = '2'; }, p => { p.capsules[0].feeds[0].generation = '2'; },
    p => { p.capsules[0].feeds[0].role = 'camera'; },
    p => { p.recordings = running(); }, p => { p.pendingIds = ['cap-demo-1']; },
    p => { p.folderConfigured = null; }, p => { p.ffmpegConfigured = false; }];
  for (const change of changes) {
    const m = await mount();
    try {
      await m.click('REC'); const stale = button(m.tree, 'Confirm').props.onClick;
      const next = snapshot(); change(next); await m.update(next);
      stale(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
    } finally { m.done(); }
  }
});

test('component rejects saved render handlers and invalidates them on unmount', async () => {
  const m = await mount();
  const staleChoose = button(m.tree, 'Choose camera window').props.onClick;
  try {
    const next = snapshot(); next.capsules[0].generation = '2'; await m.update(next);
    staleChoose(); await m.ui.nextTick(); assert.equal(m.events.length, 0);
    await m.click('REC'); const staleConfirm = button(m.tree, 'Confirm').props.onClick;
    m.done(); staleConfirm(); staleChoose(); assert.equal(m.events.length, 0);
  } finally { m.done(); }
});

test('component keeps exact prefixed identities and never reuses a removed identity', async () => {
  const props = snapshot(); props.capsules[0].capsule_id = 'capsule-cap-demo-1';
  const m = await mount(props);
  try {
    await m.click('Choose camera window');
    assert.deepEqual(m.events, [['choose-window', [{ capsule_id: 'capsule-cap-demo-1', role: 'camera' }]]]);
    const removed = snapshot(); removed.capsules = []; await m.update(removed);
    await m.update(props);
    const retired = findAll(m.tree, n => n.tag === 'article' && n.props['data-capsule-id'] === 'capsule-cap-demo-1')[0];
    if (retired) for (const target of findAll(retired, n => n.tag === 'button')) target.props.onClick?.();
    await m.ui.nextTick(); assert.equal(m.events.length, 1);
  } finally { m.done(); }
});

test('component refuses a rolled-back generation even through current handlers', async () => {
  const m = await mount();
  try {
    const newer = snapshot(); newer.capsules[0].generation = '2'; await m.update(newer);
    await m.update(snapshot());
    for (const label of ['REC', 'OBS', 'Choose camera window']) {
      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
    }
    await m.ui.nextTick(); assert.equal(m.events.length, 0);
  } finally { m.done(); }
});

test('component tombstones a removed feed and refuses its reappearance', async () => {
  const m = await mount();
  try {
    const removed = snapshot(); removed.capsules[0].feeds = []; await m.update(removed);
    await m.update(snapshot());
    for (const label of ['REC', 'OBS', 'Choose game window']) {
      const target = button(m.tree, label); assert.equal(target.props.disabled, true); target.props.onClick();
    }
    await m.ui.nextTick(); assert.equal(m.events.length, 0);
  } finally { m.done(); }
});

test('component start, stop and OBS gates keep null and unknown states closed', async () => {
  for (const change of [p => { p.folderConfigured = null; }, p => { p.ffmpegConfigured = null; },
    p => { p.platform = 'unknown'; }, p => { p.capsules[0].feeds = []; },
    p => { p.recordings = [{ capsule_id: 'cap-demo-1', state: null, feeds: [] }]; }]) {
    const props = snapshot(); change(props); const m = await mount(props);
    try { assert.equal(button(m.tree, 'REC').props.disabled, true); assert.equal(m.events.length, 0); }
    finally { m.done(); }
  }
  const props = snapshot(); props.obsReady = null; const m = await mount(props);
  try {
    assert.equal(button(m.tree, 'OBS').props.disabled, true); assert.equal(button(m.tree, 'Stop').props.disabled, true);
  } finally { m.done(); }
});

test('component rejects malformed ancillary snapshots and noncanonical native counters', async () => {
  for (const change of [p => { p.pendingIds = ['cap-demo-1', 'cap-demo-1']; },
    p => { p.folderConfigured = 1; }, p => { p.availability = 'starting'; },
    p => { p.recordings = [...running(), ...running()]; },
    p => { p.recordings = running(); p.recordings[0].extra = true; },
    p => { p.recordings = running(); p.recordings[0].feeds[0].written_frames = '01'; },
    p => { p.recordings = running(); p.recordings[0].feeds[0].backpressure = true; }]) {
    const props = snapshot(); change(props); const m = await mount(props);
    try {
      assert.match(textOf(m.tree), /Runtime unavailable/);
      for (const target of findAll(m.tree, n => n.tag === 'button')) target.props.onClick?.();
      await m.ui.nextTick(); assert.equal(m.events.length, 0);
    } finally { m.done(); }
  }
});
