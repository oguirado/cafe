// @ts-check
'use strict';

// Contract tests use synthetic authority state and browser objects. Native
// Windows capture, Discord, and concurrent recordings remain PENDING-LIVE.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { LIVE_PREVIEW_ENABLED, createPreviewSources } = require('../../src/main/capture_sources');
const security = require('../../src/main/shellSecurity');

const copy = value => JSON.parse(JSON.stringify(value));
const capsule = number => ({
  capsule_id: `cap-${number}`, person_label: `Guest ${number}`,
  feeds: { game: { feed_id: `feed-${number}`, role: 'game', generation: '1' } },
});
function authority(count = 1) {
  return {
    availability: 'available', pid: null, reason: 'authority_available',
    authority_epoch: '9007199254740993', revision: '3', stderrBytes: 0,
    state: {
      runtime: 'authority_ready', platform: 'windows',
      folder_configured: true, recording_configured: true,
      obs_available: false, obs_configurable: false,
      chat_available: false, chat_configurable: false,
      capsules: Object.fromEntries(Array.from({ length: count }, (_, n) => {
        const row = capsule(n + 1); return [row.capsule_id, row];
      })),
      windows: [], window_page: { offset: 0, next_offset: null, total: 0 },
      recordings: [], last_result: { state: 'observed', code: 'snapshot' },
      preview_sources: [{ capsule_id: 'cap-1', feed_id: 'feed-1', source_id: 'window:101:0' }],
    },
  };
}
function previewFixture(enabled = true) {
  const frame = { url: security.BOOT_URL, parent: null, detached: false };
  const contents = { mainFrame: frame, isDestroyed: () => false, getURL: () => security.BOOT_URL };
  const event = { sender: contents, senderFrame: frame };
  let status = authority();
  let sources = [{ id: 'window:101:0', name: 'Synthetic window' }];
  let time = 1000;
  let enumerations = 0;
  let getSources = async options => {
    enumerations++;
    assert.deepEqual(options.types, ['window']);
    return sources;
  };
  let displayHandler = null;
  const session = { setDisplayMediaRequestHandler: fn => { displayHandler = fn; } };
  const broker = createPreviewSources({
    getContents: () => contents, getStatus: () => status,
    getSources: options => getSources(options), now: () => time,
  }, enabled);
  broker.install(session);
  const details = { isMainFrame: true, requestingUrl: security.BOOT_URL, securityOrigin: 'cafe://app', mediaTypes: ['video'] };
  const checkDetails = { isMainFrame: true, requestingUrl: security.BOOT_URL, securityOrigin: 'cafe://app', mediaType: 'video' };
  const request = { frame, securityOrigin: 'cafe://app', videoRequested: true, audioRequested: false, userGesture: true };
  return {
    broker, frame, contents, event, details, checkDetails, request,
    setStatus: value => { status = value; }, status: () => status,
    setSources: value => { sources = value; },
    setGetSources: value => { getSources = value; },
    advance: value => { time += value; },
    enumerationCount: () => enumerations, handler: () => displayHandler,
    grant: () => broker.request(event, { capsule_id: 'cap-1', feed_id: 'feed-1' }),
    display: (value = request) => new Promise(resolve => broker.displayRequest(value, resolve)),
  };
}

test('preview flag defaults off, disabled requests enumerate nothing and open no permission', async () => {
  assert.equal(LIVE_PREVIEW_ENABLED, true);
  const f = previewFixture(false);
  assert.deepEqual(await f.grant(), { ok: false, error: 'preview_disabled' });
  assert.equal(f.enumerationCount(), 0);
  for (const permission of ['media', 'display-capture', 'geolocation', 'notifications', 'clipboard-read', 'fullscreen']) {
    assert.equal(f.broker.permissionRequest(f.contents, permission, f.details), false);
    assert.equal(f.broker.permissionCheck(f.contents, permission, 'cafe://app', f.checkDetails), false);
  }
  assert.equal(await f.display(), null);
  f.broker.dispose();
});

test('enabled grant identifies a current capsule/feed without exposing native source ids or window names', async () => {
  const f = previewFixture();
  const grant = await f.grant();
  assert.deepEqual(grant, { ok: true, capsule_id: 'cap-1', feed_id: 'feed-1' });
  assert.equal(JSON.stringify(grant).includes('window:'), false);
  assert.equal(JSON.stringify(grant).includes('Synthetic window'), false);
  assert.equal(f.enumerationCount(), 1);
  f.broker.dispose();
});

test('closed preview request cannot carry a renderer-selected source id or unknown field', async () => {
  for (const request of [
    { capsule_id: 'cap-1', feed_id: 'feed-1', source_id: 'window:999:0' },
    { capsule_id: 'cap-1', feed_id: 'feed-1', audio: true },
    { capsule_id: 'cap-1' },
    { capsule_id: 'cap-1', feed_id: 1 },
    { capsule_id: 'cap-1', feed_id: 'feed-2' },
  ]) {
    const f = previewFixture();
    const reply = await f.broker.request(f.event, request);
    assert.equal(reply.ok, false);
    assert.equal(f.enumerationCount(), 0);
    f.broker.dispose();
  }
});

test('missing, malformed, duplicate or unbound source rows cannot authorize capture', async () => {
  const valid = { capsule_id: 'cap-1', feed_id: 'feed-1', source_id: 'window:101:0' };
  for (const rows of [
    undefined, null, {}, [],
    [{ ...valid, source_id: 'screen:101:0' }],
    [{ ...valid, source_id: 'window:-1:0' }],
    [{ ...valid, source_id: 'window:101:1' }],
    [{ ...valid, source_id: 'window:101:0\n' }],
    [{ ...valid, source_id: 'window:1e2:0' }],
    [{ ...valid, source_id: 101 }],
    [{ ...valid, extra: true }],
    [valid, { ...valid }],
    [{ ...valid, capsule_id: 'cap-missing' }],
    [{ ...valid, feed_id: 'feed-missing' }],
  ]) {
    const f = previewFixture();
    const state = authority();
    if (rows === undefined) delete state.state.preview_sources;
    else state.state.preview_sources = rows;
    f.setStatus(state);
    assert.equal((await f.grant()).ok, false);
    assert.equal(await f.display(), null);
    f.broker.dispose();
  }
});

test('core mapping also needs a current desktopCapturer window source', async () => {
  const f = previewFixture();
  f.setSources([{ id: 'window:999:0', name: 'Other synthetic window' }]);
  assert.equal((await f.grant()).ok, false);
  assert.equal(await f.display(), null);
  f.broker.dispose();
});

