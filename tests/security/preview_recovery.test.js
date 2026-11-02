'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createPreviewSources } = require('../../src/main/capture_sources');
const security = require('../../src/main/shellSecurity');
const load = name => import('data:text/javascript;base64,' + fs.readFileSync(path.join(__dirname, '../../', name)).toString('base64'));
const preview = load('ui/src/capsule/capture.js');
const renderer = load('src/renderer/control.js');
const flush = () => new Promise(resolve => setImmediate(resolve));
function clock() {
  let now = 0, id = 0; const tasks = new Map();
  return {
    schedule: (callback, delay) => { const key = ++id; tasks.set(key, { callback, at: now + delay }); return key; },
    cancel: key => tasks.delete(key),
    pending: () => tasks.size,
    async advance(delay) {
      const until = now + delay; let steps = 0;
      while (true) {
        const next = [...tasks].filter(([, task]) => task.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        assert.ok(++steps < 1000, 'scheduled work must stay bounded');
        now = next[1].at; tasks.delete(next[0]); next[1].callback(); await flush();
      }
      now = until;
    },
  };
}
function stream() {
  const listeners = new Map(); let settings = { width: 320, height: 180, frameRate: 15 };
  const track = { readyState: 'live', stops: 0,
    stop() { this.stops++; this.readyState = 'ended'; },
    async applyConstraints(value) { settings = { width: value.width.max, height: value.height.max, frameRate: value.frameRate.max }; },
    getSettings: () => settings,
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); },
  };
  return { track, getTracks: () => [track], getVideoTracks: () => [track], getAudioTracks: () => [] };
}
function videoElement() {
  let next = 0; const pending = new Map(); const cancelled = [];
  return { srcObject: null, cancelled,
    requestVideoFrameCallback(fn) { const id = ++next; pending.set(id, fn); return id; },
    cancelVideoFrameCallback(id) { cancelled.push(id); pending.delete(id); },
    callbacks: () => [...pending.values()],
    present(count) { const callbacks = [...pending.values()]; pending.clear(); for (const fn of callbacks) fn(0, { presentedFrames: count }); },
  };
}
async function fixture(options = {}) {
  const api = await preview, time = clock(), video = videoElement();
  const states = [], requests = [], media = [];
  let binding = { capsule_id: 'cap-1', feed_id: 'feed-1', generation: '1', preview_available: true, preview_revision: '1' };
  const session = api.createPreviewSession({
    bridge: { previewSource: async pair => { requests.push(pair); return options.grant ? options.grant(pair) : { ok: true, ...pair }; } },
    getDisplayMedia: async () => { const value = options.capture ? await options.capture() : stream(); media.push(value); return value; },
    onStream: value => { video.srcObject = value; },
    observeFrames: (value, onFrame) => api.observePreviewFrames(video, value, onFrame),
    onState: (state, reason) => states.push({ state, reason }), size: () => ({ width: 320, height: 180 }),
    schedule: time.schedule, cancel: time.cancel, frameSchedule: time.schedule, frameCancel: time.cancel,
  });
  return { session, time, video, states, requests, media,
    async start() { session.setBinding(binding); session.setVisibility(true, true); await flush(); },
    update(value) { binding = { ...binding, ...value }; session.setBinding(binding); },
  };
}

test('live requires advancing native frame events; stalled frames recover through a fresh grant', async () => {
  const f = await fixture();
  try {
    await f.start(); assert.equal(f.states.at(-1).state, 'connecting');
    f.video.present(0); assert.equal(f.states.at(-1).state, 'connecting');
    f.video.present(1); assert.equal(f.states.at(-1).state, 'live');
    const stale = f.video.callbacks()[0];
    await f.time.advance(2999); f.video.present(1);
    await f.time.advance(1);
    assert.deepEqual(f.states.at(-1), { state: 'no_preview', reason: 'preview_no_frames' });
    assert.equal(f.media[0].track.stops, 1);
    stale(0, { presentedFrames: 2 }); assert.equal(f.states.at(-1).state, 'no_preview');
    await f.time.advance(500); assert.equal(f.requests.length, 2); assert.equal(f.states.at(-1).state, 'connecting');
    f.video.present(2); assert.equal(f.states.at(-1).state, 'live');
    f.session.setVisibility(false, true); assert.ok(f.video.cancelled.length > 0);
    assert.equal(f.media[1].track.stops, 1);
  } finally { f.session.dispose(); }
});

