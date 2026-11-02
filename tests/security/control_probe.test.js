'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs');
const { parse } = require('@vue/compiler-sfc');
const { mount: workspaceMount, button, panel, textOf, squash, settle, APP } = require('../fixtures/capsule_control.js');
const CANARY = 'PRIVATE_DIAG_CANARY';
const SHOW = 'Show diagnostics', REFRESH = 'Refresh diagnostics', CLOSE = 'Close diagnostics';
const mount = getSnapshot => workspaceMount(getSnapshot, { page: 'diagnostics' });

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
    assert.ok(text.includes('listed_window_count: no data'), 'missing evidence stays no-data');
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
    assert.ok(text.includes('Diagnostics unavailable'), text);
    assert.doesNotMatch(text, /result_code|obs\.json|hwnd/);
    assert.equal(textOf(m.tree).includes(CANARY), false);
    assert.equal(m.calls.intents, 0);
    m.ui.probe.fail = false;
    await m.click(REFRESH);
    assert.ok(squash(textOf(panel(m.tree))).includes('result_code: pair_window_ambiguous'));
  } finally { m.done(); }
});
