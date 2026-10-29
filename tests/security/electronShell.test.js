// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter, once } = require('node:events');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
const security = require('../../src/main/shellSecurity');
const preloadSecurity = require('../../src/preload/preloadSecurity');
const { createBackendSupervisor } = require('../../src/main/backendSupervisor');
const { createDesktopShell } = require('../../src/main/index');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate) {
  const deadline = performance.now() + 4000;
  while (performance.now() < deadline) { if (predicate()) return; await delay(10); }
  assert.fail('observable state deadline');
}
function nativeFixture(primary = true) {
  const trace = [], handlers = new Map(), windows = [], permission = {}, requests = {};
  const session = {
    setPermissionRequestHandler: (fn) => { permission.request = fn; },
    setPermissionCheckHandler: (fn) => { permission.check = fn; },
    webRequest: {
      onBeforeRequest: (_filter, fn) => { requests.before = fn; },
      onHeadersReceived: (fn) => { requests.headers = fn; },
    },
  };
  class Contents extends EventEmitter {
    constructor() { super(); this.mainFrame = { url: security.BOOT_URL, detached: false, parent: null }; this.sent = []; this.dead = false; this.session = session; }
    isDestroyed() { return this.dead; }
    setWindowOpenHandler(handler) { this.openHandler = handler; }
    send(channel, value) { this.sent.push({ channel, value }); }
  }
  class Window extends EventEmitter {
    constructor(options) { super(); this.options = options; this.webContents = new Contents(); this.shown = false; windows.push(this); trace.push('window'); }
    setMenu(menu) { this.menu = menu; }
    async loadURL(url) { this.loaded = url; this.webContents.mainFrame.url = url; this.emit('ready-to-show'); }
    show() { this.shown = true; }
    isDestroyed() { return this.webContents.dead; }
    destroy() { this.webContents.dead = true; this.emit('closed'); }
  }
  const app = new EventEmitter();
  Object.assign(app, {
    commandLine: { hasSwitch: () => false },
    requestSingleInstanceLock: () => { trace.push('lock'); return primary; },
    enableSandbox: () => { trace.push('sandbox'); },
    whenReady: async () => { trace.push('ready'); },
    getAppPath: () => path.resolve(__dirname, '../..'),
    quit: () => { trace.push('quit'); },
  });
  const protocol = {
    registerSchemesAsPrivileged: (schemes) => { trace.push('scheme'); protocol.schemes = schemes; },
    handle: (scheme, handler) => { protocol.scheme = scheme; protocol.handler = handler; },
    unhandle: () => {},
  };
  const ipcMain = {
    handle: (name, fn) => { assert.equal(handlers.has(name), false); handlers.set(name, fn); },
    removeHandler: (name) => { handlers.delete(name); },
  };
  return { electron: { app, BrowserWindow: Window, ipcMain, protocol, session: { defaultSession: session } },
    trace, handlers, windows, permission, requests };
}
function eventFor(contents, frame = contents.mainFrame) { return { sender: contents, senderFrame: frame }; }
function snapshot() {
  return { availability: 'available', pid: 42, reason: 'private provider detail',
    authority_epoch: '9007199254740993', revision: '18446744073709551615', state: { capsules: [] }, stderrBytes: 42 };
}

test('secure window options and bootstrap protocol do not grant renderer authority', async () => {
  const options = security.browserOptions(__filename);
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.contextIsolation, true);
  for (const key of ['nodeIntegration', 'nodeIntegrationInWorker', 'nodeIntegrationInSubFrames', 'webviewTag', 'allowRunningInsecureContent']) assert.equal(options.webPreferences[key], false);
  assert.equal(options.webPreferences.webSecurity, true);
  assert.throws(() => security.browserOptions('relative.cjs'));
  assert.throws(() => security.browserOptions(path.resolve('bad') + String.fromCharCode(0)));
  const f = nativeFixture(); security.installBootstrapProtocol(f.electron.protocol);
  const response = await f.electron.protocol.handler({ url: security.BOOT_URL, method: 'GET' });
  assert.equal(response.status, 200); assert.equal(response.headers.get('content-security-policy'), security.CSP);
  assert.match(security.CSP, /default-src 'none'/); assert.doesNotMatch(security.CSP, /unsafe-eval|unsafe-inline/);
  assert.equal(await response.text(), '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>');
  for (const url of [security.BOOT_URL + '?extra=1', security.BOOT_URL + '#fragment', 'https://remote.invalid/', 'file:///untrusted']) {
    assert.equal((await f.electron.protocol.handler({ url, method: 'GET' })).status, 403);
  }
  assert.equal((await f.electron.protocol.handler({ url: security.BOOT_URL, method: 'POST' })).status, 403);
});

