// @ts-check
'use strict';

const path = require('node:path');
const security = require('./shellSecurity');
const rendererAssets = require('./rendererAssets');

/**
 * @typedef {ReturnType<typeof import('./backendSupervisor').createBackendSupervisor>} Backend
 */

/**
 * @typedef {Object} DesktopShellOptions
 * @property {string} [preloadPath] - Host-owned absolute compiled preload path. Defaults to path.join(app.getAppPath(), 'dist', 'preload', 'index.cjs').
 * @property {(ctx: { authorizedOrigin: string, getAuthorizedFrame: () => object }) => Backend} [createBackend] - Optional factory called at most once on primary instance.
 */

/**
 * Named host-only controller for the secure desktop shell.
 * Owns the public `secondary` flag and the `getWindow` and `dispose`
 * method coordinates, backed by host-only lifecycle callbacks supplied by
 * the shell closures. Each instance is frozen; methods stay bound so
 * extraction preserves callback semantics.
 */
class DesktopShellController {
  /**
   * @param {boolean} secondary - True if requestSingleInstanceLock() failed.
   * @param {() => import('electron').BrowserWindow | null} getWindow - Returns current active BrowserWindow or null.
   * @param {() => void} dispose - Idempotent teardown of subscription, IPC handlers, and main-owned backend.
   */
  constructor(secondary, getWindow, dispose) {
    /** @type {boolean} */
    this.secondary = secondary;
    /** @type {() => import('electron').BrowserWindow | null} */
    this._getWindowCallback = getWindow;
    /** @type {() => void} */
    this._disposeCallback = dispose;
    this.getWindow = this.getWindow.bind(this);
    this.dispose = this.dispose.bind(this);
    Object.freeze(this);
  }

  /**
   * Returns the current active BrowserWindow or null.
   * @returns {import('electron').BrowserWindow | null}
   */
  getWindow() {
    return this._getWindowCallback();
  }

  /**
   * Idempotent teardown of subscription, IPC handlers, and main-owned backend.
   * @returns {void}
   */
  dispose() {
    this._disposeCallback();
  }
}

/**
 * Secure Electron boot shell. Owns the single-instance gate, the pre-ready
 * sandbox hooks, one hardened window, and at most one main-owned backend.
 * Every validation, admission, and IPC policy decision lives in the supplied
 * security module; this entry only orders calls and owns lifecycle.
 * @param {typeof import('electron')} electronApi
 * @param {DesktopShellOptions} [options]
 * @returns {Promise<DesktopShellController>}
 */
