'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.resolve(__dirname, '../../ui/src/window_catalogue.js')).href);
const row = id => ({ token: `opaque/window:${id}`, generation: '9007199254740993', layout_revision: '18446744073709551615',
  title: `Synthetic ${id}`, exe: 'synthetic.exe', rect: { width: 640, height: 480 } });
const page = (offset, ids, total, nextOffset = null) => ({ availability: 'available', windows: ids.map(row), windowPage: { offset, total, nextOffset } });
function failed(catalogue) {
  assert.deepEqual(catalogue.getState(), { entries: [], total: null, nextOffset: null, complete: false, status: 'failed' });
}

test('unknown is distinct from confirmed empty; unavailable and reset clear owned data', async () => {
  const { createWindowCatalogue } = await load();
  const c = createWindowCatalogue();
  assert.deepEqual(c.getState(), { entries: [], total: null, nextOffset: null, complete: false, status: 'idle' });
  c.accept(page(0, [], 0));
  assert.deepEqual(c.getState(), { entries: [], total: 0, nextOffset: null, complete: true, status: 'ready' });
  c.accept(page(0, ['a'], 1));
  c.accept({ availability: 'unavailable', windows: [row('untrusted')], windowPage: { total: 0 } });
  assert.deepEqual(c.getState(), { entries: [], total: null, nextOffset: null, complete: false, status: 'unavailable' });
  c.accept(page(0, ['a'], 1));
  c.accept({ availability: 'starting' });
  assert.equal(c.getState().status, 'unavailable');
  assert.equal(c.getState().total, null);
  c.reset(); assert.equal(c.getState().status, 'idle');
});

test('serial pages prove complete coverage and retain exact opaque IDs and uint64 clocks', async () => {
  const { createWindowCatalogue } = await load();
  const c = createWindowCatalogue();
  const first = page(0, ['a', 'b', 'c'], 5, 3);
  c.accept(first);
  assert.equal(c.getState().status, 'loading');
  assert.equal(c.getState().complete, false);
  assert.equal(c.getState().nextOffset, 3);
  c.accept(first); assert.equal(c.getState().entries.length, 3);
  const last = page(3, ['d', 'e'], 5);
  c.accept(last); const result = c.getState();
  assert.equal(result.status, 'ready'); assert.equal(result.complete, true);
  assert.deepEqual(result.entries.map(value => value.pageOffset), [0, 0, 0, 3, 3]);
  assert.equal(result.entries[0].token, first.windows[0].token);
  assert.equal(result.entries[0].generation, '9007199254740993');
  assert.equal(result.entries[0].layout_revision, '18446744073709551615');
  c.accept(last); assert.deepEqual(c.getState(), result);
  first.windows[0].title = 'changed externally'; last.windows[0].rect.width = 1;
  result.entries[0].rect.width = 2; result.entries.pop();
  assert.equal(c.getState().entries.length, 5);
  assert.equal(c.getState().entries[0].rect.width, 640);
  assert.equal(c.getState().entries[0].title, 'Synthetic a');
  c.accept(page(0, ['replacement'], 1));
  assert.deepEqual(c.getState().entries.map(value => value.token), ['opaque/window:replacement']);
  const numericToken = page(0, ['digits'], 1);
  numericToken.windows[0].token = '009007199254740993';
  c.accept(numericToken);
  assert.equal(c.getState().entries[0].token, '009007199254740993');
});

test('cap is explicit incomplete including a partially accepted page', async () => {
  const { createWindowCatalogue } = await load();
  for (const limit of [1, 3, 4]) {
    const c = createWindowCatalogue(limit);
    c.accept(page(0, ['a', 'b', 'c'], 5, 3));
    if (limit === 4) c.accept(page(3, ['d', 'e'], 5));
    assert.equal(c.getState().entries.length, limit);
    assert.equal(c.getState().status, 'incomplete');
    assert.equal(c.getState().complete, false); assert.equal(c.getState().total, 5);
    assert.equal(c.getState().nextOffset, null);
  }
  const exact = createWindowCatalogue(3);
  exact.accept(page(0, ['a', 'b', 'c'], 3));
  assert.equal(exact.getState().complete, true);
  for (const bad of [0, -1, 257, NaN, Infinity, 1.5, '3', null, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => createWindowCatalogue(bad), RangeError);
  }
});

test('gaps, repeated noncurrent offsets, empty advances and changed totals fail closed', async () => {
  const { createWindowCatalogue } = await load();
  const cases = [
    page(4, ['e'], 5), page(3, ['d'], 6, 4), page(3, [], 5, 4),
    page(3, ['d'], 5, 3), page(3, ['d'], 5, 5), page(3, ['d'], 5),
    page(3, ['d'], 5, 4.5), page(3, ['d'], '5', 4),
    page(3, ['d'], Number.MAX_SAFE_INTEGER + 1, 4),
  ];
  for (const malformed of cases) {
    const c = createWindowCatalogue(); c.accept(page(0, ['a', 'b', 'c'], 5, 3));
    c.accept(malformed); failed(c);
  }
  const c = createWindowCatalogue();
  c.accept(page(3, ['d'], 4)); failed(c);
  c.accept(page(0, ['a'], 3, 1)); c.accept(page(1, ['b'], 3, 2));
  c.accept(page(2, ['c'], 3)); c.accept(page(1, ['b'], 3, 2)); failed(c);
});

test('duplicate identity, conflicting replay and malformed fields never produce completion', async () => {
  const { createWindowCatalogue } = await load();
  for (const mutation of [
    value => { value.windows.push(row('d')); },
    value => { value.windows = null; },
    value => { value.windows[0].generation = 9007199254740992; },
    value => { value.windows[0].generation = '01'; },
    value => { value.windows[0].generation = '0'; },
    value => { value.windows[0].layout_revision = '18446744073709551616'; },
    value => { value.windows[0].token = 42; },
    value => { value.windows[0].token = 'bad\0id'; },
    value => { value.windows[0].rect.width = -1; },
    value => { value.windows[0].rect.height = 4294967296; },
    value => { value.windows[0].title = null; },
    value => { value.windowPage.offset = '0'; },
    value => { value.windowPage.offset = -1; },
    value => { value.availability = 'unknown'; },
  ]) {
    const c = createWindowCatalogue(); const value = page(0, ['a', 'b', 'c'], 3);
    mutation(value); c.accept(value); failed(c);
  }
  for (const duplicate of [row('a'), { ...row('a'), generation: '2' }]) {
    const c = createWindowCatalogue(); c.accept(page(0, ['a'], 2, 1));
    c.accept({ ...page(1, ['b'], 2), windows: [duplicate] }); failed(c);
  }
  const samePage = createWindowCatalogue(); samePage.accept(page(0, ['a', 'a'], 2)); failed(samePage);
  const replay = createWindowCatalogue(); replay.accept(page(0, ['a'], 2, 1)); replay.accept(page(1, ['b'], 2));
  replay.accept(page(1, ['replacement'], 2)); failed(replay);
});