test('navigation, popup, webview and every permission are denied before an effect', () => {
  const f = nativeFixture(); const win = new f.electron.BrowserWindow({}); security.hardenWindow(win); security.hardenSession(f.electron.session.defaultSession);
  assert.deepEqual(win.webContents.openHandler({ url: 'https://remote.invalid' }), { action: 'deny' });
  for (const name of ['will-redirect', 'will-attach-webview']) { let prevented = false; win.webContents.emit(name, { preventDefault() { prevented = true; } }); assert.equal(prevented, true); }
  let prevented = false; win.webContents.emit('will-navigate', { preventDefault() { prevented = true; } }, 'https://remote.invalid'); assert.equal(prevented, true);
  for (const name of ['media', 'display-capture', 'openExternal', 'unknown']) {
    let grant; f.permission.request(win.webContents, name, (value) => { grant = value; }); assert.equal(grant, false); assert.equal(f.permission.check(win.webContents, name, security.BOOT_URL, {}), false);
  }
  let answer; f.requests.before({ url: 'https://remote.invalid' }, (value) => { answer = value; }); assert.deepEqual(answer, { cancel: true });
  f.requests.before({ url: security.BOOT_URL }, (value) => { answer = value; }); assert.deepEqual(answer, { cancel: false });
});

test('native IPC authenticates actual sender, current top frame and exact URL before reading backend', () => {
  const f = nativeFixture(); const win = new f.electron.BrowserWindow({}); let reads = 0;
  const remove = security.bindNativeIpc(f.electron.ipcMain, () => win, () => { reads++; return null; });
  const invoke = f.handlers.get('cafe:getSnapshot');
  const frame = win.webContents.mainFrame;
  for (const event of [{ sender: {}, senderFrame: frame }, { sender: win.webContents, senderFrame: null }, eventFor(win.webContents, { ...frame, parent: frame }), eventFor(win.webContents, { ...frame }), eventFor(win.webContents, { ...frame, detached: true })]) {
    assert.throws(() => invoke(event), /DENIED/);
  }
  for (const url of ['https://remote.invalid/', security.BOOT_URL + '?x=1', 'cafe://attacker/bootstrap']) {
    frame.url = url; assert.throws(() => invoke(eventFor(win.webContents)), /DENIED/);
  }
  frame.url = security.BOOT_URL; frame.detached = true; assert.throws(() => invoke(eventFor(win.webContents)), /DENIED/); frame.detached = false;
  assert.equal(reads, 0); frame.url = security.BOOT_URL;
  assert.equal(invoke(eventFor(win.webContents)).availability, 'unavailable');
  assert.deepEqual([...f.handlers.keys()].sort(), ['cafe:getSnapshot', 'cafe:submitIntent']);
  assert.equal(f.handlers.has('cafe:spawn'), false); assert.throws(() => security.admitInvokeChannel('cafe:spawn'));
  remove(); assert.equal(f.handlers.size, 0);
});

test('public snapshots preserve decimal clocks and remove native PID/provider errors', () => {
  const result = security.publicSnapshot(snapshot());
  assert.equal(result.pid, null); assert.equal(result.authority_epoch, '9007199254740993'); assert.equal(result.revision, '18446744073709551615');
  assert.equal(result.reason, 'authority_available'); assert.equal(result.stderrBytes, 0);
  for (const value of [9007199254740992, '18446744073709551616', '01']) assert.throws(() => security.publicSnapshot({ ...snapshot(), revision: value }));
});

