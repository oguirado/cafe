// @ts-check
'use strict';
/** Synthetic presentation tests only; no native approval or live effect is asserted. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const COMPONENT = path.resolve(__dirname, '../../ui/src/bridge/control.vue');
const SCHEMA = path.resolve(__dirname, '../../ui/src/bridge/control_schema.js');
const NOW = 100000;
const REJECTED = { ok: false, view: null };
const FIELDS = ['capsule_id', 'expires_at_ms', 'op', 'proposal_id', 'role', 'slot'];

/** @param {object} [changes] */
function proposal(changes = {}) {
  return { proposal_id: 'synthetic-proposal-a', op: 'start_recording',
    capsule_id: 'synthetic-capsule-a', slot: 'slot-7', role: null,
    expires_at_ms: NOW + 30000, ...changes };
}

function schema() { return import(pathToFileURL(SCHEMA).href); }

/** Compile the actual SFC. Only its declared imports are injected, never fixture text. */
async function component() {
  const Vue = require('vue');
  const { parse, compileScript, compileStyle } = require('vue/compiler-sfc');
  const source = fs.readFileSync(COMPONENT, 'utf8');
  const parsed = parse(source, { filename: 'control.vue' });
  assert.deepEqual(parsed.errors, []);
  assert.ok(parsed.descriptor.scriptSetup, 'the actual script-setup component is compiled');
  assert.equal(parsed.descriptor.scriptSetup.lang, undefined);
  assert.ok(parsed.descriptor.template, 'the actual template is compiled');
  for (const style of parsed.descriptor.styles) {
    const result = compileStyle({ source: style.content, filename: 'control.vue',
      id: 'data-v-control-test', scoped: style.scoped });
    assert.deepEqual(result.errors, []);
  }
  const compiled = compileScript(parsed.descriptor, { id: 'control-test',
    genDefaultAs: '__component', inlineTemplate: true,
    templateOptions: { compilerOptions: { hoistStatic: false } } });
  const body = compiled.content.replace(
    /import\s+\{([^}]+)\}\s+from\s+(['"])([^'"]+)\2;?/g,
    (_whole, bindings, _quote, specifier) => {
      assert.ok(specifier === 'vue' || specifier === './control_schema.js',
        'component imports must stay inside the presentation boundary');
      const names = bindings.split(',').map(name => name.trim()).filter(Boolean);
      for (const name of names) assert.match(name, /^[$\w]+(?:\s+as\s+[$\w]+)?$/);
      const destructured = names.map(name => name.replace(/\s+as\s+/, ': ')).join(', ');
      return `const { ${destructured} } = ${specifier === 'vue' ? '__vue' : '__schema'};`;
    });
  const Component = new Function('__vue', '__schema', `'use strict';\n${body}\nreturn __component;`)(Vue, await schema());
  return { Vue, Component };
}

/** A tiny Vue host: it records rendered nodes, not a substitute component implementation. */
function host(Vue) {
  const node = (tag, text = '') => ({ tag, text, props: {}, children: [], parent: null });
  function remove(child) {
    if (child.parent) {
      const index = child.parent.children.indexOf(child);
      if (index >= 0) child.parent.children.splice(index, 1);
      child.parent = null;
    }
  }
  const renderer = Vue.createRenderer({
    createElement: tag => node(tag), createText: text => node('#text', text),
    createComment: text => node('#comment', text),
    setText: (target, text) => { target.text = text; },
    setElementText: (target, text) => { target.children.forEach(child => { child.parent = null; }); target.children = []; target.text = text; },
    patchProp: (target, key, _previous, value) => { target.props[key] = value; },
    insert(child, parent, anchor = null) {
      remove(child);
      const index = anchor === null ? -1 : parent.children.indexOf(anchor);
      parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
      child.parent = parent;
    },
    remove, parentNode: target => target.parent,
    nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
    setScopeId: (target, id) => { target.props[id] = ''; },
    insertStaticContent() { throw new Error('UNEXPECTED_STATIC_HOST_NODE'); },
  });
  return { renderer, root: node('root') };
}

function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
function textOf(node) { return node.tag === '#comment' ? '' : node.text + node.children.map(textOf).join(' '); }

/** Real Vue renders and emits; only time and the display host are synthetic. */
async function mount(t, overrides = {}, onConfirm = () => {}) {
  const { Vue, Component } = await component();
  let now = NOW;
  const timers = new Map();
  const mocks = [];
  let sequence = 0;
  mocks.push(t.mock.method(Date, 'now', () => now));
  for (const name of ['setInterval', 'setTimeout']) {
    mocks.push(t.mock.method(globalThis, name, (callback, _delay) => {
      const handle = ++sequence;
      timers.set(handle, { callback, once: name === 'setTimeout' });
      return handle;
    }));
  }
  for (const name of ['clearInterval', 'clearTimeout']) {
    mocks.push(t.mock.method(globalThis, name, handle => { timers.delete(handle); }));
  }
  const props = Vue.shallowReactive({ proposal: Vue.reactive(proposal()),
    availability: 'available', pending: Vue.reactive(new Set(['synthetic-proposal-a'])), ...overrides });
  const events = [], errors = [], warnings = [];
  const { renderer, root } = host(Vue);
  const app = renderer.createApp({ render: () => Vue.h(Component, { ...props,
    onConfirm: payload => { events.push({ name: 'confirm', payload }); onConfirm(payload); },
    onReject: payload => { events.push({ name: 'reject', payload }); },
  }) });
  app.config.errorHandler = () => { errors.push('component_error'); };
  app.config.warnHandler = () => { warnings.push('component_warning'); };
  app.mount(root);
  await Vue.nextTick();
  let mounted = true;
  function unmount() {
    if (mounted) {
      mounted = false; app.unmount();
      // Each loop fixture restores its own globals before another fixture mounts.
      for (const mocked of mocks.slice().reverse()) mocked.mock.restore();
    }
  }
  t.after(() => {
    unmount();
    assert.deepEqual(errors, [], 'malformed presentation data must not escape through Vue errors');
    assert.deepEqual(warnings, [], 'no warning may echo untrusted presentation values');
    assert.equal(timers.size, 0, 'unmount clears presentation timers');
  });
  function button(label) { return descendants(root).find(node => node.tag === 'button' && textOf(node).trim() === label); }
  function handler(label) {
    const control = button(label);
    assert.ok(control, `explicit ${label} control is rendered`);
    assert.equal(control.props.type, 'button');
    assert.equal(typeof control.props.onClick, 'function');
    return control.props.onClick;
  }
  function click(label) {
    const control = button(label);
    assert.ok(control, `explicit ${label} control is rendered`);
    if (typeof control.props.onClick !== 'function') {
      assert.equal(control.props.disabled, true, 'a control without a handler must be disabled');
      return;
    }
    handler(label)({ type: 'click' });
  }
  return { Vue, Component, props, events, root, button, handler, unmount,
    text: () => textOf(root),
    click,
    replace: async changes => { Object.assign(props, changes); await Vue.nextTick(); },
    now: value => { now = value; },
    tick: async () => {
      for (const [handle, timer] of [...timers]) {
        if (timer.once) timers.delete(handle);
        timer.callback();
      }
      await Vue.nextTick();
    },
  };
}

test('schema: all four operations return a detached closed view and preserve namespaces', async () => {
  const { validateProposalView } = await schema();
  for (const op of ['bind_window', 'start_recording', 'stop_recording', 'send_to_obs']) {
    for (const role of op === 'bind_window' ? ['game', 'camera'] : [null]) {
      const input = proposal({ op, role, proposal_id: 'synthetic-shared', capsule_id: 'synthetic-shared' });
      const result = validateProposalView(input, NOW);
      assert.equal(result.ok, true);
      assert.deepEqual(result.view, input);
      assert.notEqual(result.view, input);
      assert.deepEqual(Object.keys(result).sort(), ['ok', 'view']);
      assert.deepEqual(Reflect.ownKeys(result.view).sort(), FIELDS);
    }
  }
  for (const slot of [null, 'slot-0', 'slot-01', 'slot-17', 'slot-9007199254740993']) {
    assert.equal(validateProposalView(proposal({ slot }), NOW).ok, true);
  }
  for (const id of ['A', `A${'a'.repeat(95)}`, 'synthetic.A_b:c-7']) {
    assert.equal(validateProposalView(proposal({ proposal_id: id, capsule_id: id }), NOW).ok, true);
  }
});

test('schema: missing, malformed, duplicate-list and nonclosed views fail without partial output', async () => {
  const { validateProposalView } = await schema();
  for (const value of [null, undefined, false, 3, 'synthetic', [], [proposal(), proposal()], () => {}]) {
    assert.deepEqual(validateProposalView(value, NOW), REJECTED);
  }
  for (const key of FIELDS) {
    const missing = proposal(); delete missing[key];
    assert.deepEqual(validateProposalView(missing, NOW), REJECTED);
  }
  for (const key of ['token', 'folder', 'account_name', 'channel_name', 'secret', 'snapshot', 'frame_bytes', 'argv']) {
    assert.deepEqual(validateProposalView(proposal({ [key]: 'SYNTHETIC_PRIVATE_CANARY' }), NOW), REJECTED);
  }
  for (const id of ['', '_synthetic', 'synthetic id', 'synthetic/id', 'é', 'A'.repeat(97), 'synthetic\n', 'synthetic\r']) {
    for (const key of ['proposal_id', 'capsule_id']) assert.deepEqual(validateProposalView(proposal({ [key]: id }), NOW), REJECTED);
  }
  for (const slot of ['', 'slot-', 'slot--1', 'slot-1.5', 'slot-1\n', 'slot-1\r', 1, false]) {
    assert.deepEqual(validateProposalView(proposal({ slot }), NOW), REJECTED);
  }
  for (const change of [{ op: 'set_folder' }, { op: 'bind_window', role: null },
    { op: 'bind_window', role: 'other' }, { role: 'camera' }, { role: false }]) {
    assert.deepEqual(validateProposalView(proposal(change), NOW), REJECTED);
  }
});

test('schema: numeric millisecond deadlines reject unknown, unsafe and expired values', async () => {
  const { validateProposalView } = await schema();
  const invalid = [null, undefined, true, false, '100001', NaN, Infinity, -Infinity, -1, 0.5, Number.MAX_SAFE_INTEGER + 1];
  for (const value of invalid) {
    assert.deepEqual(validateProposalView(proposal({ expires_at_ms: value }), NOW), REJECTED);
    assert.deepEqual(validateProposalView(proposal(), value), REJECTED);
  }
  for (const deadline of [0, NOW - 1, NOW]) assert.deepEqual(validateProposalView(proposal({ expires_at_ms: deadline }), NOW), REJECTED);
  assert.equal(validateProposalView(proposal({ expires_at_ms: NOW + 1 }), NOW).ok, true);
  assert.equal(validateProposalView(proposal({ expires_at_ms: Number.MAX_SAFE_INTEGER }), NOW).ok, true);
  for (const clock of [0, true, null, '00', '-1', '18446744073709551616', '0', '18446744073709551615']) {
    for (const key of ['authority_epoch', 'revision', 'generation']) {
      assert.deepEqual(validateProposalView(proposal({ [key]: clock }), NOW), REJECTED);
    }
  }
});

test('schema: accessors, hidden keys and throwing or revoked proxies fail with a fixed result', async () => {
  const { validateProposalView } = await schema();
  let reads = 0;
  const accessor = proposal();
  Object.defineProperty(accessor, 'capsule_id', { enumerable: true,
    get() { reads += 1; throw new Error('SYNTHETIC_FAILURE_DETAIL'); } });
  const hidden = proposal(); Object.defineProperty(hidden, 'extra', { value: 'SYNTHETIC_PRIVATE_CANARY' });
  const symbol = proposal(); symbol[Symbol('synthetic-extra')] = true;
  const inherited = Object.assign(Object.create({ inherited: true }), proposal());
  const throwing = new Proxy(proposal(), { ownKeys() { throw new Error('SYNTHETIC_FAILURE_DETAIL'); } });
  const revoked = Proxy.revocable(proposal(), {}); revoked.revoke();
  for (const value of [accessor, hidden, symbol, inherited, throwing, revoked.proxy]) {
    assert.doesNotThrow(() => {
      const result = validateProposalView(value, NOW);
      assert.deepEqual(result, REJECTED);
      assert.equal(JSON.stringify(result).includes('SYNTHETIC_FAILURE_DETAIL'), false);
    });
  }
  assert.equal(reads, 0, 'untrusted getters are not invoked');
});

test('component: actual SFC has only the closed props and emits and explicit controls', async t => {
  const f = await mount(t);
  assert.deepEqual(f.Component.props.slice().sort(), ['availability', 'pending', 'proposal']);
  assert.deepEqual(f.Component.emits.slice().sort(), ['confirm', 'reject']);
  assert.equal(f.Component.inheritAttrs, false);
  assert.equal(f.button('Confirm').props.disabled, false);
  assert.equal(f.button('Reject').props.disabled, false);
  assert.match(f.text(), /start recording/i);
  assert.match(f.text(), /slot-7/);
  assert.doesNotMatch(f.text(), /synthetic-proposal-a|synthetic-capsule-a/);
  assert.match(f.text(), /30/);
  assert.match(f.text(), /preview.*unavailable|no.*preview/i);
  assert.deepEqual(f.events, []);
  f.click('Confirm');
  assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: every operation and binding role renders and emits only a proposal identifier', async t => {
  for (const [op, role] of [['bind_window', 'game'], ['bind_window', 'camera'], ['stop_recording', null], ['send_to_obs', null]]) {
    const f = await mount(t, { proposal: proposal({ op, role, slot: null }) });
    assert.match(f.text(), /synthetic-capsule-a/);
    if (role) assert.match(f.text(), new RegExp(role, 'i'));
    f.click('Confirm');
    assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
    f.unmount();
  }
});

test('component: duplicate clicks, reentrant listeners and competing decisions are coalesced', async t => {
  let retry = () => {};
  const f = await mount(t, {}, () => retry());
  const confirm = f.handler('Confirm'), reject = f.handler('Reject');
  retry = () => confirm({ type: 'click' });
  confirm({ type: 'click' }); confirm({ type: 'click' }); reject({ type: 'click' });
  await f.Vue.nextTick();
  f.click('Confirm');
  assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: rejection remains explicit and single-use when native availability is lost', async t => {
  const f = await mount(t);
  await f.replace({ availability: 'unavailable' });
  assert.equal(f.button('Confirm').props.disabled, true);
  assert.equal(f.button('Reject').props.disabled, false);
  f.click('Confirm');
  const reject = f.handler('Reject'); reject({ type: 'click' }); reject({ type: 'click' });
  f.click('Confirm');
  assert.deepEqual(f.events, [{ name: 'reject', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: deadline is rechecked before a click even without a timer or repaint', async t => {
  const f = await mount(t);
  const confirm = f.handler('Confirm');
  f.now(NOW + 30000);
  confirm({ type: 'click' });
  assert.deepEqual(f.events, []);
  await f.tick();
  assert.equal(f.button('Confirm').props.disabled, true);
  await f.replace({ availability: 'unavailable' });
  assert.equal(f.button('Reject').props.disabled, false);
  f.click('Reject');
  assert.deepEqual(f.events, [{ name: 'reject', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: invalid current clocks never confirm', async t => {
  const f = await mount(t);
  const confirm = f.handler('Confirm');
  for (const value of [null, true, NaN, Infinity, -1, NOW + 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    f.now(value); confirm({ type: 'click' });
  }
  assert.deepEqual(f.events, []);
});

test('component: an observed expiry stays inhibited after a local clock rollback', async t => {
  const f = await mount(t);
  const confirm = f.handler('Confirm');
  f.now(NOW + 30000); confirm({ type: 'click' });
  f.now(NOW + 1000); confirm({ type: 'click' });
  await f.tick();
  f.click('Confirm');
  assert.deepEqual(f.events, []);
  f.click('Reject');
  assert.deepEqual(f.events, [{ name: 'reject', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: unavailable, unknown and lost runtime never appear as empty capsules', async t => {
  for (const availability of ['unavailable', 'unknown', 'starting', null, undefined, true]) {
    const f = await mount(t, { availability });
    assert.match(f.text(), /unavailable/i);
    assert.doesNotMatch(f.text(), /no capsules|empty capsules/i);
    f.click('Confirm');
    assert.deepEqual(f.events, []);
    f.unmount();
  }
  const f = await mount(t);
  const old = f.handler('Confirm');
  await f.replace({ proposal: null, pending: null, availability: 'unavailable' });
  old({ type: 'click' });
  assert.deepEqual(f.events, []);
  assert.match(f.text(), /unavailable/i);
});

test('component: unknown, duplicate, missing and oversized pending identity collections fail closed', async t => {
  for (const pending of [null, undefined, [], ['synthetic-proposal-a', 'synthetic-proposal-a'],
    {}, new Set(), new Set(['synthetic-other']), new Set(['synthetic-proposal-a', '']),
    new Set(['synthetic-proposal-a', ...Array.from({ length: 64 }, (_, n) => `synthetic-other-${n}`)])]) {
    const f = await mount(t, { pending });
    f.click('Confirm');
    assert.deepEqual(f.events, []);
    f.unmount();
  }
  const f = await mount(t, { pending: new Set(['synthetic-proposal-a',
    ...Array.from({ length: 63 }, (_, n) => `synthetic-other-${n}`)]) });
  f.click('Confirm');
  assert.equal(f.events.length, 1, 'the full existing pending capacity remains supported');
});

test('component: forged Set size and duplicate-producing iterators cannot invent pending membership', async t => {
  for (const duplicate of [false, true]) {
    const pending = new Proxy(new Set(), { get(target, key) {
      if (key === 'size') return duplicate ? 2 : 1;
      if (key === Symbol.iterator) return function* () {
        yield 'synthetic-proposal-a';
        if (duplicate) yield 'synthetic-proposal-a';
      };
      return Reflect.get(target, key, target);
    } });
    const f = await mount(t, { pending });
    f.click('Confirm'); f.click('Reject');
    assert.deepEqual(f.events, []);
    f.unmount();
  }
});

test('component: malformed views never display untrusted content or emit a decision', async t => {
  const missing = proposal(); delete missing.proposal_id;
  for (const value of [null, missing, proposal({ proposal_id: '' }), proposal({ capsule_id: '' }),
    proposal({ expires_at_ms: NOW }), proposal({ role: 'camera' }),
    proposal({ snapshot: 'SYNTHETIC_PRIVATE_CANARY' })]) {
    const f = await mount(t, { proposal: value });
    assert.doesNotMatch(f.text(), /SYNTHETIC_PRIVATE_CANARY/);
    for (const label of ['Confirm', 'Reject']) if (f.button(label)) f.click(label);
    assert.deepEqual(f.events, []);
    f.unmount();
  }
});

test('component: hostile view descriptors produce fixed unavailable text without logging exceptions', async t => {
  let getterReads = 0;
  const accessor = proposal();
  Object.defineProperty(accessor, 'capsule_id', { enumerable: true, get() {
    getterReads += 1;
    throw new Error('SYNTHETIC_FAILURE_DETAIL');
  } });
  const throwing = new Proxy(proposal(), { ownKeys() { throw new Error('SYNTHETIC_FAILURE_DETAIL'); } });
  const logged = [];
  for (const name of ['log', 'warn', 'error']) t.mock.method(console, name, () => { logged.push(name); });
  for (const value of [accessor, throwing]) {
    const f = await mount(t, { proposal: value });
    f.click('Confirm'); f.click('Reject');
    assert.match(f.text(), /unavailable/i);
    assert.doesNotMatch(f.text(), /SYNTHETIC_FAILURE_DETAIL/);
    assert.deepEqual(f.events, []);
    f.unmount();
  }
  assert.equal(getterReads, 0);
  assert.deepEqual(logged, []);
});

test('component: already decided identifiers cannot emit again after another modal', async t => {
  const f = await mount(t);
  f.click('Confirm');
  await f.replace({ proposal: proposal({ proposal_id: 'synthetic-proposal-b' }),
    pending: new Set(['synthetic-proposal-a', 'synthetic-proposal-b']) });
  await f.replace({ proposal: proposal() });
  f.click('Confirm'); f.click('Reject');
  assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: replacing modal identity permanently kills obsolete handlers, including A to B to A', async t => {
  const f = await mount(t);
  const oldConfirm = f.handler('Confirm'), oldReject = f.handler('Reject');
  await f.replace({ proposal: proposal({ proposal_id: 'synthetic-proposal-b' }),
    pending: new Set(['synthetic-proposal-a', 'synthetic-proposal-b']) });
  oldConfirm({ type: 'click' }); oldReject({ type: 'click' });
  await f.replace({ proposal: proposal() });
  oldConfirm({ type: 'click' }); oldReject({ type: 'click' });
  assert.deepEqual(f.events, []);
  f.click('Confirm');
  assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: same-id content changes kill old closures before the next render', async t => {
  for (const change of [{ op: 'stop_recording' }, { capsule_id: 'synthetic-capsule-b' },
    { slot: 'slot-9' }, { expires_at_ms: NOW + 20000 }]) {
    const f = await mount(t);
    const oldConfirm = f.handler('Confirm'), oldReject = f.handler('Reject');
    Object.assign(f.props.proposal, change);
    oldConfirm({ type: 'click' }); oldReject({ type: 'click' });
    assert.deepEqual(f.events, []);
    await f.Vue.nextTick();
    f.click('Confirm');
    assert.equal(f.events.length, 1);
    f.unmount();
  }
  const f = await mount(t, { proposal: require('vue').reactive(proposal({ op: 'bind_window', role: 'game' })) });
  const old = f.handler('Confirm');
  f.props.proposal.role = 'camera'; old({ type: 'click' });
  assert.deepEqual(f.events, []);
});

test('component: transient reactive changes cannot revive saved handlers before paint', async t => {
  const f = await mount(t);
  const old = f.handler('Confirm');
  f.props.proposal.slot = 'slot-8';
  f.props.proposal.slot = 'slot-7';
  old({ type: 'click' });
  assert.deepEqual(f.events, []);
  await f.Vue.nextTick();
  f.click('Confirm');
  assert.equal(f.events.length, 1);
});

test('component: reentrant validation cannot revive a handler invalidated during its own click', async t => {
  for (const label of ['Confirm', 'Reject']) {
    const source = require('vue').reactive(proposal());
    let armed = false;
    const input = new Proxy(source, { ownKeys(target) {
      if (armed) {
        armed = false;
        target.slot = 'slot-8';
        target.slot = 'slot-7';
      }
      return Reflect.ownKeys(target);
    } });
    const f = await mount(t, { proposal: input });
    const saved = f.handler(label);
    armed = true;
    saved({ type: 'click' });
    assert.equal(armed, false, 'the real click performed fresh validation');
    assert.deepEqual(f.events, []);
    f.unmount();
  }
});

test('component: pending membership is checked again against in-place removal', async t => {
  const f = await mount(t);
  const confirm = f.handler('Confirm'), reject = f.handler('Reject');
  f.props.pending.delete('synthetic-proposal-a');
  confirm({ type: 'click' }); reject({ type: 'click' });
  assert.deepEqual(f.events, []);
  f.props.pending.add('synthetic-proposal-a');
  confirm({ type: 'click' });
  assert.deepEqual(f.events, []);
});

test('component: stale views and event-carried identities cannot redirect consent', async t => {
  const f = await mount(t);
  const saved = f.handler('Confirm');
  f.props.proposal.capsule_id = 'synthetic-capsule-b';
  saved({ type: 'click', proposal_id: 'synthetic-proposal-a' });
  assert.deepEqual(f.events, []);
  await f.Vue.nextTick();
  let reads = 0;
  const event = { type: 'click', get proposal_id() { reads += 1; throw new Error('SYNTHETIC_FAILURE_DETAIL'); } };
  f.handler('Confirm')(event);
  assert.equal(reads, 0);
  assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
});

test('component: timers, backdrop and Escape never decide; unmounted handlers stay dead', async t => {
  const f = await mount(t);
  f.now(NOW + 1000); await f.tick();
  assert.match(f.text(), /29/);
  for (const node of descendants(f.root)) {
    for (const name of ['onKeydown', 'onKeyup']) {
      const handleKey = node.props[name];
      if (typeof handleKey === 'function') handleKey({ key: 'Escape', type: 'keydown' });
    }
    if (node.tag !== 'button' && typeof node.props.onClick === 'function') node.props.onClick({ type: 'click' });
  }
  assert.deepEqual(f.events, []);
  const confirm = f.handler('Confirm'), reject = f.handler('Reject');
  f.unmount(); confirm({ type: 'click' }); reject({ type: 'click' });
  assert.deepEqual(f.events, []);
});

test('component: mounting, rendering and clicks never access native, storage, network or capture surfaces', async t => {
  // Initialize Vue before traps: its own environment discovery is not component code.
  require('vue'); require('vue/compiler-sfc');
  const reads = [];
  const originals = new Map();
  const forbidden = ['localStorage', 'sessionStorage', 'fetch', 'XMLHttpRequest',
    'WebSocket', 'EventSource', 'navigator', 'MediaRecorder', 'RTCPeerConnection',
    'cafe', 'preload', 'ipcRenderer'];
  originals.set('window', Object.getOwnPropertyDescriptor(globalThis, 'window'));
  // Vue may inspect HTMLElement when registering development hooks. No DOM exists.
  const browser = { HTMLElement: undefined };
  for (const name of forbidden) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    const descriptor = { configurable: true, get() {
      reads.push(name); throw new Error('SYNTHETIC_SURFACE_DENIED');
    } };
    Object.defineProperty(globalThis, name, descriptor);
    Object.defineProperty(browser, name, descriptor);
  }
  Object.defineProperty(globalThis, 'window', { configurable: true, value: browser });
  try {
    const f = await mount(t);
    f.now(NOW + 500); await f.tick();
    f.click('Confirm');
    f.unmount();
    assert.deepEqual(reads, []);
    assert.deepEqual(f.events, [{ name: 'confirm', payload: { proposal_id: 'synthetic-proposal-a' } }]);
  } finally {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});