test('preview requires an available Windows authority and never falls back to another platform', async () => {
  for (const stateChange of [
    { platform: 'unsupported' }, { platform: undefined },
    { runtime: 'starting' }, { runtime: undefined },
  ]) {
    const f = previewFixture();
    const value = authority(); Object.assign(value.state, stateChange);
    f.setStatus(value);
    assert.equal((await f.grant()).ok, false);
    assert.equal(f.enumerationCount(), 0);
    f.broker.dispose();
  }
});

test('foreign sender, subframe, detached frame and wrong app origin never enumerate windows', async () => {
  for (const mutate of [
    f => ({ ...f.event, sender: {} }),
    f => ({ ...f.event, senderFrame: { ...f.frame } }),
    f => { f.frame.parent = {}; return f.event; },
    f => { f.frame.detached = true; return f.event; },
    f => { f.frame.url = 'https://example.invalid/'; return f.event; },
    f => { f.frame.url = 'cafe://other/bootstrap'; return f.event; },
    f => { f.frame.url = 'cafe://app/bootstrap?other=1'; return f.event; },
  ]) {
    const f = previewFixture();
    const reply = await f.broker.request(mutate(f), { capsule_id: 'cap-1', feed_id: 'feed-1' });
    assert.equal(reply.ok, false);
    assert.equal(f.enumerationCount(), 0);
    f.broker.dispose();
  }
});

test('a grant is revalidated when asynchronous source enumeration completes', async () => {
  const f = previewFixture();
  let resolveSources;
  f.setGetSources(() => new Promise(resolve => { resolveSources = resolve; }));
  const pending = f.grant();
  const next = authority();
  next.revision = '4'; next.state.preview_sources = [];
  f.setStatus(next);
  resolveSources([{ id: 'window:101:0', name: 'Synthetic window' }]);
  assert.equal((await pending).ok, false);
  assert.equal(await f.display(), null);
  f.broker.dispose();
});

test('display handler only consumes a granted current window once and never grants audio', async () => {
  const f = previewFixture();
  assert.equal(await f.display(), null);
  await f.grant();
  const capture = await f.display();
  assert.equal(capture.video.id, 'window:101:0');
  assert.equal(Object.hasOwn(capture, 'audio'), false);
  assert.equal(await f.display(), null);
  f.broker.dispose();
});

test('display requests reject audio, unknown source grants, changed frame, and non-app origins', async () => {
  for (const change of [
    { audioRequested: true }, { videoRequested: false },
    { securityOrigin: 'https://example.invalid' },
    { securityOrigin: 'cafe://other' },
    { frame: null }, { frame: {} },
  ]) {
    const f = previewFixture();
    await f.grant();
    assert.equal(await f.display({ ...f.request, ...change }), null);
    f.broker.dispose();
  }
  const f = previewFixture();
  await f.grant();
  f.setSources([]);
  assert.equal(await f.display(), null);
  f.broker.dispose();
});

test('expired grant and authority changes between grant and display fail closed', async () => {
  for (const mutate of [
    f => f.advance(6000),
    f => { const next = authority(); next.state.preview_sources[0].source_id = 'window:999:0'; f.setStatus(next); },
    f => { const next = authority(); next.state.capsules = {}; f.setStatus(next); },
    f => { const next = authority(); next.availability = 'unavailable'; f.setStatus(next); },
    f => { f.contents.mainFrame = { ...f.frame }; },
  ]) {
    const f = previewFixture();
    await f.grant(); mutate(f);
    assert.equal(await f.display(), null);
    f.broker.dispose();
  }
});

test('permission callbacks refuse other origins, frames, audio and ambiguous Electron 44 desktop requests', async () => {
  const f = previewFixture();
  await f.grant();
  // Electron 44's media/video permission is a camera request; typed media stays denied.
  assert.equal(f.broker.permissionRequest(f.contents, 'media', f.details), false);
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', f.checkDetails), false);
  for (const details of [
    { ...f.details, mediaTypes: ['audio'] },
    { ...f.details, mediaTypes: ['video', 'audio'] },
    { ...f.details, mediaTypes: ['unknown'] },
    { ...f.details, mediaTypes: undefined },
    { ...f.details, mediaTypes: [], isMainFrame: false },
    { ...f.details, mediaTypes: [], requestingUrl: 'https://example.invalid/' },
    { ...f.details, mediaTypes: [], securityOrigin: 'cafe://other' },
  ]) {
    for (const permission of ['media', 'display-capture']) {
      assert.equal(f.broker.permissionRequest(f.contents, permission, details), false);
    }
  }
  assert.equal(f.broker.permissionRequest(f.contents, 'display-capture', { ...f.details, mediaTypes: [] }), false);
  for (const details of [
    { ...f.checkDetails, mediaType: 'audio' },
    { ...f.checkDetails, mediaType: 'video' },
    { ...f.checkDetails, mediaType: 'unknown' },
    { ...f.checkDetails, mediaType: undefined, isMainFrame: false },
    { ...f.checkDetails, mediaType: undefined, requestingUrl: 'https://example.invalid/' },
  ]) {
    assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', details), false);
  }
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'https://example.invalid', { ...f.checkDetails, mediaType: undefined }), false);
  for (const permission of ['notifications', 'clipboard-read', 'camera', 'microphone', 'geolocation', 'fullscreen']) {
    assert.equal(f.broker.permissionRequest(f.contents, permission, { ...f.details, mediaTypes: [] }), false);
    assert.equal(f.broker.permissionCheck(f.contents, permission, 'cafe://app', { ...f.checkDetails, mediaType: undefined }), false);
  }
  f.broker.dispose();
});

test('renderer snapshots strip native preview mappings without changing authority data', () => {
  const original = authority();
  const saved = copy(original);
  const projected = security.publicSnapshot(original);
  assert.equal(Object.hasOwn(projected.state, 'preview_sources'), false);
  assert.equal(JSON.stringify(projected).includes('window:101:0'), false);
  assert.deepEqual(original, saved);
  assert.equal(projected.authority_epoch, original.authority_epoch);
});

async function controlsFixture(value = authority()) {
  const module = await import(pathToFileURL(path.resolve(__dirname, '../../src/renderer/control.js')).href);
  let listener = () => {};
  let sequence = 0;
  const sent = [];
  const bridge = {
    getSnapshot: async () => value,
    onSnapshot: fn => { listener = fn; return () => {}; },
    submitIntent: async request => { sent.push(request); return { queued: true }; },
  };
  const controls = module.createControls(bridge, () => `request-${++sequence}`);
  await controls.connect();
  return { controls, bridge, sent, publish: value => listener(value), diagnostics: module.buildDiagnosticReport };
}