test('a source which never delivers a frame exhausts a finite retry budget', async () => {
  const f = await fixture();
  try {
    await f.start(); await f.time.advance(30000);
    assert.equal(f.requests.length, 4); assert.equal(f.media.length, 4);
    assert.ok(f.media.every(value => value.track.stops === 1));
    assert.equal(f.time.pending(), 0);
    assert.deepEqual(f.states.at(-1), { state: 'no_preview', reason: 'preview_no_frames' });
    for (let i = 0; i < 100; i++) f.update({ preview_available: true, preview_revision: '1' });
    await f.time.advance(30000); assert.equal(f.requests.length, 4);
  } finally { f.session.dispose(); }
});

test('denied and missing previews wake only on a changed current projection, without snapshot retry floods', async () => {
  for (const error of ['preview_denied', 'no_preview']) {
    let denied = true;
    const f = await fixture({ grant: pair => denied ? { ok: false, error } : { ok: true, ...pair } });
    try {
      await f.start(); assert.equal(f.requests.length, 1); assert.equal(f.media.length, 0);
      for (let i = 0; i < 100; i++) f.update({ preview_available: true, preview_revision: '1' });
      await f.time.advance(30000); assert.equal(f.requests.length, 1); assert.equal(f.time.pending(), 0);
      f.update({ preview_available: false, preview_revision: null });
      for (let i = 0; i < 100; i++) f.update({ preview_available: false, preview_revision: null });
      await f.time.advance(30000); assert.equal(f.requests.length, 1);
      denied = false; f.update({ preview_available: true, preview_revision: '1' });
      await f.time.advance(499); assert.equal(f.requests.length, 1);
      await f.time.advance(1); assert.equal(f.requests.length, 2); assert.equal(f.media.length, 1);
      assert.deepEqual(f.requests.at(-1), { capsule_id: 'cap-1', feed_id: 'feed-1' });
      f.video.present(1); assert.equal(f.states.at(-1).state, 'live');
    } finally { f.session.dispose(); }
  }
});

test('permission denial does not retry repeatedly on an unchanged eligible projection', async () => {
  let captures = 0;
  const f = await fixture({ capture: () => { captures++; const error = new Error('PRIVATE_PROVIDER_DETAIL'); error.name = 'NotAllowedError'; throw error; } });
  try {
    await f.start(); await f.time.advance(30000);
    assert.equal(captures, 1); assert.equal(f.requests.length, 1); assert.equal(f.time.pending(), 0);
    assert.equal(JSON.stringify(f.states).includes('PRIVATE_PROVIDER_DETAIL'), false);
    for (let i = 0; i < 100; i++) f.update({ preview_available: true, preview_revision: '1' });
    await f.time.advance(30000); assert.equal(captures, 1);
  } finally { f.session.dispose(); }
});

test('layout revisions reacquire with unchanged feed generation and cancel stale presented-frame callbacks', async () => {
  const f = await fixture();
  try {
    await f.start(); f.video.present(1); const stale = f.video.callbacks()[0];
    f.update({ preview_revision: '2' }); assert.equal(f.media[0].track.stops, 1);
    stale(0, { presentedFrames: 999 }); assert.equal(f.states.at(-1).state, 'no_preview');
    await f.time.advance(250); f.update({ preview_revision: '3' });
    await f.time.advance(499); assert.equal(f.requests.length, 1);
    await f.time.advance(1); assert.equal(f.requests.length, 2);
    f.video.present(1); assert.equal(f.states.at(-1).state, 'live');
    assert.deepEqual(f.requests, [{ capsule_id: 'cap-1', feed_id: 'feed-1' }, { capsule_id: 'cap-1', feed_id: 'feed-1' }]);
    f.session.dispose(); const last = f.states.length; stale(0, { presentedFrames: 1000 }); assert.equal(f.states.length, last);
  } finally { f.session.dispose(); }
});

