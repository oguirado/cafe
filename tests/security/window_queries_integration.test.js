'use strict';
// Executes the compiled authored App/WindowBrowser and the actual controller.
// Snapshots are explicit authority observations; a queued reply supplies no page.
const test = require('node:test');
const assert = require('node:assert/strict');
const { mount, button, findAll, textOf, squash, settle } = require('../fixtures/capsule_control.js');

const EPOCH = '9007199254740993';
const GENERATION = '9007199254740995';
const LAYOUT = '18446744073709551615';
const ALPHA = 'opaque:window-alpha/9007199254740993';
const BETA = 'opaque:window-beta/9007199254740995';
function windowRow(handle) {
  return { handle, generation: GENERATION, layout_revision: LAYOUT,
    title: handle === ALPHA ? 'Synthetic Alpha' : 'Synthetic Beta',
    executable: 'synthetic.exe', class: 'SyntheticWindow', rect: { width: 640, height: 480 } };
}
function snapshot(revision = '1', offset = 0, windows = [], total = 0, next = null) {
  return { availability: 'available', authority_epoch: EPOCH, revision,
    state: { runtime: 'authority_ready', platform: 'windows',
      capsules: { 'cap-a': { capsule_id: 'cap-a', person_label: 'Synthetic Alpha', feeds: {} } },
      windows, window_page: { offset, total, next_offset: next }, last_result: { state: 'observed' },
      recordings: [], folder_configured: true, recording_configured: true,
      obs_available: false, obs_configurable: false, chat_available: false, chat_configurable: false } };
}
const rows = tree => findAll(tree, node => node.tag === 'button'
  && typeof node.props.class === 'string' && node.props.class.split(' ').includes('cafe-window-row'));
const bodies = m => m.calls.requests.map(request => request.body);
const pairBodies = m => bodies(m).filter(body => body.op === 'pair_window');
function observed(m, ...args) {
  const value = snapshot(...args);
  const request = m.calls.requests.at(-1);
  assert.equal(request.body.op, 'list_windows');
  value.state.last_result.request_id = request.request_id;
  return value;
}
async function mountApp() {
  return mount(async () => snapshot(), { submit: async () => ({ queued: true }) });
}
async function openAndCollect(m) {
  await m.click('Ventanas');
  assert.deepEqual(bodies(m), [{ op: 'list_windows', offset: 0 }]);
  // Queuing alone must neither finish the scan nor reveal the initial page.
  assert.equal(rows(m.tree).length, 0);
  assert.equal(button(m.tree, 'Actualizar').props.disabled, true);
  await m.push(observed(m, '2', 0, [windowRow(ALPHA)], 2, 1));
  assert.deepEqual(bodies(m), [{ op: 'list_windows', offset: 0 }, { op: 'list_windows', offset: 1 }]);
  assert.equal(button(m.tree, 'Actualizar').props.disabled, true);
  await m.push(observed(m, '3', 1, [windowRow(BETA)], 2));
}
async function selectAlpha(m) {
  const row = rows(m.tree).find(node => textOf(node).includes(ALPHA));
  assert.ok(row, 'cached first-page row remains selectable after the final page');
  assert.notEqual(row.props.disabled, true);
  await row.props.onClick();
  await settle(m.ui.nextTick);
}
async function beginPair(m) {
  const target = button(m.tree, 'Emparejar ventana');
  assert.ok(target);
  assert.notEqual(target.props.disabled, true);
  const pending = target.props.onClick();
  await settle(m.ui.nextTick);
  return { pending };
}

test('actual App collects all bounded pages before reporting the complete browser catalogue', async () => {
  const m = await mountApp();
  try {
    await openAndCollect(m);
    assert.equal(rows(m.tree).length, 2);
    assert.deepEqual(rows(m.tree).map(node => findAll(node, n => n.tag === 'code').map(textOf)[0]), [ALPHA, BETA]);
    assert.match(squash(textOf(m.tree)), /Ventanas actualizadas/);
    assert.match(squash(textOf(m.tree)), /2 coincidencias · 2 cargadas · total 2/);
    assert.equal(button(m.tree, 'Actualizar').props.disabled, false);
    assert.equal(pairBodies(m).length, 0);
    assert.ok(bodies(m).every(body => body.op === 'list_windows'));
  } finally { m.done(); }
});