test('controller admits twelve authoritative capsules and rejects thirteen without altering window page size', async () => {
  for (const count of [1, 3, 12, 13]) {
    const f = await controlsFixture(authority(count));
    assert.equal(f.controls.getState().availability, count <= 12 ? 'available' : 'unavailable');
    assert.equal(f.diagnostics(authority(count)).capsule_count, count <= 12 ? count : null);
    f.controls.dispose();
  }
  const value = authority(12);
  value.state.windows = Array.from({ length: 4 }, (_, n) => ({
    handle: `synthetic-${n}`, generation: '1', layout_revision: '1', rect: { width: 100, height: 80 },
  }));
  const f = await controlsFixture(value);
  assert.equal(f.controls.getState().availability, 'unavailable');
  f.controls.dispose();
});

test('third capsule reaches the authority and its refusal is not represented as a created capsule', async () => {
  const f = await controlsFixture(authority(2));
  await f.controls.createCapsule('Guest 3');
  assert.equal(f.sent[0].body.op, 'create_capsule');
  assert.equal(f.controls.getState().capsules.length, 2);
  const refused = authority(2);
  refused.revision = '4';
  refused.state.last_result = { state: 'rejected', code: 'request_or_capacity_conflict', request_id: f.sent[0].request_id };
  f.publish(refused);
  assert.equal(f.controls.getState().capsules.length, 2);
  assert.equal(f.controls.getState().lastResult.state, 'rejected');
  assert.equal(f.controls.getState().lastResult.code, 'request_or_capacity_conflict');
  f.controls.dispose();
  const full = await controlsFixture(authority(12));
  await assert.rejects(full.controls.createCapsule('Guest 13'));
  assert.equal(full.sent.length, 0);
  full.controls.dispose();
});

test('per-capsule recording commands preserve another capsule and await authoritative state', async () => {
  const value = authority(2);
  value.state.recordings = [{ capsule_id: 'cap-2', state: 'Running', elapsed_ms: '5000', feeds: [] }];
  const f = await controlsFixture(value);
  await f.controls.startRecording('cap-1');
  assert.deepEqual(f.sent[0].body, { op: 'start_recording', capsule_id: 'cap-1' });
  assert.deepEqual(f.controls.getState().recordings.map(row => [row.capsule_id, row.state]), [['cap-2', 'Running']]);
  await assert.rejects(f.controls.stopRecording('cap-1'));
  await f.controls.stopRecording('cap-2');
  assert.deepEqual(f.sent[1].body, { op: 'stop_recording', capsule_id: 'cap-2' });
  assert.equal(f.controls.getState().recordings[0].state, 'Running');
  f.controls.dispose();
});

test('organizer and auto-anchor stay absent until authority publishes their frozen contracts', async () => {
  const f = await controlsFixture();
  assert.equal(f.controls.getState().organizerPlan, null);
  assert.equal(f.controls.getState().autoAnchor, null);
  await assert.rejects(f.controls.confirmOrganizerPlan('plan-1'));
  await assert.rejects(f.controls.rejectOrganizerPlan('plan-1'));
  await assert.rejects(f.controls.setAutoAnchor(true));
  assert.equal(f.sent.length, 0);
  f.controls.dispose();
});

test('explicit organizer confirmation/rejection and auto-anchor use only exact named intents', async () => {
  const value = authority();
  value.state.organizer_plan = {
    plan_id: 'plan-1', windows: [{ title: '<strong>Synthetic call</strong>' }],
    folder_name: 'Synthetic session', expires_unix_ms: String(Date.now() + 60000),
  };
  value.state.auto_anchor = false;
  const f = await controlsFixture(value);
  assert.equal(f.sent.length, 0);
  assert.equal(f.controls.getState().organizerPlan.windows[0].title, '<strong>Synthetic call</strong>');
  assert.equal(f.controls.getState().autoAnchor, false);
  await assert.rejects(f.controls.confirmOrganizerPlan('plan-other'));
  await f.controls.confirmOrganizerPlan('plan-1');
  await f.controls.rejectOrganizerPlan('plan-1');
  await f.controls.setAutoAnchor(true);
  assert.deepEqual(f.sent.map(row => row.body), [
    { op: 'organizer_plan_confirm', plan_id: 'plan-1' },
    { op: 'organizer_plan_reject', plan_id: 'plan-1' },
    { op: 'set_auto_anchor', enabled: true },
  ]);
  assert.equal(f.controls.getState().autoAnchor, false);
  assert.equal(f.controls.getState().organizerPlan.plan_id, 'plan-1');
  await assert.rejects(f.controls.setAutoAnchor('true'));
  f.controls.dispose();
});

test('an expired organizer plan can be rejected but cannot be confirmed', async () => {
  const value = authority();
  value.state.organizer_plan = { plan_id: 'plan-1', windows: [{ title: 'Synthetic call' }], folder_name: 'Synthetic session', expires_unix_ms: '1' };
  const f = await controlsFixture(value);
  await assert.rejects(f.controls.confirmOrganizerPlan('plan-1'));
  await f.controls.rejectOrganizerPlan('plan-1');
  assert.deepEqual(f.sent.map(row => row.body), [{ op: 'organizer_plan_reject', plan_id: 'plan-1' }]);
  f.controls.dispose();
});

test('recording reason and elapsed time are authoritative and unknown provider text remains closed', async () => {
  const value = authority();
  value.state.recordings = [
    { capsule_id: 'cap-1', state: 'Failed', reason: 'recording_capture_minimized', elapsed_ms: '9007199254740993', feeds: [] },
  ];
  const f = await controlsFixture(value);
  assert.equal(f.controls.getState().recordings[0].reason, 'recording_capture_minimized');
  assert.equal(f.controls.getState().recordings[0].elapsed_ms, '9007199254740993');
  const named = copy(value); named.revision = '4'; named.state.recordings[0].reason = 'window_unavailable';
  f.publish(named);
  assert.equal(f.controls.getState().recordings[0].reason, 'window_unavailable');
  const next = copy(value); next.revision = '5';
  next.state.recordings[0].reason = 'SYNTHETIC_PROVIDER_PRIVATE_CANARY';
  next.state.recordings[0].elapsed_ms = 9007199254740992;
  f.publish(next);
  assert.equal(f.controls.getState().recordings[0].reason, 'recording_reason_unavailable');
  assert.equal(f.controls.getState().recordings[0].elapsed_ms, null);
  assert.equal(JSON.stringify(f.controls.getState()).includes('SYNTHETIC_PROVIDER_PRIVATE_CANARY'), false);
  f.controls.dispose();
});