test('decoded-frame fallback requires an actual advancing counter and cancels on a stream switch', async () => {
  const { observePreviewFrames } = await preview, time = clock(), value = stream(), seen = [];
  let frames = 17; const video = { srcObject: value, getVideoPlaybackQuality: () => ({ totalVideoFrames: frames }) };
  const cancel = observePreviewFrames(video, value, count => seen.push(count), time);
  await time.advance(500); assert.deepEqual(seen, []);
  frames = 18; await time.advance(250); assert.deepEqual(seen, [18]);
  video.srcObject = stream(); frames = 19; await time.advance(250); assert.deepEqual(seen, [18]);
  cancel(); assert.equal(time.pending(), 0);
});

function authority() {
  return { availability: 'available', pid: null, reason: 'authority_available', authority_epoch: '77', revision: '3', stderrBytes: 0,
    state: { runtime: 'authority_ready', platform: 'windows', capsules: { 'cap-1': { capsule_id: 'cap-1', person_label: 'Guest', feeds: { game: { feed_id: 'feed-1', role: 'game', generation: '1' } } } }, windows: [], window_page: { offset: 0, next_offset: null, total: 0 }, recordings: [],
      preview_sources: [{ capsule_id: 'cap-1', feed_id: 'feed-1', source_id: 'window:101:0' }],
      preview_eligibility: [{ capsule_id: 'cap-1', feed_id: 'feed-1', layout_revision: '1' }],
    } };
}
test('only closed safe eligibility signals reach the renderer, and stale snapshots cannot rewind them', async () => {
  const value = authority(), projected = security.publicSnapshot(value);
  assert.equal(JSON.stringify(projected).includes('window:101:0'), false);
  assert.deepEqual(projected.state.preview_eligibility, value.state.preview_eligibility);
  let publish; const controls = (await renderer).createControls({ getSnapshot: async () => projected,
    onSnapshot: fn => { publish = fn; return () => {}; }, submitIntent: async () => { throw new Error('preview metadata cannot send an intent'); },
  });
  try {
    await controls.connect(); assert.equal(controls.getState().capsules[0].feeds[0].preview_revision, '1');
    const newer = authority(); newer.revision = '4'; newer.state.preview_eligibility[0].layout_revision = '2'; publish(security.publicSnapshot(newer));
    publish(projected); assert.equal(controls.getState().capsules[0].feeds[0].preview_revision, '2');
    const missing = authority(); missing.revision = '5'; missing.state.preview_sources = []; missing.state.preview_eligibility = []; publish(security.publicSnapshot(missing));
    assert.equal(controls.getState().capsules[0].feeds[0].preview_available, false);
    for (const mutation of [row => { row.source_id = 'window:101:0'; }, row => { row.layout_revision = '0'; }, row => { row.layout_revision = null; }]) {
      const malformed = authority(); malformed.revision = '6'; mutation(malformed.state.preview_eligibility[0]);
      assert.throws(() => security.publicSnapshot(malformed), /SNAPSHOT_REJECTED/);
      publish(malformed); assert.equal(controls.getState().availability, 'unavailable');
    }
  } finally { controls.dispose(); }
});

test('recovery signals do not mint grants, override consent, or revive stale single-use selections', async () => {
  const frame = { url: security.BOOT_URL, parent: null, detached: false };
  const contents = { mainFrame: frame, isDestroyed: () => false }; let status = authority(), now = 1000;
  const broker = createPreviewSources({ getContents: () => contents, getStatus: () => status, now: () => now, getSources: async () => [{ id: 'window:101:0' }] });
  broker.install({ setDisplayMediaRequestHandler() {} });
  const event = { sender: contents, senderFrame: frame }, pair = { capsule_id: 'cap-1', feed_id: 'feed-1' };
  const details = { isMainFrame: true, requestingUrl: security.BOOT_URL, securityOrigin: 'cafe://app', mediaTypes: [] };
  const display = () => new Promise(resolve => { void broker.displayRequest({ frame, securityOrigin: 'cafe://app', videoRequested: true, audioRequested: false }, resolve); });
  try {
    assert.equal(broker.permissionRequest(contents, 'media', details), false); assert.equal(await display(), null);
    assert.equal((await broker.request(event, pair)).ok, true);
    status.state.capsules['cap-1'].feeds.game.generation = '2'; assert.equal(await display(), null);
    assert.equal((await broker.request(event, pair)).ok, true); now += 5000; assert.equal(await display(), null);
    assert.equal((await broker.request(event, pair)).ok, true); status.state.preview_sources = []; assert.equal(await display(), null);
    status = authority(); status.revision = '4'; assert.equal((await broker.request(event, pair)).ok, true);
    assert.ok(await display()); assert.equal(await display(), null);
    assert.equal(broker.permissionRequest(contents, 'media', details), false);
    assert.equal((await broker.request(event, { ...pair, source_id: 'window:101:0' })).ok, false);
  } finally { broker.dispose(); }
});

