'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../ui/src/App.vue'), 'utf8');
const batchSource = source.slice(source.indexOf('async function onBatch(op)'), source.indexOf('async function onOrganizer('));

// Execute the production coordinator, with only the authority action boundary stubbed.
async function batch(op, results) {
  const calls = [];
  const state = { batchPending: { value: false }, batchNote: { value: '' }, appDisposed: false,
    capsules: { value: [{ capsule_id: 'cap-a' }, { capsule_id: 'cap-b' }] },
    recFor: () => op === 'stop' ? { state: 'Running' } : undefined,
    cid: capsule => capsule.capsule_id,
    queueCapsuleAction: async (id, action) => { calls.push({ id, action }); return results[calls.length - 1]; } };
  vm.createContext(state); vm.runInContext(batchSource, state);
  await state.onBatch(op);
  assert.equal(state.batchPending.value, false);
  return { ids: calls.map(row => row.id), note: state.batchNote.value };
}

test('Record all pauses on local blockers and authority failures before the next action', async () => {
  for (const failure of [{ status: 'blocked' }, { status: 'failed' }, { status: 'unconfirmed' },
    { status: 'cancelled' }, { state: 'rejected' }, { state: 'blocked' }, { state: 'interrupted' }, { status: 'queued' }]) {
    const result = await batch('start', [failure, { state: 'committed' }]);
    assert.deepEqual(result.ids, ['cap-a']);
    assert.match(result.note, /Batch paused/);
  }
});

test('Stop all attempts remaining capsules after failure and preserves an honest failure note', async () => {
  for (const failure of [{ status: 'blocked' }, { status: 'failed' }, { status: 'unconfirmed' },
    { state: 'rejected' }, { state: 'interrupted' }]) {
    const result = await batch('stop', [failure, { state: 'committed' }]);
    assert.deepEqual(result.ids, ['cap-a', 'cap-b']);
    assert.match(result.note, /failed or are unconfirmed/);
    assert.doesNotMatch(result.note, /all stopped|success/i);
  }
});

test('Both successful batches continue and clear prior batch failure messaging', async () => {
  for (const op of ['start', 'stop']) {
    const result = await batch(op, [{ state: 'committed' }, { state: 'observed' }]);
    assert.deepEqual(result, { ids: ['cap-a', 'cap-b'], note: '' });
  }
});