test('preload rejects extra source identities, mismatched grants and provider error text', async () => {
  const { createCafeBridge } = require('../../src/preload/cafeBridge');
  const calls = [];
  let response = { ok: true, capsule_id: 'cap-1', feed_id: 'feed-1' };
  const bridge = createCafeBridge({
    invoke: async (channel, request) => { calls.push({ channel, request }); return response; },
    subscribe: () => () => {},
  });
  const pair = { capsule_id: 'cap-1', feed_id: 'feed-1' };
  assert.equal((await bridge.previewSource({ ...pair, source_id: 'window:999:0' })).ok, false);
  assert.equal(calls.length, 0);
  assert.deepEqual(await bridge.previewSource(pair), response);
  assert.deepEqual(calls[0], { channel: 'cafe:previewSource', request: pair });
  for (const value of [
    { ok: true, capsule_id: 'cap-other', feed_id: 'feed-1' },
    { ok: true, ...pair, source_id: 'window:999:0' },
    { ok: false, error: 'SYNTHETIC_PROVIDER_PRIVATE_CANARY' },
  ]) {
    response = value;
    assert.deepEqual(await bridge.previewSource(pair), { ok: false, error: 'preview_unavailable' });
  }
});

test('a mocked distinct display permission is grant-scoped; this is not Electron 44 compatibility evidence', async () => {
  const f = previewFixture();
  const untypedRequest = { ...f.details, mediaTypes: [] };
  const untypedCheck = { ...f.checkDetails, mediaType: undefined };
  assert.equal(f.broker.permissionRequest(f.contents, 'media', untypedRequest), false);
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', untypedCheck), false);
  await f.grant();
  assert.equal(f.broker.permissionRequest(f.contents, 'media', untypedRequest), true);
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', untypedCheck), true);
  assert.equal(f.broker.permissionRequest(f.contents, 'media', f.details), false);
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', f.checkDetails), false);
  assert.equal(f.broker.permissionRequest(f.contents, 'display-capture', untypedRequest), false);
  assert.equal(f.broker.permissionRequest({}, 'media', untypedRequest), false);
  await f.display();
  assert.equal(f.broker.permissionRequest(f.contents, 'media', untypedRequest), false);
  assert.equal(f.broker.permissionCheck(f.contents, 'media', 'cafe://app', untypedCheck), false);
  f.broker.dispose();
});

test('supervisor retains native preview mappings for main while stripping handle and subscription results', () => {
  const { EventEmitter } = require('node:events');
  const { createBackendSupervisor } = require('../../src/main/backendSupervisor');
  const frame = {};
  const child = new EventEmitter();
  Object.assign(child, {
    pid: 4242, stdout: new EventEmitter(), stderr: new EventEmitter(),
    stdin: Object.assign(new EventEmitter(), { writableLength: 0, write: () => true }),
    kill: () => true,
  });
  const supervisor = createBackendSupervisor({
    spawn: () => child, authorizedOrigin: 'cafe://app', getAuthorizedFrame: () => frame,
  });
  const caller = { origin: 'cafe://app', frame, isMainFrame: true };
  const projected = [];
  supervisor.subscribe(caller, snapshot => projected.push(snapshot));
  supervisor.start();
  const value = authority();
  child.stdout.emit('data', Buffer.from(JSON.stringify({
    kind: 'snapshot', authority_epoch: value.authority_epoch, revision: value.revision, state: value.state,
  }) + '\n'));
  assert.equal(supervisor.getStatus().availability, 'available');
  assert.deepEqual(supervisor.getStatus().state.preview_sources, value.state.preview_sources);
  assert.equal(Object.hasOwn(supervisor.handle(caller, 'getSnapshot').state, 'preview_sources'), false);
  assert.equal(Object.hasOwn(projected.at(-1).state, 'preview_sources'), false);
  const next = copy(value.state); next.preview_sources[0].source_id = 'window:102:0';
  child.stdout.emit('data', Buffer.from(JSON.stringify({
    kind: 'delta', authority_epoch: value.authority_epoch, base_revision: '3', revision: '4', state: next,
  }) + '\n'));
  assert.equal(supervisor.getStatus().state.preview_sources[0].source_id, 'window:102:0');
  assert.equal(Object.hasOwn(projected.at(-1).state, 'preview_sources'), false);
  supervisor.dispose();
});

const moduleUrl = filename => pathToFileURL(path.resolve(__dirname, '../../ui/src/capsule', filename)).href;
const flush = () => new Promise(resolve => setImmediate(resolve));
function mediaStream({ audio = false, ignoreLimits = false } = {}) {
  let settings = { width: 320, height: 180, frameRate: 15 };
  const listeners = new Map();
  const track = {
    readyState: 'live', stops: 0, constraints: [],
    stop() { this.stops++; this.readyState = 'ended'; },
    getSettings: () => settings,
    async applyConstraints(value) {
      this.constraints.push(copy(value));
      if (!ignoreLimits) settings = { width: value.width.max, height: value.height.max, frameRate: value.frameRate.max };
      else settings = { width: 1920, height: 1080, frameRate: 30 };
    },
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name, listener) => { if (listeners.get(name) === listener) listeners.delete(name); },
    end() { this.readyState = 'ended'; listeners.get('ended')?.(); },
  };
  const audioTrack = { stops: 0, stop() { this.stops++; } };
  return {
    track, audioTrack,
    getTracks: () => audio ? [track, audioTrack] : [track],
    getVideoTracks: () => [track], getAudioTracks: () => audio ? [audioTrack] : [],
  };
}
async function sessionFixture(options = {}) {
  const { createPreviewSession } = await import(moduleUrl('capture.js'));
  const states = [], streams = [], calls = [], requests = [], tasks = [], scheduledDelays = [];
  let size = { width: 320, height: 180 };
  let counter = 0, frameListener = null;
  const value = mediaStream();
  const session = createPreviewSession({
    bridge: { previewSource: async pair => {
      requests.push(pair);
      return options.grant ? options.grant(pair) : { ok: true, ...pair };
    } },
    getDisplayMedia: async constraints => { calls.push(constraints); return options.capture ? options.capture() : value; },
    observeFrames: (_stream, onFrame) => { frameListener = onFrame; return () => { if (frameListener === onFrame) frameListener = null; }; },
    onState: (state, reason) => states.push({ state, reason }),
    onStream: stream => streams.push(stream), size: () => size,
    schedule: (callback, delay) => {
      const id = ++counter; tasks.push({ id, callback, delay }); scheduledDelays.push(delay); return id;
    },
    cancel: id => { const at = tasks.findIndex(task => task.id === id); if (at >= 0) tasks.splice(at, 1); },
  });
  const binding = { capsule_id: 'cap-1', feed_id: 'feed-1', generation: '1' };
  return {
    session, states, streams, calls, requests, tasks, scheduledDelays, value, binding,
    frame: count => frameListener?.(count),
    resizeTo: next => { size = next; },
    start: async () => { session.setBinding(binding); session.setVisibility(true, true); await flush(); },
  };
}