test('cached first-page selection is refreshed and only then paired through the real controller', async () => {
  const m = await mountApp();
  try {
    await openAndCollect(m);
    await selectAlpha(m);
    assert.equal(pairBodies(m).length, 0);
    const { pending } = await beginPair(m);
    assert.deepEqual(bodies(m).map(body => [body.op, body.offset]), [
      ['list_windows', 0], ['list_windows', 1], ['list_windows', 0],
    ]);
    assert.equal(pairBodies(m).length, 0, 'queued refresh cannot authorize pairing');
    assert.equal(button(m.tree, 'Emparejar ventana').props.disabled, true);
    await m.push(observed(m, '4', 0, [windowRow(ALPHA)], 2, 1));
    await pending;
    await settle(m.ui.nextTick);
    assert.equal(pairBodies(m).length, 1);
    const pair = pairBodies(m)[0];
    assert.equal(pair.window_token, ALPHA);
    assert.equal(pair.window_generation, GENERATION);
    assert.equal(pair.layout_revision, LAYOUT);
    assert.equal(pair.capsule_id, 'cap-a');
    assert.equal(pair.person_label, 'Synthetic Alpha');
    assert.equal(pair.role, 'game');
    assert.deepEqual(pair.crop, { x: 0, y: 0, width: 640, height: 480 });
    assert.equal(m.calls.requests.at(-1).expected_revision, '4');
    assert.equal(m.calls.requests.at(-1).authority_epoch, EPOCH);
    assert.equal(findAll(m.tree, node => node.props.id === 'cafe-window-browser').length, 0);
    assert.ok(bodies(m).every(body => ['list_windows', 'pair_window'].includes(body.op)));
  } finally { m.done(); }
});

test('replayed observed result for the same offset cannot satisfy a fresh pair query', async () => {
  const m = await mountApp();
  try {
    await openAndCollect(m);
    await selectAlpha(m);
    const oldRequestId = m.calls.requests[0].request_id;
    const { pending } = await beginPair(m);
    const replay = snapshot('3', 0, [windowRow(ALPHA)], 2, 1);
    replay.state.last_result.request_id = oldRequestId;
    await m.push(replay);
    assert.equal(pairBodies(m).length, 0, 'same-offset observed replay has the wrong request identity');
    assert.equal(button(m.tree, 'Emparejar ventana').props.disabled, true);
    assert.deepEqual(bodies(m).map(body => body.op), ['list_windows', 'list_windows', 'list_windows']);
    // Listing is read-only: the matching observation need not increase revision.
    await m.push(observed(m, '3', 0, [windowRow(ALPHA)], 2, 1));
    await pending;
    await settle(m.ui.nextTick);
    assert.equal(pairBodies(m).length, 1);
    assert.equal(pairBodies(m)[0].window_token, ALPHA);
    assert.equal(m.calls.requests.at(-1).expected_revision, '3');
  } finally { m.done(); }
});

for (const change of ['generation', 'layout_revision', 'absent']) {
  test(`fresh page with ${change} changed rejects the cached identity without any pair effect`, async () => {
    const m = await mountApp();
    try {
      await openAndCollect(m);
      await selectAlpha(m);
      const { pending } = await beginPair(m);
      assert.equal(pairBodies(m).length, 0);
      const fresh = windowRow(ALPHA);
      if (change !== 'absent') fresh[change] = '2';
      await m.push(observed(m, '4', 0, change === 'absent' ? [windowRow(BETA)] : [fresh], 2, 1));
      await pending;
      await settle(m.ui.nextTick);
      assert.deepEqual(bodies(m), [
        { op: 'list_windows', offset: 0 }, { op: 'list_windows', offset: 1 }, { op: 'list_windows', offset: 0 },
      ]);
      assert.match(squash(textOf(m.tree)), /La ventana cambió o se cerró/);
      assert.equal(button(m.tree, 'Emparejar ventana').props.disabled, true);
      assert.equal(pairBodies(m).length, 0);
    } finally { m.done(); }
  });
}

test('availability lost midscan stops collection and cannot authorize effects from a partial cache', async () => {
  const m = await mountApp();
  try {
    await m.click('Ventanas');
    await m.push(observed(m, '2', 0, [windowRow(ALPHA)], 2, 1));
    assert.deepEqual(bodies(m), [{ op: 'list_windows', offset: 0 }, { op: 'list_windows', offset: 1 }]);
    await m.push({ availability: 'unavailable' });
    assert.match(squash(textOf(m.tree)), /No se pudo completar la búsqueda|Café no está conectado/);
    assert.doesNotMatch(squash(textOf(m.tree)), /Ventanas actualizadas/);
    assert.equal(button(m.tree, 'Emparejar ventana').props.disabled, true);
    assert.ok(rows(m.tree).every(row => row.props.disabled === true));
    assert.equal(pairBodies(m).length, 0);
    assert.ok(bodies(m).every(body => body.op === 'list_windows'));
    assert.equal(m.ui.probe.actions.length, 0);
    await m.click('Cerrar');
    assert.deepEqual(bodies(m), [{ op: 'list_windows', offset: 0 }, { op: 'list_windows', offset: 1 }]);
  } finally { m.done(); }
});
