'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { load, nodeOps, element, button, textOf, squash, findAll, settle } = require('../fixtures/capsule_control.js');

const windows = [
  { token: 'opaque:window-alpha/9007199254740993', title: 'Synthetic Alpha', exe: 'alpha.exe', generation: '9007199254740993', layout_revision: '7', rect: { width: 640, height: 480 } },
  { token: 'opaque:window-beta/9007199254740995', title: 'Synthetic Beta', exe: 'beta.exe', generation: '9007199254740995', layout_revision: '8', rect: { width: 800, height: 600 } },
];

async function mountBrowser(overrides = {}, clipboard) {
  const ui = await load();
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: clipboard ? { clipboard } : {} });
  const events = [];
  const props = ui.shallowRef({ windows, currentPage: { offset: 0, nextOffset: null, total: windows.length }, total: windows.length,
    status: '', busy: false, selectedId: '', ...overrides });
  const handlers = Object.fromEntries(['select','refresh','prev','next','close','pair'].map(event =>
    ['on' + event[0].toUpperCase() + event.slice(1), (...args) => events.push([event, ...args])]));
  const tree = element('root');
  const app = ui.createRenderer(nodeOps).createApp({ render: () => ui.h(ui.WindowBrowser, { ...props.value, ...handlers }) });
  app.mount(tree); await settle(ui.nextTick);
  return { ui, tree, events,
    async update(patch) { props.value = { ...props.value, ...patch }; await settle(ui.nextTick); },
    async click(label) { const target = button(tree, label); assert.ok(target, 'button missing: ' + label); assert.notEqual(target.props.disabled, true); await target.props.onClick(); await settle(ui.nextTick); },
    done() { app.unmount(); if (descriptor) Object.defineProperty(globalThis, 'navigator', descriptor); else delete globalThis.navigator; },
  };
}

const rows = tree => findAll(tree, node => node.tag === 'button' && typeof node.props.class === 'string' && node.props.class.split(' ').includes('cafe-window-row'));
const input = (tree, type) => findAll(tree, node => node.tag === 'input' && node.props.type === type)[0];
async function setModel(m, target, value) { assert.ok(target); assert.equal(typeof target.props['onUpdate:modelValue'], 'function'); target.props['onUpdate:modelValue'](value); await settle(m.ui.nextTick); }

test('search filters the complete parent-provided list locally without authority or clipboard effects', async () => {
  let writes = 0;
  const supplied = [...windows, { ...windows[1], token: 'opaque:discord-third-page', title: 'Synthetic remote guest', exe: 'Discord.exe' }];
  const m = await mountBrowser({ windows: supplied, total: 3, status: 'ready' }, { writeText: async () => { writes += 1; } });
  try {
    assert.equal(rows(m.tree).length, 3);
    await setModel(m, input(m.tree, 'search'), 'remote guest');
    assert.equal(rows(m.tree).length, 1); assert.match(textOf(rows(m.tree)[0]), /opaque:discord-third-page/);
    await setModel(m, input(m.tree, 'search'), '');
    await setModel(m, input(m.tree, 'checkbox'), true);
    assert.equal(rows(m.tree).length, 1); assert.match(textOf(rows(m.tree)[0]), /Discord.exe/);
    assert.deepEqual(m.events, []); assert.equal(writes, 0);
  } finally { m.done(); }
});

test('selection and clipboard retain the exact opaque ID and never emit pairing implicitly', async () => {
  const writes = [];
  const m = await mountBrowser({ status: 'ready' }, { writeText: async token => { writes.push(token); } });
  try {
    assert.equal(button(m.tree, 'Copiar identificador'), undefined);
    await rows(m.tree)[0].props.onClick(); await settle(m.ui.nextTick);
    assert.deepEqual(m.events, [['select', windows[0].token]]);
    await m.update({ selectedId: windows[0].token });
    const field = findAll(m.tree, node => node.props.id === 'cafe-window-token')[0];
    assert.equal(field.props.value, windows[0].token); assert.equal(field.props.readonly, '');
    assert.deepEqual(writes, []);
    await m.click('Copiar identificador');
    assert.deepEqual(writes, [windows[0].token]);
    assert.match(squash(textOf(m.tree)), /Identificador copiado/);
    assert.deepEqual(m.events, [['select', windows[0].token]]);
    await m.click('Emparejar ventana'); assert.deepEqual(m.events[1], ['pair']);
  } finally { m.done(); }
});

test('clipboard failure offers the selected exact ID without a false copied claim or action', async () => {
  const m = await mountBrowser({ selectedId: windows[1].token, status: 'ready' }, { writeText: async () => { throw new Error('PRIVATE_CLIPBOARD_CANARY'); } });
  try {
    await m.click('Copiar identificador');
    const text = squash(textOf(m.tree));
    assert.match(text, /Selecciona el identificador y pulsa Ctrl\+C\./);
    assert.doesNotMatch(text, /Identificador copiado|PRIVATE_CLIPBOARD_CANARY/);
    assert.equal(findAll(m.tree, node => node.props.id === 'cafe-window-token')[0].props.value, windows[1].token);
    assert.deepEqual(m.events, []);
  } finally { m.done(); }
});

test('removed selection and incomplete or unavailable listing never invent success', async () => {
  const m = await mountBrowser({ selectedId: windows[0].token, status: 'ready' });
  try {
    await m.update({ windows: [windows[1]], status: 'selection_stale' });
    assert.match(squash(textOf(m.tree)), /La ventana cambió o se cerró/);
    assert.equal(button(m.tree, 'Copiar identificador'), undefined);
    assert.equal(button(m.tree, 'Emparejar ventana').props.disabled, true);
    await m.update({ status: 'incomplete', total: null });
    assert.match(squash(textOf(m.tree)), /Listado parcial/); assert.match(squash(textOf(m.tree)), /total sin confirmar/);
    await m.update({ windows: [], status: 'unavailable', total: null });
    assert.match(squash(textOf(m.tree)), /Café no está conectado/);
    assert.doesNotMatch(squash(textOf(m.tree)), /Ventanas actualizadas|No hay ventanas que coincidan|Identificador copiado/);
    assert.deepEqual(m.events, []);
  } finally { m.done(); }
});

test('clipboard completion after selection disappears cannot claim copied success', async () => {
  let finish; const pending = new Promise(resolve => { finish = resolve; });
  const m = await mountBrowser({ selectedId: windows[0].token, status: 'ready' }, { writeText: () => pending });
  try {
    const copying = button(m.tree, 'Copiar identificador').props.onClick();
    await m.update({ windows: [], selectedId: '', status: 'selection_stale' });
    finish(); await copying; await settle(m.ui.nextTick);
    assert.doesNotMatch(squash(textOf(m.tree)), /Identificador copiado/); assert.deepEqual(m.events, []);
  } finally { m.done(); }
});