test('closed preload has exactly three frozen methods and discards native IPC event', async () => {
  const ipc = new EventEmitter(); const calls = [];
  ipc.invoke = async (name, ...args) => { calls.push(name); if (name === 'cafe:getSnapshot') assert.equal(args.length, 0); return snapshot(); };
  let exposed;
  const api = preloadSecurity.exposeClosedBridge({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'cafe'); exposed = value; } } });
  assert.equal(api, exposed); assert.deepEqual(Object.keys(api).sort(), ['getSnapshot', 'onSnapshot', 'submitIntent']); assert.equal(Object.isFrozen(api), true);
  assert.equal(api.ipcRenderer, undefined); assert.equal(api.invoke, undefined);
  assert.equal((await api.getSnapshot()).revision, '18446744073709551615');
  let received; const stop = api.onSnapshot((value) => { received = value; }); const nativeEvent = { nativeCapability: true };
  ipc.emit('cafe:snapshot', nativeEvent, snapshot()); assert.equal(received.nativeCapability, undefined); assert.equal(received.authority_epoch, '9007199254740993');
  stop(); stop(); assert.equal(ipc.listenerCount('cafe:snapshot'), 0);
  const transport = preloadSecurity.closedTransport(ipc);
  await assert.rejects(() => transport.invoke('arbitrary', {}), /DENIED/);
  assert.throws(() => transport.subscribe('arbitrary', () => {}), /DENIED/);
});

test('bundled sandbox preload loads with electron as the only require capability', async () => {
  const bundle = fs.readFileSync(path.resolve(__dirname, '../../dist/preload/index.cjs'), 'utf8');
  const ipc = new EventEmitter(); let facade; const required = [];
  const electron = { ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_name, api) => { facade = api; } } };
  const context = { process: { type: 'renderer' }, require: (name) => { required.push(name); assert.equal(name, 'electron'); return electron; }, exports: {}, module: { exports: {} }, TextEncoder, TextDecoder, console: { log() { assert.fail('preload logging'); } } };
  const realm = vm.createContext({ ...context, snapshotWire: JSON.stringify(snapshot()) });
  ipc.invoke = async () => vm.runInContext('JSON.parse(snapshotWire)', realm);
  vm.runInContext(bundle, realm, { timeout: 1000 });
  assert.deepEqual(required, ['electron']); assert.equal(Object.isFrozen(facade), true);
  assert.deepEqual(Object.keys(facade).sort(), ['getSnapshot', 'onSnapshot', 'submitIntent']);
  assert.equal((await facade.getSnapshot()).authority_epoch, '9007199254740993');
});

test('dangerous launch switches are rejected without relaxed sandbox fallback', () => {
  for (const name of ['no-sandbox', 'disable-web-security', 'remote-debugging-port', 'inspect', 'js-flags']) {
    assert.throws(() => security.assertSafeLaunch({ hasSwitch: (value) => value === name }), /REJECTED/);
  }
});

test('secondary process never creates a window or authority', async () => {
  const f = nativeFixture(false); let factories = 0;
  const shell = await createDesktopShell(f.electron, { createBackend: () => { factories++; throw Error('not called'); } });
  assert.equal(shell.secondary, true); assert.equal(factories, 0); assert.equal(f.windows.length, 0); assert.ok(f.trace.includes('quit'));
});

test('unbound production adapter is visibly unavailable, never fixture success', async () => {
  const f = nativeFixture(); const shell = await createDesktopShell(f.electron, {});
  try {
    const win = shell.getWindow(); const read = f.handlers.get('cafe:getSnapshot'); const state = read(eventFor(win.webContents));
    assert.equal(state.availability, 'unavailable'); assert.equal(state.reason, 'authority_adapter_unbound'); assert.equal(state.pid, null);
    assert.throws(() => f.handlers.get('cafe:submitIntent')(eventFor(win.webContents), {}), /UNAVAILABLE/);
    assert.ok(f.trace.indexOf('lock') < f.trace.indexOf('window')); assert.ok(f.trace.indexOf('sandbox') < f.trace.indexOf('ready'));
  } finally { shell.dispose(); }
});