test('preview uses video-only GDM, validates tile bounds and 15fps before attaching a stream', async () => {
  const f = await sessionFixture();
  await f.start();
  assert.deepEqual(f.requests, [{ capsule_id: 'cap-1', feed_id: 'feed-1' }]);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].audio, false);
  assert.equal(f.calls[0].video.frameRate.ideal, 15);
  assert.deepEqual(f.value.track.constraints[0], { width: { max: 320 }, height: { max: 180 }, frameRate: { max: 15 } });
  assert.equal(f.states.at(-1).state, 'connecting', 'track admission alone is not a displayed frame');
  f.frame(1);
  assert.equal(f.states.at(-1).state, 'live');
  assert.equal(f.streams.at(-1), f.value);
  f.resizeTo({ width: 160, height: 90 });
  await f.session.resize();
  assert.deepEqual(f.value.track.constraints.at(-1), { width: { max: 160 }, height: { max: 90 }, frameRate: { max: 15 } });
  f.session.dispose();
  assert.equal(f.value.track.stops, 1);
  assert.equal(f.streams.at(-1), null);
});

test('hidden tiles never acquire and hiding or rebinding a tile stops all of its tracks', async () => {
  const values = [];
  const f = await sessionFixture({ capture: () => { const value = mediaStream(); values.push(value); return value; } });
  f.session.setBinding(f.binding);
  f.session.setVisibility(false, true);
  await flush();
  assert.equal(f.calls.length, 0);
  f.session.setVisibility(true, true);
  await flush();
  assert.equal(values.length, 1);
  f.session.setVisibility(false, true);
  assert.equal(values[0].track.stops, 1);
  assert.equal(f.streams.at(-1), null);
  f.session.setVisibility(true, true);
  await flush();
  f.session.setBinding({ ...f.binding, feed_id: 'feed-2', generation: '2' });
  assert.equal(values[1].track.stops, 1);
  await flush();
  assert.equal(f.requests.at(-1).feed_id, 'feed-2');
  f.session.dispose();
  assert.equal(values[2].track.stops, 1);
});

test('late streams after disposal are stopped and cannot update the tile', async () => {
  let resolveCapture;
  const f = await sessionFixture({ capture: () => new Promise(resolve => { resolveCapture = resolve; }) });
  await f.start();
  assert.equal(f.calls.length, 1);
  f.session.dispose();
  const value = mediaStream();
  resolveCapture(value);
  await flush();
  assert.equal(value.track.stops, 1);
  assert.equal(f.streams.includes(value), false);
  assert.equal(f.states.some(row => row.state === 'live'), false);
});

test('source ending stops tracks and recovery retries have a finite schedule', async () => {
  let attempts = 0;
  const value = mediaStream();
  const f = await sessionFixture({ capture: () => {
    attempts++;
    if (attempts === 1) return value;
    throw new Error('SYNTHETIC_PROVIDER_PRIVATE_CANARY');
  } });
  await f.start();
  value.track.end();
  assert.equal(value.track.stops, 1);
  assert.equal(f.streams.at(-1), null);
  while (f.tasks.length) {
    const pending = f.tasks.shift(); pending.callback(); await flush();
  }
  assert.deepEqual(f.scheduledDelays, [500, 1500, 3000]);
  assert.equal(attempts, 4);
  assert.equal(f.states.at(-1).state, 'no_preview');
  assert.equal(JSON.stringify(f.states).includes('SYNTHETIC_PROVIDER_PRIVATE_CANARY'), false);
  f.session.dispose();
});

test('disabled/denied/unlisted previews without a changed current projection schedule no retry', async () => {
  for (const error of ['preview_disabled', 'preview_denied', 'no_preview']) {
    const f = await sessionFixture({ grant: () => ({ ok: false, error }) });
    await f.start();
    assert.equal(f.calls.length, 0);
    assert.equal(f.tasks.length, 0);
    assert.deepEqual(f.states.at(-1), { state: 'no_preview', reason: error });
    f.session.dispose();
  }
});

test('audio tracks and noncompliant size/fps are stopped before a preview reaches video', async () => {
  for (const value of [mediaStream({ audio: true }), mediaStream({ ignoreLimits: true })]) {
    const f = await sessionFixture({ capture: () => value });
    await f.start();
    assert.equal(f.streams.includes(value), false);
    assert.equal(value.track.stops, 1);
    if (value.getAudioTracks().length) assert.equal(value.audioTrack.stops, 1);
    assert.equal(f.states.at(-1).state, 'no_preview');
    f.session.dispose();
  }
});

test('all tile states and elapsed text derive from explicit authority or actual preview state', async () => {
  const { tileState, elapsedLabel, planSecondsRemaining } = await import(moduleUrl('control.js'));
  const cap = { feeds: [{ feed_id: 'feed-1' }] };
  assert.equal(tileState(cap, null, {}), 'no_preview');
  assert.equal(tileState(cap, null, { 'feed-1': 'connecting' }), 'connecting');
  assert.equal(tileState(cap, null, { 'feed-1': 'live' }), 'live');
  assert.equal(tileState(cap, { state: 'Running' }, {}), 'recording');
  assert.equal(tileState(cap, { state: 'Failed', reason: 'recording_capture_source_failed' }, {}), 'recording_failed');
  assert.equal(tileState({ feeds: [{ window_state: 'window_minimized' }] }, null, {}), 'window_minimized');
  assert.equal(tileState({ feeds: [{ window_state: 'window_closed' }] }, null, {}), 'window_closed');
  assert.equal(elapsedLabel(null), '—');
  assert.equal(elapsedLabel('65000'), '01:05');
  assert.equal(elapsedLabel('3661000'), '01:01:01');
  assert.equal(planSecondsRemaining('10001', 10000), 1);
  assert.equal(planSecondsRemaining('10000', 10000), 0);
  assert.equal(planSecondsRemaining('unknown', 10000), null);
});

