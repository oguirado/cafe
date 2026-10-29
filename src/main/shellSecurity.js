// @ts-check
'use strict';
const path = require('node:path');
const rendererAssets = require('./rendererAssets');

const BOOT_URL = 'cafe://app/bootstrap';
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'none'";
const INVOKE_CHANNELS = Object.freeze(['cafe:getSnapshot', 'cafe:submitIntent']);
const SNAPSHOT_CHANNEL = 'cafe:snapshot';

/** @param {unknown} value @returns {boolean} */
function isBootstrapUrl(value) { return value === BOOT_URL; }

/** @param {unknown} value @returns {import('electron').BrowserWindowConstructorOptions} */
function browserOptions(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || value.includes('\0') || path.resolve(value) !== value) {
    throw new Error('PRELOAD_PATH_REJECTED');
  }
  return {
    show: false,
    webPreferences: {
      preload: value, contextIsolation: true, sandbox: true,
      nodeIntegration: false, nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false, webSecurity: true,
      allowRunningInsecureContent: false, webviewTag: false,
      experimentalFeatures: false, spellcheck: false,
    },
  };
}

/**
 * The sender, current top-frame object and exact internal URL are independently
 * checked before any adapter call. Native accessors may throw during teardown.
 * @param {unknown} event @param {import('electron').WebContents} contents
 * @returns {{origin:string,frame:object,isMainFrame:true}}
 */
function admitCaller(event, contents) {
  let frame;
  let admitted = false;
  try {
    if (typeof event !== 'object' || event === null || contents.isDestroyed()) throw new Error();
    const record = /** @type {import('electron').IpcMainInvokeEvent} */ (event);
    frame = record.senderFrame;
    admitted = record.sender === contents && frame !== null &&
      frame === contents.mainFrame && frame.detached === false &&
      frame.parent === null && isBootstrapUrl(frame.url);
  } catch { admitted = false; }
  if (!admitted || !frame) throw new Error('IPC_CALLER_DENIED');
  return callerRecord(frame);
}

/**
 * @param {object} frame
 * @returns {{origin:string,frame:object,isMainFrame:true}}
 */
function callerRecord(frame) { return { origin: 'cafe://app', frame, isMainFrame: true }; }

/** @param {string} value */
function admitInvokeChannel(value) {
  if (!INVOKE_CHANNELS.includes(value)) throw new Error('IPC_CHANNEL_DENIED');
}

/** @param {import('electron').Session} session
 * @param {import('./rendererAssets').RendererBundle} [bundle] */
function hardenSession(session, bundle) {
  session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
  session.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    callback({ cancel: !isBootstrapUrl(details.url) && !rendererAssets.admitsResource(details.url, bundle) });
  });
  session.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...details.responseHeaders };
    for (const key of Object.keys(headers)) {
      if (key.toLowerCase() === 'content-security-policy') delete headers[key];
    }
    headers['Content-Security-Policy'] = [CSP];
    callback({ responseHeaders: headers });
  });
}

/** @param {import('electron').BrowserWindow} win */
function hardenWindow(win) {
  win.setMenu(null);
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => { if (!isBootstrapUrl(url)) event.preventDefault(); });
  win.webContents.on('will-redirect', (event) => event.preventDefault());
  win.webContents.on('will-attach-webview', (event) => event.preventDefault());
}

/** @param {import('electron').Protocol} protocol
 * @param {import('./rendererAssets').RendererBundle} [bundle] */
function installBootstrapProtocol(protocol, bundle) {
  protocol.handle('cafe', (request) => {
    if (request.method !== 'GET' || (!isBootstrapUrl(request.url) && !rendererAssets.admitsResource(request.url, bundle))) {
      return new Response('', { status: 403, headers: { 'Content-Security-Policy': CSP } });
    }
    if (!isBootstrapUrl(request.url) && bundle) return rendererAssets.rendererResponse(request.url, bundle);
    return new Response(bundle ? rendererAssets.HTML : '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>', {
      status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': CSP },
    });
  });
}

/** @returns {Readonly<import('./backendSupervisor').SupervisorStatus>} */
function unavailableSnapshot() {
  return Object.freeze({ availability: 'unavailable', pid: null,
    reason: 'authority_adapter_unbound', authority_epoch: null,
    revision: null, state: null, stderrBytes: 0 });
}