async function createDesktopShell(electronApi, options) {
  const app = electronApi.app;
  const BrowserWindow = electronApi.BrowserWindow;
  const ipcMain = electronApi.ipcMain;
  const protocol = electronApi.protocol;
  const session = electronApi.session;

  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return new DesktopShellController(true, () => null, () => {});
  }

  security.assertSafeLaunch(app.commandLine);
  app.enableSandbox();
  protocol.registerSchemesAsPrivileged([
    { scheme: 'cafe', privileges: { standard: true, secure: true } },
  ]);

  const resolvedOptions = options === undefined ? {} : options;
  const preloadPath = resolvedOptions.preloadPath === undefined
    ? path.join(app.getAppPath(), 'dist', 'preload', 'index.cjs')
    : resolvedOptions.preloadPath;

  await app.whenReady();

  const rendererBundle = rendererAssets.loadRendererBundle(app.getAppPath());
  security.hardenSession(session.defaultSession, rendererBundle);
  security.installBootstrapProtocol(protocol, rendererBundle);

  /** @type {import('electron').BrowserWindow | null} */
  let currentWin = null;
  /** @type {(() => void) | null} */
  let unsubscribeBackend = null;
  /** @type {object|null} */
  let subscriptionFrame = null;
  /** @type {Backend | null} */
  let backend = null;
  let disposed = false;

  // Stable unbound-frame token returned until a real window exists. It is
  // never admitted by native IPC: with no window, admission fails first.
  const unboundFrame = {};

  const getWindow = () => {
    if (disposed || !currentWin || currentWin.isDestroyed()) return null;
    return currentWin;
  };
  const getBackend = () => backend;
  const getAuthorizedFrame = () => {
    if (!currentWin || currentWin.isDestroyed() || currentWin.webContents.isDestroyed()) return unboundFrame;
    return currentWin.webContents.mainFrame;
  };

  const removeIpcHandlers = security.bindNativeIpc(ipcMain, getWindow, getBackend, async (win) => {
    const choice = await electronApi.dialog.showOpenDialog(win, {
      title: 'Select audited FFmpeg executable', properties: ['openFile'],
      filters: [{ name: 'FFmpeg executable', extensions: ['exe'] }],
    });
    if (choice.canceled || choice.filePaths.length !== 1) return null;
    return require('./runtimeBackend').selectExternalFfmpeg(choice.filePaths[0]);
  }, async (win) => {
    const choice = await electronApi.dialog.showOpenDialog(win, {
      title: 'Choose recording folder', properties: ['openDirectory', 'createDirectory'],
    });
    if (choice.canceled || choice.filePaths.length !== 1) return null;
    return { folder: choice.filePaths[0] };
  });

  const clearBackendSubscription = () => {
    subscriptionFrame = null;
    if (unsubscribeBackend) {
      const stop = unsubscribeBackend;
      unsubscribeBackend = null;
      stop();
    }
  };

  /** @param {import('electron').BrowserWindow} win */
  const bindBackendSnapshots = (win) => {
    if (!backend || getWindow() !== win) return;
    const frame = win.webContents.mainFrame;
    if (unsubscribeBackend && subscriptionFrame === frame) return;
    clearBackendSubscription();
    unsubscribeBackend = security.subscribeBackend(getWindow, backend);
    subscriptionFrame = frame;
  };

  const createAndShowWindow = async () => {
    if (disposed) return;
    const win = new BrowserWindow(security.browserOptions(preloadPath));
    currentWin = win;
    security.hardenWindow(win);
    win.once('ready-to-show', () => {
      win.show();
    });
    win.on('closed', () => {
      clearBackendSubscription();
      if (currentWin === win) currentWin = null;
    });
    win.webContents.on('did-finish-load', () => {
      try { bindBackendSnapshots(win); }
      catch { security.publishSnapshot(win, security.unavailableSnapshot()); }
    });
    await win.loadURL(security.BOOT_URL);
    if (disposed || currentWin !== win || win.isDestroyed()) return;
    if (backend) bindBackendSnapshots(win);
  };

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    clearBackendSubscription();
    currentWin = null;
    removeIpcHandlers();
    if (backend) backend.dispose();
  };

  app.on('window-all-closed', () => {
    app.quit();
  });
  app.on('activate', () => {
    if (!disposed && getWindow() === null) {
      void createAndShowWindow().catch(() => {});
    }
  });
  app.on('before-quit', () => {
    dispose();
  });

  try {
    if (typeof resolvedOptions.createBackend === 'function') {
      backend = resolvedOptions.createBackend({
        authorizedOrigin: 'cafe://app',
        getAuthorizedFrame,
      });
      backend.start();
    }
    await createAndShowWindow();
  } catch {
    dispose();
    throw security.bootRejected();
  }
  return new DesktopShellController(false, getWindow, dispose);
}

exports.DesktopShellController = DesktopShellController;
exports.createDesktopShell = createDesktopShell;

if (typeof process !== 'undefined' && process !== null && process.type === 'browser') {
  const browserApi = require('electron');
  createDesktopShell(browserApi, { createBackend: (ctx) =>
    require('./runtimeBackend').createPackagedBackend({ ...ctx,
      resourcesPath: process.resourcesPath,
      runtimeRoot: path.join(browserApi.app.getPath('userData'), 'authority'),
    }) }).then(
    () => {},
    () => {
      browserApi.app.quit();
      process.exitCode = 1;
    },
  );
}