test('batch confirmation waits for the matching authority result and never treats queued as success', async () => {
  const { waitForAuthorityResult } = await import(moduleUrl('control.js'));
  let listener = () => {}, removed = 0, settled = false;
  const current = { availability: 'available', lastResult: { state: 'committed', requestId: 'request-other' } };
  const controls = {
    getState: () => current,
    subscribe: fn => { listener = fn; fn(current); return () => { removed++; }; },
  };
  const pending = waitForAuthorityResult(controls, 'request-1', { timeoutMs: 1000 }).then(value => { settled = true; return value; });
  listener({ ...current, lastResult: { state: 'pending', requestId: 'request-1' } });
  await flush();
  assert.equal(settled, false);
  listener({ ...current, lastResult: { state: 'rejected', code: 'recording_capture_source_failed', requestId: 'request-1' } });
  assert.equal((await pending).state, 'rejected');
  assert.equal(removed, 1);
});

const uiFixture = require('../fixtures/capsule_control');

test('actual Vue wall renders twelve authoritative tiles with independent REC controls', async () => {
  const m = await uiFixture.mount(async () => authority(12));
  try {
    const tiles = uiFixture.findAll(m.tree, node => node.tag === 'article');
    assert.equal(tiles.length, 12);
    const gallery = uiFixture.findAll(m.tree, node => node.props['data-capsule-id'] !== undefined);
    assert.equal(gallery.length, 12);
    assert.deepEqual(new Set(gallery.map(row => row.props['data-capsule-id'])), new Set(Object.keys(authority(12).state.capsules)));
    for (const tile of tiles) assert.ok(uiFixture.button(tile, 'REC'));
    assert.equal(uiFixture.button(m.tree, 'Add window').props.disabled, true);
    assert.equal(m.calls.intents, 0);
    assert.equal(uiFixture.findAll(m.tree, node => node.props.id === 'cafe-setup-title').length, 0);
    await m.click('Settings');
    assert.equal(uiFixture.findAll(m.tree, node => node.props.id === 'cafe-setup-title').length, 1);
    assert.equal(m.calls.intents, 0);
  } finally { m.done(); }
});

test('actual organizer panel under Ask me before AI actions renders untrusted titles as text and requires confirmation', async () => {
  const value = authority();
  value.state.ask_before_ai_actions = true;
  value.state.organizer_plan = {
    plan_id: 'plan-ui', windows: [{ title: '<em data-title="synthetic">Synthetic call</em>' }],
    folder_name: '<b>Synthetic folder</b>', expires_unix_ms: String(Date.now() + 60000),
  };
  const m = await uiFixture.mount(async () => value, { submit: async () => ({ queued: true }) });
  try {
    assert.equal(m.calls.intents, 0);
    assert.equal(uiFixture.findAll(m.tree, node => node.tag === 'em' || node.tag === 'b').length, 0);
    assert.ok(uiFixture.textOf(m.tree).includes(value.state.organizer_plan.windows[0].title));
    assert.ok(uiFixture.textOf(m.tree).includes(value.state.organizer_plan.folder_name));
    const confirm = uiFixture.button(m.tree, 'Confirm');
    assert.ok(confirm);
    assert.equal(confirm.props.autofocus, undefined);
    assert.equal(confirm.props.type, 'button');
    assert.match(uiFixture.squash(uiFixture.textOf(m.tree)), /Expires in \d+s/);
    await m.click('Settings');
    assert.ok(uiFixture.button(m.tree, 'Confirm'), 'settings navigation cannot dismiss the plan');
    assert.equal(m.calls.intents, 0);
    await m.click('Confirm');
    assert.deepEqual(m.calls.requests.map(row => row.body), [{ op: 'organizer_plan_confirm', plan_id: 'plan-ui' }]);
    assert.ok(uiFixture.button(m.tree, 'Confirm'), 'queued response does not remove the plan');
    const confirmed = copy(value); confirmed.revision = '4'; confirmed.state.organizer_plan = null;
    confirmed.state.last_result = { state: 'committed', code: 'snapshot', request_id: m.calls.requests[0].request_id };
    await m.push(confirmed);
    assert.equal(uiFixture.button(m.tree, 'Confirm'), undefined);
  } finally { m.done(); }
});

test('actual recording failure shows the closed authority reason with Retry', async () => {
  const value = authority();
  value.state.recordings = [{ capsule_id: 'cap-1', state: 'Failed', reason: 'recording_capture_source_failed', feeds: [] }];
  const m = await uiFixture.mount(async () => value);
  try {
    const tile = uiFixture.findAll(m.tree, node => node.tag === 'article')[0];
    assert.equal(tile.props['data-tile-state'], 'recording_failed');
    assert.ok(uiFixture.button(tile, 'Retry'));
    assert.ok(uiFixture.textOf(tile).includes('The Windows capture source failed.'));
    assert.equal(m.calls.intents, 0);
    const next = copy(value); next.revision = '4';
    next.state.recordings[0].reason = 'SYNTHETIC_PROVIDER_PRIVATE_CANARY';
    await m.push(next);
    assert.equal(uiFixture.textOf(m.tree).includes('SYNTHETIC_PROVIDER_PRIVATE_CANARY'), false);
  } finally { m.done(); }
});

test('AI confirmation preference is local, authority-driven and absent capability stays closed', async () => {
  const f = await controlsFixture();
  await assert.rejects(f.controls.setAskBeforeAiActions(true));
  assert.equal(f.sent.length, 0); f.controls.dispose();
  const value = authority(); value.state.ask_before_ai_actions = false;
  value.state.ai_activity = { code: 'ai_recording_submitted', count: 2, title: '<em>untrusted</em>' };
  const m = await uiFixture.mount(async () => value, { submit: async () => ({ queued: true }) });
  try {
    assert.ok(uiFixture.button(m.tree, 'Stop all'));
    assert.ok(uiFixture.button(m.tree, 'REC'));
    assert.match(uiFixture.textOf(m.tree), /AI submitted recording for 2 windows/);
    assert.equal(uiFixture.textOf(m.tree).includes('untrusted'), false);
    assert.equal(uiFixture.button(m.tree, 'Confirm'), undefined);
    assert.equal(m.calls.intents, 0);
    await m.click('Settings');
    const labels = () => uiFixture.findAll(m.tree, node => node.tag === 'label' && uiFixture.textOf(node).includes('Ask me before AI actions'));
    const toggle = uiFixture.findAll(labels()[0], node => node.tag === 'input')[0];
    assert.equal(toggle.props.checked, false);
    const event = { target: { checked: true } }; toggle.props.onChange(event);
    await uiFixture.settle(m.ui.nextTick);
    assert.deepEqual(m.calls.requests[0].body, { op: 'set_ask_before_ai_actions', enabled: true });
    assert.equal(event.target.checked, false);
    const next = copy(value); next.revision = '4'; next.state.ask_before_ai_actions = true;
    next.state.ai_activity = { code: '<em>untrusted</em>', count: 2 };
    await m.push(next);
    assert.equal(uiFixture.findAll(labels()[0], node => node.tag === 'input')[0].props.checked, true);
    await m.click('Back to wall');
    assert.equal(uiFixture.textOf(m.tree).includes('untrusted'), false);
  } finally { m.done(); }
});

