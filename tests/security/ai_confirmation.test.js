'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/renderer/control.js'), 'utf8');
const load = import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
function snapshot() {
  return { availability: 'available', authority_epoch: '77', revision: '0', state: {
    runtime: 'authority_ready', platform: 'windows', capsules: {}, windows: [], recordings: [],
    window_page: { offset: 0, next_offset: null, total: 0 },
    ask_before_ai_actions: false,
  } };
}
async function fixture(value) {
  const sent = []; let publish; let sequence = 0;
  const controls = (await load).createControls({ getSnapshot: async () => value,
    onSnapshot: fn => { publish = fn; return () => {}; },
    submitIntent: async request => { sent.push(request); return { queued: true }; },
  }, () => `local-${++sequence}`);
  await controls.connect();
  return { controls, sent, publish: next => publish(next) };
}
test('local confirmation preference requires an authoritative boolean and never changes optimistically', async () => {
  const value = snapshot(); const f = await fixture(value);
  try {
    assert.equal(f.controls.getState().askBeforeAiActions, false);
    assert.equal(f.sent.length, 0);
    await f.controls.setAskBeforeAiActions(true);
    assert.deepEqual(f.sent[0], { schema: 'cafe.operator.v1', kind: 'operator_intent', request_id: 'local-1', authority_epoch: '77', expected_revision: '0', body: { op: 'set_ask_before_ai_actions', enabled: true } });
    assert.equal(f.controls.getState().askBeforeAiActions, false);
    value.state.ask_before_ai_actions = true; value.revision = '1'; f.publish(value);
    assert.equal(f.controls.getState().askBeforeAiActions, true);
    for (const invalid of ['false', 0, null, undefined]) await assert.rejects(f.controls.setAskBeforeAiActions(invalid));
    assert.equal(f.sent.length, 1);
    delete value.state.ask_before_ai_actions; f.publish(value);
    await assert.rejects(f.controls.setAskBeforeAiActions(false));
    assert.equal(f.sent.length, 1);
  } finally { f.controls.dispose(); }
});
test('manual stop/OBS plans retain Confirm with an empty folder and do not execute while observed', async () => {
  const value = snapshot(); value.state.ask_before_ai_actions = true;
  value.state.organizer_plan = { plan_id: 'plan-1', folder_name: '', windows: [{ title: '<b>untrusted</b>' }], expires_unix_ms: Date.now() + 60000 };
  const f = await fixture(value);
  try {
    assert.equal(f.sent.length, 0);
    assert.equal(f.controls.getState().organizerPlan.folder_name, '');
    await f.controls.confirmOrganizerPlan('plan-1');
    assert.deepEqual(f.sent[0].body, { op: 'organizer_plan_confirm', plan_id: 'plan-1' });
    assert.equal(f.controls.getState().organizerPlan.plan_id, 'plan-1');
  } finally { f.controls.dispose(); }
});
test('wall activity admits only closed codes and bounded counts, discarding titles', async () => {
  const value = snapshot(); const f = await fixture(value);
  try {
    for (const code of ['ai_recording_submitted', 'ai_stop_submitted', 'ai_obs_submitted', 'ai_scene_submitted']) {
      value.state.ai_activity = { code, count: 2, title: '<b>untrusted</b>' }; f.publish(value);
      assert.deepEqual(f.controls.getState().aiActivity, { code, count: 2 });
    }
    for (const activity of [{ code: '<b>untrusted</b>', count: 2 }, { code: 'ai_recording_submitted', count: -1 }, { code: 'ai_recording_submitted', count: 13 }, { code: 'ai_recording_submitted', count: '2' }, { code: 'ai_recording_submitted', count: null }]) {
      value.state.ai_activity = activity; f.publish(value);
      assert.equal(f.controls.getState().aiActivity, null);
    }
  } finally { f.controls.dispose(); }
});