test('real owned child survives renderer reload with unchanged authority count and revokes old frame', async () => {
  const f = nativeFixture(); const children = [], owned = new Set(); let starts = 0;
  const script = "process.stdout.write(JSON.stringify({kind:'snapshot',authority_epoch:'9007199254740993',revision:'0',state:{recording:true}})+'\\n');process.stdin.resume();setInterval(()=>{},1000)";
  const shell = await createDesktopShell(f.electron, { createBackend: (bindings) => {
    const backend = createBackendSupervisor({ ...bindings, command: process.execPath, args: ['-e', script], spawn: (command, args, options) => {
      starts++; const child = spawn(command, args, options); children.push(child); owned.add(child); return security.ownedChildAdapter(child, owned);
    } }); return backend;
  } });
  try {
    const win = shell.getWindow(); const old = win.webContents.mainFrame; const read = f.handlers.get('cafe:getSnapshot');
    await until(() => read(eventFor(win.webContents)).availability === 'available');
    const first = read(eventFor(win.webContents)); assert.equal(first.authority_epoch, '9007199254740993'); assert.equal(first.pid, null);
    win.webContents.mainFrame = { url: security.BOOT_URL, detached: false, parent: null }; win.webContents.emit('did-finish-load');
    assert.throws(() => read(eventFor(win.webContents, old)), /DENIED/);
    assert.equal(read(eventFor(win.webContents)).state.recording, true); assert.equal(starts, 1); assert.equal(children.length, 1);
    assert.ok(children[0].pid > 1 && children[0].pid !== process.pid);
  } finally {
    shell.dispose();
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null) await Promise.race([once(child, 'exit'), delay(3000).then(() => { throw Error('owned child not reaped'); })]);
    }
  }
});

test('signal adapter never calls kill for self, sentinel or unknown child', () => {
  let calls = 0;
  for (const pid of [0, 1, process.pid]) {
    const child = { pid, kill() { calls++; return true; } };
    assert.throws(() => security.ownedChildAdapter(child, new Set([child])), /OWNERSHIP/);
  }
  const child = { pid: process.pid + 1, exitCode: null, signalCode: null, kill() { calls++; return true; }, on() {} };
  assert.throws(() => security.ownedChildAdapter(child, new Set()), /OWNERSHIP/);
  const owned = new Set([child]); const adapter = security.ownedChildAdapter(child, owned); owned.delete(child); assert.throws(() => adapter.kill(), /OWNERSHIP/); assert.equal(calls, 0);
});


test('actual Electron browser entry starts the secure shell without an external caller', async () => {
  const f = nativeFixture(); const source = fs.readFileSync(path.resolve(__dirname, '../../src/main/index.js'), 'utf8');
  const entry = { exports: {} };
  const imports = (name) => {
    if (name === 'electron') return f.electron;
    if (name === './shellSecurity') return security;
    if (name === './rendererAssets') return require('../../src/main/rendererAssets');
    if (name === 'node:path') return path;
    assert.fail('unexpected entry import');
  };
  imports.main = entry;
  vm.runInNewContext(source, { module: entry, exports: entry.exports, require: imports,
    process: { type: 'browser', versions: { electron: '44.9.2' }, exitCode: 0 } }, { timeout: 1000 });
  try { await until(() => f.windows.length === 1); assert.equal(f.windows[0].loaded, security.BOOT_URL); }
  finally { f.electron.app.emit('before-quit'); }
});

test('failed authority startup removes privileged handlers and propagates only a fixed error', async () => {
  const f = nativeFixture(); let disposed = 0;
  await assert.rejects(() => createDesktopShell(f.electron, { createBackend: () => ({
    start() { throw new Error('synthetic private provider text'); },
    dispose() { disposed++; },
  }) }), (error) => error.message === 'SHELL_BOOT_REJECTED');
  assert.equal(disposed, 1); assert.equal(f.handlers.size, 0);
});