test('auto-anchor settings stay hidden when absent and reflect only an authoritative boolean', async () => {
  const m = await uiFixture.mount(async () => authority(), { submit: async () => ({ queued: true }) });
  try {
    await m.click('Settings');
    const labels = () => uiFixture.findAll(m.tree, node => node.tag === 'label' && uiFixture.textOf(node).includes('Auto-anchor Discord pop-outs'));
    assert.equal(labels().length, 0);
    const value = authority(); value.revision = '4'; value.state.auto_anchor = false;
    await m.push(value);
    assert.equal(labels().length, 1);
    const toggle = uiFixture.findAll(labels()[0], node => node.tag === 'input')[0];
    assert.equal(toggle.props.checked, false);
    const event = { target: { checked: true } };
    toggle.props.onChange(event);
    await uiFixture.settle(m.ui.nextTick);
    assert.deepEqual(m.calls.requests[0].body, { op: 'set_auto_anchor', enabled: true });
    assert.equal(event.target.checked, false);
    assert.equal(toggle.props.checked, false);
    const next = copy(value); next.revision = '5'; next.state.auto_anchor = true;
    await m.push(next);
    assert.equal(uiFixture.findAll(labels()[0], node => node.tag === 'input')[0].props.checked, true);
  } finally { m.done(); }
});

test('Record all invokes one capsule at a time and reports each authority result independently', async () => {
  const m = await uiFixture.mount(async () => authority(2), { submit: async () => ({ queued: true }) });
  try {
    const batch = uiFixture.button(m.tree, 'Record all').props.onClick();
    await uiFixture.settle(m.ui.nextTick);
    assert.equal(m.calls.requests.length, 1);
    assert.deepEqual(m.calls.requests[0].body, { op: 'start_recording', capsule_id: 'cap-1' });
    assert.doesNotMatch(uiFixture.textOf(m.tree), /Recording state: Running/);
    const first = authority(2); first.revision = '4';
    first.state.recordings = [{ capsule_id: 'cap-1', state: 'Running', elapsed_ms: '1000', feeds: [] }];
    first.state.last_result = { state: 'committed', code: 'snapshot', request_id: m.calls.requests[0].request_id };
    await m.push(first);
    assert.equal(m.calls.requests.length, 2);
    assert.deepEqual(m.calls.requests[1].body, { op: 'start_recording', capsule_id: 'cap-2' });
    assert.equal(m.calls.requests[1].expected_revision, '4');
    const second = copy(first); second.revision = '5';
    second.state.last_result = { state: 'rejected', code: 'recording_capture_source_failed', request_id: m.calls.requests[1].request_id };
    await m.push(second); await batch;
    const tiles = uiFixture.findAll(m.tree, node => node.tag === 'article');
    assert.equal(tiles[0].props['data-tile-state'], 'recording');
    assert.equal(tiles[1].props['data-tile-state'], 'no_preview');
    assert.match(uiFixture.textOf(tiles[1]), /Authority refused/);
    assert.equal(m.calls.requests.length, 2);
  } finally { m.done(); }
});

test('Stop all sends separate stop commands and preserves recordings until authority changes', async () => {
  const value = authority(2);
  value.state.recordings = [1, 2].map(id => ({ capsule_id: `cap-${id}`, state: 'Running', elapsed_ms: '1000', feeds: [] }));
  const m = await uiFixture.mount(async () => value, { submit: async () => ({ queued: true }) });
  try {
    const batch = uiFixture.button(m.tree, 'Stop all').props.onClick();
    await uiFixture.settle(m.ui.nextTick);
    assert.equal(m.calls.requests.length, 1);
    assert.deepEqual(m.calls.requests[0].body, { op: 'stop_recording', capsule_id: 'cap-1' });
    assert.equal(uiFixture.findAll(m.tree, node => node.props['data-tile-state'] === 'recording').length, 2);
    const first = copy(value); first.revision = '4'; first.state.recordings[0].state = 'Stopped';
    first.state.last_result = { state: 'committed', code: 'snapshot', request_id: m.calls.requests[0].request_id };
    await m.push(first);
    assert.equal(m.calls.requests.length, 2);
    assert.deepEqual(m.calls.requests[1].body, { op: 'stop_recording', capsule_id: 'cap-2' });
    const second = copy(first); second.revision = '5'; second.state.recordings[1].state = 'Stopped';
    second.state.last_result = { state: 'committed', code: 'snapshot', request_id: m.calls.requests[1].request_id };
    await m.push(second); await batch;
    assert.equal(uiFixture.findAll(m.tree, node => node.props['data-tile-state'] === 'recording').length, 0);
    assert.equal(m.calls.requests.length, 2);
  } finally { m.done(); }
});

