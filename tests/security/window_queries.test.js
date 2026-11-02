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
    status: '', busy: false, selectedId: '', anchored: {}, ...overrides });
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
  const supplied = [
    ...windows,
    { ...windows[0], token: 'opaque:explorer-noise', title: 'Folder View', exe: 'C:\\Windows\\explorer.exe' },
    { ...windows[1], token: 'opaque:discord-third-page', title: 'Synthetic remote guest', exe: 'Discord.exe' },
  ];
  const m = await mountBrowser({ windows: supplied, total: 4, status: 'ready' }, { writeText: async () => { writes += 1; } });
  try {
    assert.equal(rows(m.tree).length, 3);
    assert.match(textOf(rows(m.tree)[0]), /Discord\.exe/);
    await setModel(m, input(m.tree, 'search'), 'remote guest');
    assert.equal(rows(m.tree).length, 1);
    assert.match(textOf(rows(m.tree)[0]), /Synthetic remote guest/);
    assert.equal(textOf(m.tree).includes('opaque:discord-third-page'), false);
    await setModel(m, input(m.tree, 'search'), '');
    await setModel(m, input(m.tree, 'checkbox'), true);
    assert.equal(rows(m.tree).length, 4);
    assert.ok(rows(m.tree).some(row => textOf(row).includes('explorer.exe')));
    assert.deepEqual(m.events, []); assert.equal(writes, 0);
  } finally { m.done(); }
});

test('selection emits the exact opaque ID once without exposing raw tokens in DOM text or touching clipboard', async () => {
  const writes = [];
  const m = await mountBrowser({ status: 'ready' }, { writeText: async token => { writes.push(token); } });
  try {
    assert.equal(button(m.tree, 'Copy identifier'), undefined);
    assert.equal(findAll(m.tree, node => node.tag === 'code').length, 0);
    assert.equal(textOf(m.tree).includes(windows[0].token), false);
    await rows(m.tree)[0].props.onClick(); await settle(m.ui.nextTick);
    assert.deepEqual(m.events, [['select', windows[0].token]]);
    await m.update({ selectedId: windows[0].token });
    assert.equal(rows(m.tree)[0].props['aria-pressed'], true);
    assert.ok(rows(m.tree)[0].props.class.includes('selected'));
    assert.equal(textOf(m.tree).includes(windows[0].token), false);
    assert.deepEqual(writes, []);
    assert.deepEqual(m.events, [['select', windows[0].token]]);
  } finally { m.done(); }
});

test('anchored or busy rows stay disabled and never emit selection actions', async () => {
  const m = await mountBrowser({
    selectedId: windows[1].token,
    status: 'ready',
    anchored: { [windows[1].token]: 'slot-1' },
  }, { writeText: async () => { throw new Error('PRIVATE_CLIPBOARD_CANARY'); } });
  try {
    const anchoredRow = rows(m.tree)[1];
    assert.equal(anchoredRow.props.disabled, true);
    assert.match(squash(textOf(anchoredRow)), /Anchored to slot-1/);
    await anchoredRow.props.onClick();
    await settle(m.ui.nextTick);
    assert.deepEqual(m.events, []);
    assert.doesNotMatch(squash(textOf(m.tree)), /PRIVATE_CLIPBOARD_CANARY/);
  } finally { m.done(); }
});

test('removed selection and incomplete or unavailable listing never invent success', async () => {
  const m = await mountBrowser({ selectedId: windows[0].token, status: 'ready' });
  try {
    await m.update({ windows: [windows[1]], status: 'selection_stale' });
    assert.match(squash(textOf(m.tree)), /The window changed or closed\. Select it again\./);
    await m.update({ status: 'incomplete', total: null });
    assert.match(squash(textOf(m.tree)), /Partial list\. Refresh to scan again\./);
    assert.match(squash(textOf(m.tree)), /total unconfirmed/);
    await m.update({ windows: [], status: 'unavailable', total: null });
    assert.match(squash(textOf(m.tree)), /Café is not connected\. Open the app before scanning windows\./);
    assert.doesNotMatch(squash(textOf(m.tree)), /Windows updated|No matching windows found/);
    assert.deepEqual(m.events, []);
  } finally { m.done(); }
});

test('duplicate, empty, null-byte or oversized window tokens fail closed and admit no selectable rows', async () => {
  for (const invalid of [
    [windows[0], { ...windows[1], token: windows[0].token }],
    [{ ...windows[0], token: '' }],
    [{ ...windows[0], token: 'bad\0token' }],
    [{ ...windows[0], token: 'x'.repeat(129) }],
  ]) {
    const m = await mountBrowser({ windows: invalid, status: 'ready' });
    try {
      assert.equal(rows(m.tree).length, 0);
      assert.deepEqual(m.events, []);
    } finally { m.done(); }
  }
});