/**
 * Only an actual host-owned child can reach the native kill method. The
 * renderer never receives this adapter or a process identifier.
 * @param {import('node:child_process').ChildProcessWithoutNullStreams} child
 * @param {Set<import('node:child_process').ChildProcessWithoutNullStreams>} owned
 */
function ownedChildAdapter(child, owned) {
  const recorded = child.pid;
  if (!Number.isSafeInteger(recorded) || /** @type {number} */ (recorded) <= 1 || recorded === process.pid || !owned.has(child)) {
    throw new Error('CHILD_OWNERSHIP_REJECTED');
  }
  const kill = () => {
    if (!owned.has(child) || child.pid !== recorded || /** @type {number} */ (recorded) <= 1 || recorded === process.pid) {
      throw new Error('CHILD_OWNERSHIP_REJECTED');
    }
    if (child.exitCode !== null || child.signalCode !== null) return false;
    return child.kill();
  };
  return { pid: recorded, stdout: child.stdout, stderr: child.stderr, stdin: child.stdin,
    on: child.on.bind(child), kill };
}


/** @typedef {ReturnType<typeof import('./backendSupervisor').createBackendSupervisor>} Backend */

/** @param {unknown} value @returns {boolean} */
function decimalClock(value) {
  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) &&
    BigInt(value) <= 18446744073709551615n;
}

/** @param {import('./backendSupervisor').SupervisorStatus} value */
function publicSnapshot(value) {
  if (!['starting', 'available', 'unavailable'].includes(value.availability) ||
      (value.authority_epoch !== null && !decimalClock(value.authority_epoch)) ||
      (value.revision !== null && !decimalClock(value.revision)) ||
      (value.availability === 'available' &&
       (!decimalClock(value.authority_epoch) || !decimalClock(value.revision)))) {
    throw new Error('SNAPSHOT_REJECTED');
  }
  let state = null;
  if (value.state !== null) {
    const wire = JSON.stringify(value.state);
    if (Buffer.byteLength(wire, 'utf8') > 4096) throw new Error('SNAPSHOT_REJECTED');
    state = JSON.parse(wire);
    if (typeof state !== 'object' || state === null || Array.isArray(state)) throw new Error('SNAPSHOT_REJECTED');
  }
  return Object.freeze({ availability: value.availability, pid: null,
    reason: value.availability === 'available' ? 'authority_available' : 'authority_unavailable',
    authority_epoch: value.authority_epoch, revision: value.revision, state, stderrBytes: 0 });
}

/**
 * Only these native handlers exist; no generic invoke channel is registered.
 * @param {import('electron').IpcMain} ipcMain
 * @param {() => import('electron').BrowserWindow|null} getWindow
 * @param {() => Backend|null} getBackend
 * @param {(window:import('electron').BrowserWindow)=>Promise<{path:string,sha256:string}|null>} [chooseFfmpeg]
 * @param {(window:import('electron').BrowserWindow)=>Promise<{folder:string}|null>} [chooseFolder]
 */