test('missing or faulty optional preview module keeps the packaged shell available and permissions closed', async () => {
  const fs = require('node:fs'), vm = require('node:vm');
  const { createRequire } = require('node:module');
  const file = path.resolve(__dirname, '../../src/main/shellSecurity.js');
  const source = fs.readFileSync(file, 'utf8');
  const originalRequire = createRequire(file);
  for (const fault of ['missing', 'factory']) {
    const exported = {};
    const module = { exports: exported };
    const sandboxRequire = name => {
      if (name !== './capture_sources') return originalRequire(name);
      if (fault === 'missing') {
        const error = new Error('SYNTHETIC_OPTIONAL_MODULE_MISSING'); error.code = 'MODULE_NOT_FOUND'; throw error;
      }
      return { createPreviewSources() { throw new Error('SYNTHETIC_PROVIDER_PRIVATE_CANARY'); } };
    };
    vm.runInNewContext(source, { module, exports: exported, require: sandboxRequire, Buffer, Response }, { filename: file });
    const shell = module.exports;
    let captures = 0;
    const broker = shell.createPreviewSources({ getSources: async () => { captures++; return []; } });
    assert.deepEqual(copy(await broker.request({}, {})), { ok: false, error: 'preview_unavailable' });
    assert.equal(captures, 0);
    const callbacks = {};
    const session = {
      setDisplayMediaRequestHandler: fn => { callbacks.display = fn; },
      setPermissionRequestHandler: fn => { callbacks.request = fn; },
      setPermissionCheckHandler: fn => { callbacks.check = fn; },
      webRequest: { onBeforeRequest() {}, onHeadersReceived() {} },
    };
    shell.hardenSession(session, undefined, broker);
    for (const permission of ['media', 'display-capture', 'notifications']) {
      let allowed;
      callbacks.request({}, permission, value => { allowed = value; }, { mediaTypes: ['video'], isMainFrame: true });
      assert.equal(allowed, false);
      assert.equal(callbacks.check({}, permission, 'cafe://app', { mediaType: 'video', isMainFrame: true }), false);
    }
    let selected;
    callbacks.display({}, value => { selected = value; });
    assert.equal(selected, null);
    const options = shell.browserOptions(path.resolve(__dirname, '../../src/preload/index.js'));
    assert.equal(options.webPreferences.sandbox, true);
    assert.equal(options.webPreferences.contextIsolation, true);
    const frame = { url: security.BOOT_URL, parent: null, detached: false };
    const contents = { mainFrame: frame, isDestroyed: () => false };
    const handlers = new Map();
    const cleanup = shell.bindNativeIpc({ handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) },
      () => ({ webContents: contents }), () => null, undefined, undefined, broker);
    const event = { sender: contents, senderFrame: frame };
    assert.equal(handlers.get('cafe:getSnapshot')(event).availability, 'unavailable');
    assert.equal((await handlers.get('cafe:previewSource')(event, { capsule_id: 'cap-1', feed_id: 'feed-1' })).error, 'preview_unavailable');
    assert.equal(captures, 0);
    cleanup(); assert.equal(handlers.size, 0); broker.dispose();
  }
});

test('a timed-out native capture leaves connecting, quarantines its grant, and stops its late stream', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let resolveCapture;
  const first = await sessionFixture({ capture: () => new Promise(resolve => { resolveCapture = resolve; }) });
  const second = await sessionFixture();
  try {
    await first.start();
    await second.start();
    assert.equal(first.calls.length, 1);
    assert.equal(second.requests.length, 0, 'another tile cannot arm a grant while the previous GDM is unresolved');
    t.mock.timers.tick(8001);
    await flush();
    assert.equal(first.states.at(-1).state, 'no_preview');
    assert.equal(second.requests.length, 0, 'timeout cannot let a late GDM steal another tile selection');
    const late = mediaStream(); resolveCapture(late); await flush();
    assert.equal(late.track.stops, 1);
    assert.equal(first.streams.includes(late), false);
    assert.equal(second.requests.length, 1);
    assert.equal(second.states.at(-1).state, 'connecting');
    second.frame(1);
    assert.equal(second.states.at(-1).state, 'live');
  } finally {
    first.session.dispose(); second.session.dispose(); t.mock.timers.reset();
  }
});


test('trim accepts only bounded integer metadata and keeps rows closed', async () => {
  for (const trim of [undefined, 0, 23, 2160, -1, 2161, 1.5, '23', null]) {
    const f = previewFixture();
    if (trim !== undefined) f.status().state.preview_sources[0].top_trim = trim;
    const valid = trim === undefined || [0, 23, 2160].includes(trim);
    assert.equal((await f.grant()).ok, valid, String(trim));
    if (valid) { assert.ok(await f.display()); assert.equal(await f.display(), null); }
    else assert.throws(() => security.publicSnapshot(f.status()), /SNAPSHOT_REJECTED/);
    f.broker.dispose();
  }
  const f = previewFixture();
  Object.assign(f.status().state.preview_sources[0], { top_trim: 23, unexpected: true });
  assert.equal((await f.grant()).ok, false);f.broker.dispose();
});

test('safe preview trim reaches controller without source identity and malformed metadata is refused', async () => {
  const raw = authority();raw.state.preview_sources[0].top_trim = 23;
  const projected = security.publicSnapshot(raw);
  assert.equal(JSON.stringify(projected).includes('window:101:0'), false);
  const f = await controlsFixture(projected);
  assert.equal(f.controls.getState().capsules[0].feeds[0].top_trim, 23);
  for (const trim of [-1, 2161, 0.5, null, '23']) {
    const next = copy(projected);next.state.preview_trims[0].top_trim = trim;
    f.publish(next);assert.equal(f.controls.getState().availability, 'unavailable');
  }
  const extra = copy(projected);extra.state.preview_trims[0].source_id = 'window:101:0';
  f.publish(extra);assert.equal(f.controls.getState().availability, 'unavailable');
  const legacy = authority();delete legacy.state.preview_sources;
  f.publish(legacy);assert.equal(f.controls.getState().capsules[0].feeds[0].top_trim, 0);
  f.controls.dispose();
});

test('cropped preview fits decoded dimensions at any tile aspect ratio without distortion', async () => {
  const { previewGeometry } = await import(moduleUrl('capture.js'));
  for (const [width, height] of [[100, 100], [1920, 180], [80, 1080], [1, 1]]) {
    const g = previewGeometry(width, height, 856, 481, 23);
    assert.ok(g.width <= width + 1e-9 && g.height <= height + 1e-9);
    assert.ok(Math.abs(g.width / 856 - g.videoHeight / 481) < 1e-9);
    assert.ok(Math.abs(g.height - (g.videoHeight + g.top)) < 1e-9);
    assert.ok(Math.abs(g.top / g.videoHeight + 23 / 481) < 1e-9);
  }
  assert.equal(previewGeometry(100, 100, 0, 0, 23), null);
  assert.equal(previewGeometry(100, 100, 856, 481, 481).top, -0);
  assert.equal(previewGeometry(100, 100, 856, 481, 0).top, -0);
});

test('running chat is reported as running, not as setup pending', async () => {
  const value = authority();
  value.state.chat_configurable = false;
  value.state.chat_available = true;
  const m = await uiFixture.mount(async () => value);
  try {
    await m.click('Settings');
    const text = uiFixture.textOf(m.tree);
    assert.ok(text.includes('Chat is running'));
    assert.equal(text.includes('Chat setup pending'), false);
  } finally { m.done(); }
});