test('an unresolved timed-out acquisition keeps the global grant queue quarantined', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let resolve;
  const first = await fixture({ capture: () => new Promise(done => { resolve = done; }) }), second = await fixture();
  try {
    await first.start(); await second.start(); assert.equal(second.requests.length, 0);
    t.mock.timers.tick(8001); await flush(); assert.equal(first.states.at(-1).state, 'no_preview'); assert.equal(second.requests.length, 0);
    const late = stream(); resolve(late); await flush(); assert.equal(late.track.stops, 1); assert.equal(first.video.srcObject, null);
    assert.equal(second.requests.length, 1); assert.equal(second.states.at(-1).state, 'connecting');
    second.video.present(1); assert.equal(second.states.at(-1).state, 'live');
  } finally { if (resolve) resolve(stream()); await flush(); first.session.dispose(); second.session.dispose(); t.mock.timers.reset(); }
});

test('many projection changes coalesce behind one pending acquisition without growing queued session work', async () => {
  let resolve, captures = 0;
  const first = await fixture({ capture: () => ++captures === 1 ? new Promise(done => { resolve = done; }) : stream() });
  const second = await fixture();
  try {
    await first.start();
    for (let revision = 2; revision <= 21; revision++) {
      first.update({ preview_revision: String(revision) }); await first.time.advance(500);
    }
    assert.equal(first.requests.length, 1);
    assert.equal(first.states.filter(row => row.state === 'connecting').length, 1, 'stale projection wakeups must not enqueue connecting work');
    await second.start(); assert.equal(second.requests.length, 0, 'other sessions still respect the native queue owner');
    const late = stream(); resolve(late); await flush();
    assert.equal(late.track.stops, 1); assert.equal(first.requests.length, 1);
    assert.equal(second.requests.length, 1);
    await first.time.advance(499); assert.equal(first.requests.length, 1);
    await first.time.advance(1); assert.equal(first.requests.length, 2);
    first.video.present(1); assert.equal(first.states.at(-1).state, 'live');
  } finally { if (resolve) resolve(stream()); await flush(); first.session.dispose(); second.session.dispose(); }
});

test('settling stale acquisitions cannot rearm disposed, hidden, disabled or unavailable sessions', async () => {
  for (const mode of ['disposed', 'hidden', 'disabled', 'unavailable']) {
    let resolve;
    const f = await fixture({ capture: () => new Promise(done => { resolve = done; }) });
    try {
      await f.start(); f.update({ preview_revision: '2' });
      if (mode === 'disposed') f.session.dispose();
      if (mode === 'hidden') f.session.setVisibility(false, true);
      if (mode === 'disabled') f.session.setVisibility(true, false);
      if (mode === 'unavailable') f.update({ preview_available: false, preview_revision: null });
      const late = stream(); resolve(late); await flush(); await f.time.advance(30000);
      assert.equal(late.track.stops, 1, mode); assert.equal(f.requests.length, 1, mode); assert.equal(f.time.pending(), 0, mode);
      assert.equal(f.video.srcObject, null, mode);
    } finally { if (resolve) resolve(stream()); await flush(); f.session.dispose(); }
  }
});

test('operator preview reasons are fixed actionable text without provider payload', async () => {
  const { previewReasonText } = await preview;
  assert.match(previewReasonText('preview_no_frames', 'no_preview'), /visible and uncovered/);
  assert.match(previewReasonText('preview_denied', 'no_preview'), /window is still open/);
  assert.equal(previewReasonText('<b>PRIVATE_PATH</b>', 'no_preview').includes('PRIVATE_PATH'), false);
});