function bindNativeIpc(ipcMain, getWindow, getBackend, chooseFfmpeg, chooseFolder) {
  ipcMain.handle('cafe:getSnapshot', (event, ...args) => {
    const win = getWindow();
    if (!win || args.length !== 0) throw new Error('IPC_REQUEST_REJECTED');
    const caller = admitCaller(event, win.webContents);
    const backend = getBackend();
    if (!backend) return unavailableSnapshot();
    try {
      return publicSnapshot(/** @type {import('./backendSupervisor').SupervisorStatus} */
        (backend.handle(caller, 'getSnapshot')));
    } catch { throw new Error('IPC_REQUEST_REJECTED'); }
  });
  ipcMain.handle('cafe:submitIntent', (event, ...args) => {
    const win = getWindow();
    if (!win || args.length !== 1) throw new Error('IPC_REQUEST_REJECTED');
    const caller = admitCaller(event, win.webContents);
    const backend = getBackend();
    if (!backend) throw new Error('AUTHORITY_UNAVAILABLE');
    const payload = args[0];
    if (typeof payload === 'object' && payload !== null && !Array.isArray(payload) &&
        typeof payload.body === 'object' && payload.body !== null && ['choose_ffmpeg', 'choose_folder'].includes(payload.body.op)) {
      // Native choice carries the path/hash; renderer may request a dialog only.
      const picker = payload.body.op === 'choose_ffmpeg' ? chooseFfmpeg : chooseFolder;
      const keys = Object.keys(payload).sort().join(',');
      const status = backend.getStatus();
      if (!picker || keys !== 'authority_epoch,body,expected_revision,kind,request_id,schema' ||
          Object.keys(payload.body).join(',') !== 'op' || payload.schema !== 'cafe.operator.v1' ||
          payload.kind !== 'operator_intent' || typeof payload.request_id !== 'string' ||
          !/^[A-Za-z0-9_-]{1,96}$/.test(payload.request_id) || status.availability !== 'available' ||
          payload.authority_epoch !== status.authority_epoch || payload.expected_revision !== status.revision) throw new Error('IPC_REQUEST_REJECTED');
      return picker(win).then(selected => {
        // Selection may outlive the current frame or authority revision.
        const active = getWindow();
        if (!active || active !== win) throw new Error('IPC_REQUEST_REJECTED');
        admitCaller(event, active.webContents);
        if (!selected) return { queued: false, cancelled: true };
        return backend.submitHostSelection({ ...payload, body: { op: payload.body.op === 'choose_ffmpeg' ? 'configure_ffmpeg' : 'set_folder', ...selected } });
      }).catch(() => { throw new Error('IPC_REQUEST_REJECTED'); });
    }
    try { return backend.handle(caller, 'submitIntent', payload); }
    catch { throw new Error('IPC_REQUEST_REJECTED'); }
  });
  return () => { for (const channel of INVOKE_CHANNELS) ipcMain.removeHandler(channel); };
}

/**
 * Called by the main-owned subscription, never by a renderer. Detached or
 * navigated documents cannot receive a snapshot during a reload race.
 * @param {import('electron').BrowserWindow|null} win
 * @param {import('./backendSupervisor').SupervisorStatus} value
 */
function publishSnapshot(win, value) {
  if (!win) return;
  try {
    admitCaller({ sender: win.webContents, senderFrame: win.webContents.mainFrame }, win.webContents);
    win.webContents.send(SNAPSHOT_CHANNEL, publicSnapshot(value));
  } catch { /* No native/provider exception crosses the closed bridge. */ }
}


/** @param {import('electron').CommandLine} commandLine */
function assertSafeLaunch(commandLine) {
  const denied = ['no-sandbox', 'disable-web-security', 'allow-running-insecure-content',
    'ignore-certificate-errors', 'remote-debugging-port', 'remote-debugging-pipe',
    'inspect', 'inspect-brk', 'inspect-port', 'js-flags', 'disable-site-isolation-trials'];
  if (denied.some((name) => commandLine.hasSwitch(name))) throw new Error('LAUNCH_SWITCH_REJECTED');
}

/** @param {() => import('electron').BrowserWindow|null} getWindow @param {Backend} backend */
function subscribeBackend(getWindow, backend) {
  const win = getWindow();
  if (!win) throw new Error('IPC_CALLER_DENIED');
  const caller = admitCaller({ sender: win.webContents, senderFrame: win.webContents.mainFrame }, win.webContents);
  return backend.subscribe(caller, (value) => publishSnapshot(getWindow(), value));
}

exports.BOOT_URL = BOOT_URL;
exports.CSP = CSP;
exports.INVOKE_CHANNELS = INVOKE_CHANNELS;
exports.SNAPSHOT_CHANNEL = SNAPSHOT_CHANNEL;
exports.isBootstrapUrl = isBootstrapUrl;
exports.browserOptions = browserOptions;
exports.admitCaller = admitCaller;
exports.admitInvokeChannel = admitInvokeChannel;
exports.hardenSession = hardenSession;
exports.hardenWindow = hardenWindow;
exports.installBootstrapProtocol = installBootstrapProtocol;
exports.unavailableSnapshot = unavailableSnapshot;
exports.ownedChildAdapter = ownedChildAdapter;

exports.publicSnapshot = publicSnapshot;
exports.bindNativeIpc = bindNativeIpc;
exports.publishSnapshot = publishSnapshot;

exports.assertSafeLaunch = assertSafeLaunch;
exports.subscribeBackend = subscribeBackend;

/** Fixed public startup error; provider text is never forwarded. */
function bootRejected() { return new Error('SHELL_BOOT_REJECTED'); }
exports.bootRejected = bootRejected;
